import asyncio
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from espn_fantasy_hub.config import ConfigError, load_settings, normalize_swid, parse_leagues_env
from espn_fantasy_hub.discovery import parse_fan_profile
from espn_fantasy_hub.server import build_server
from espn_fantasy_hub.sports import FOOTBALL_SPORT, BASKETBALL_SPORT
import datetime as dt


def test_parse_leagues_env():
    lg = parse_leagues_env("football:123, basketball:456:2027:3 ,nfl:789")
    assert [(l.sport, l.league_id, l.season, l.team_id) for l in lg] == [
        ("football", 123, None, None),
        ("basketball", 456, 2027, 3),
        ("football", 789, None, None),
    ]
    with pytest.raises(ConfigError):
        parse_leagues_env("hockey:1")
    with pytest.raises(ConfigError):
        parse_leagues_env("football:abc")


def test_swid_normalization():
    assert normalize_swid("abcdef01-2345-6789-abcd-ef0123456789") == "{ABCDEF01-2345-6789-ABCD-EF0123456789}"
    assert normalize_swid(' "{ABCDEF01-2345-6789-ABCD-EF0123456789}" ') == "{ABCDEF01-2345-6789-ABCD-EF0123456789}"
    with pytest.raises(ConfigError):
        normalize_swid("not-a-swid")


def test_load_settings_env_and_file(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'espn_s2 = "S2VALUE"\nswid = "{ABCDEF01-2345-6789-ABCD-EF0123456789}"\n'
        '[[league]]\nsport = "football"\nid = 111\nname = "Work"\n'
    )
    s = load_settings(environ={"ESPN_FANTASY_CONFIG": str(cfg), "ESPN_LEAGUES": "basketball:222,football:111"})
    assert s.has_credentials
    assert [(l.sport, l.league_id, l.name) for l in s.leagues] == [("football", 111, "Work"), ("basketball", 222, None)]
    with pytest.raises(ConfigError):
        load_settings(environ={"ESPN_S2": "only-one", "ESPN_FANTASY_CONFIG": str(tmp_path / "none.toml")})


def test_guess_season():
    assert FOOTBALL_SPORT.guess_season(dt.date(2026, 11, 13)) == 2026
    assert FOOTBALL_SPORT.guess_season(dt.date(2027, 1, 20)) == 2026
    assert BASKETBALL_SPORT.guess_season(dt.date(2026, 11, 13)) == 2027
    assert BASKETBALL_SPORT.guess_season(dt.date(2027, 3, 1)) == 2027


def test_parse_fan_profile_keeps_latest_season():
    data = {"preferences": [
        {"metaData": {"entry": {"abbrev": "FFL", "seasonId": 2026, "entryId": 3, "groups": [{"groupId": 111, "groupName": "Work"}]}}},
        {"metaData": {"entry": {"abbrev": "FFL", "seasonId": 2026, "entryId": 5, "groups": [{"groupId": 222}]}}},
        {"metaData": {"entry": {"abbrev": "FFL", "seasonId": 2024, "entryId": 1, "groups": [{"groupId": 999}]}}},
        {"metaData": {"entry": {"abbrev": "FBA", "seasonId": 2027, "entryId": 7, "groups": [{"groupId": 333}]}}},
        {"metaData": {"entry": {"abbrev": "FLB", "seasonId": 2026, "groups": [{"groupId": 444}]}}},
        {"type": {"code": "team"}},
    ]}
    found = parse_fan_profile(data)
    assert [(l.sport, l.league_id, l.team_id) for l in found] == [
        ("basketball", 333, 7), ("football", 111, 3), ("football", 222, 5),
    ]
    assert found[1].name == "Work" and found[1].discovered


def _call(mcp, name, args):
    res = asyncio.run(mcp.call_tool(name, args))
    content = res[0] if isinstance(res, tuple) else res
    return json.loads(content[0].text)


def test_server_tools(football_hub):
    mcp = build_server(football_hub)
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert {"my_leagues", "lineup_check", "free_agents", "matchup_detail", "player_info", "standings"} <= names
    writes = {"set_lineup", "add_player", "drop_player", "propose_trade"}
    assert writes <= names
    for t in tools:
        if t.name in writes:
            assert t.annotations.destructiveHint and not t.annotations.readOnlyHint
        else:
            assert t.annotations.readOnlyHint
    out = _call(mcp, "standings", {})
    assert len(out["standings"]) == 12
    out = _call(mcp, "lineup_check", {"team": "T1"})
    assert out["team"].startswith("Team 1")
    with pytest.raises(ToolError) as e:
        asyncio.run(mcp.call_tool("team_roster", {"team": "nobody"}))
    assert "No team matches" in str(e.value)
