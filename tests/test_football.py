import json

import pytest

from espn_fantasy_hub.config import LeagueConfig
from espn_fantasy_hub.service import UserError
from espn_fantasy_hub.views import find_split, player_of

from conftest import FOOTBALL_NOW, make_hub


def team_entries(data, team_id=1):
    for t in data["core"]["teams"]:
        if t["id"] == team_id:
            return t["roster"]["entries"]
    raise AssertionError("team not found")


def entry_named(data, name, team_id=1):
    for e in team_entries(data, team_id):
        if player_of(e)["fullName"] == name:
            return e
    raise AssertionError(f"{name} not on team {team_id}")


def set_injury(entry, status):
    entry["injuryStatus"] = status
    player_of(entry)["injuryStatus"] = status


def hub_for(data, **kw):
    cfg = LeagueConfig(sport="football", league_id=1086064, season=2026, team_id=kw.pop("team_id", 1))
    return make_hub([(cfg, data)], FOOTBALL_NOW, **kw)


def small(out, limit=30000):
    text = json.dumps(out, separators=(",", ":"))
    assert len(text) < limit, f"output too large ({len(text)} chars)"
    return out


def test_settings(football_hub):
    s = small(football_hub.league_settings())
    assert s["format"] == "Head-to-head points"
    assert s["scoring_summary"].startswith("Half PPR")
    assert s["roster"]["starters"] == 9
    assert s["roster"]["slots"]["FLEX"] == 1
    assert any("Each reception = 0.5" in r for r in s["scoring"])


def test_standings(football_hub):
    out = small(football_hub.standings())
    rows = out["standings"]
    assert len(rows) == 12
    seeds = [r["seed"] for r in rows]
    assert seeds == sorted(seeds)
    assert all("-" in r["record"] for r in rows)
    assert rows[0]["playoff_odds_pct"] >= rows[-1]["playoff_odds_pct"]


def test_my_team_found_from_swid(football_data):
    team1 = [t for t in football_data["core"]["teams"] if t["id"] == 1][0]
    swid = team1["owners"][0]
    hub = hub_for(football_data, team_id=None, swid=swid)
    h = hub.handles()[0]
    assert h.my_team_id(football_data["core"]) == 1


def test_roster_and_stat_names(football_hub):
    out = small(football_hub.roster(team=None, detail=True), 40000)
    players = out["players"]
    slots = [p["slot"] for p in players]
    # starters first, then bench, then IR
    first_bench = slots.index("BE")
    assert all(s not in ("BE", "IR") for s in slots[:first_bench])
    assert "IR" not in slots[:first_bench]
    higgins = next(p for p in players if p["name"] == "Tee Higgins")
    # espn-api's stat map names stat 61 (yards PER GAME) "receivingYards";
    # make sure the season total wins.
    assert higgins["season_stats"]["receivingYards"] == 758
    assert higgins["season_stats"]["receivingYardsPerGame"] == pytest.approx(94.8, abs=0.1)
    assert higgins["eligible"] == ["WR", "FLEX"]  # only slots this league uses
    assert higgins["week"]["proj"] > 0


def test_player_card(football_hub):
    out = small(football_hub.player(name="tee higgins"))
    assert out["name"] == "Tee Higgins"
    assert out["fantasy_team"].startswith("Team 1")
    assert out["week"]["proj"] > 0
    assert len(out["weekly_log"]) >= 8
    assert any(w.get("game") == "BYE" for w in out["weekly_log"])
    assert out["news"] and all("<" not in (n.get("story") or "") for n in out["news"])


def test_player_not_found(football_hub):
    with pytest.raises(UserError):
        football_hub.player(name="zzzz qqqq")


def test_free_agents_grouped_and_locked_last(football_hub):
    out = small(football_hub.free_agents())
    groups = out["by_position"]
    assert set(groups) == {"QB", "RB", "WR", "TE", "D/ST", "K"}
    for pos, rows in groups.items():
        assert all(r["pos"] == pos for r in rows)
        assert all(r["fantasy_team"] in ("Free agent", "On waivers") for r in rows)
    rb = small(football_hub.free_agents(position="RB", limit=50))["players"]
    seen_locked = False
    for r in rb:
        if r.get("locked"):
            seen_locked = True
        else:
            assert not seen_locked, "unlocked player listed after a locked one"


def test_free_agents_season_avg_needs_games(football_hub):
    rows = football_hub.free_agents(position="RB", sort_by="season_avg", limit=5)["players"]
    assert (rows[0]["season_fpts"].get("games") or 0) >= 3


def test_bad_position(football_hub):
    with pytest.raises(UserError) as e:
        football_hub.free_agents(position="XX")
    assert "QB" in str(e.value) and "DT" not in str(e.value)


def test_scoreboard_and_matchup(football_hub):
    sb = small(football_hub.scoreboard())
    assert len(sb["matchups"]) == 6
    m = small(football_hub.matchup())
    assert m["status"] == "in progress"
    assert len(m["you"]["lineup"]) >= 14
    assert m["you"]["projected"] > 0
    prev = small(football_hub.matchup(period=9))
    assert prev["status"] == "final"
    assert any(p.get("pts") for p in prev["you"]["lineup"])


def test_schedule(football_hub):
    out = small(football_hub.schedule())
    assert len(out["matchups"]) == 14
    assert sum(1 for m in out["matchups"] if m.get("result")) == 9
    assert 0 <= out["remaining_opponents_win_pct"] <= 1


def test_transactions(football_hub):
    out = small(football_hub.transactions())
    assert out["transactions"]
    for tx in out["transactions"]:
        assert tx["type"] != "ROSTER"
        for name in tx.get("added", []) + tx.get("dropped", []):
            assert not name.isdigit()


def test_draft(football_hub):
    out = small(football_hub.draft(), 60000)
    assert len(out["picks"]) >= 12 * 10
    mine = football_hub.draft(team="1")
    assert all(p["team"] == "T1" for p in mine["picks"])


def test_all_rosters(football_hub):
    out = small(football_hub.all_rosters(), 40000)
    assert len(out["teams"]) == 12


def test_dashboard(football_hub):
    out = small(football_hub.my_leagues())
    card = out["leagues"][0]
    assert card["your_team"]["team_id"] == 1
    assert "current_matchup" in card
    assert card["lineup_alerts"]


# ---- lineup check scenarios --------------------------------------------------

def test_lineup_baseline(football_hub):
    out = football_hub.lineup_check()
    assert out["lineup_ok"] is True
    assert [i["level"] for i in out["issues"]] == ["watch"]  # a QUESTIONABLE starter
    assert out["starters"][0]["slot"] == "QB"


def test_lineup_out_starter_gets_replacement(football_data):
    set_injury(entry_named(football_data, "Bijan Robinson"), "OUT")
    out = hub_for(football_data).lineup_check()
    assert out["lineup_ok"] is False
    crit = [i for i in out["issues"] if i["level"] == "critical"]
    assert crit and "Bijan Robinson is OUT" in crit[0]["problem"]
    assert "Mike Washington" in crit[0]["fix"]  # the only RB-eligible bench player
    assert out["suggested_moves"][0]["bench"] == "Bijan Robinson"


def test_lineup_bye_week_starter(football_data):
    bye_team = next(
        t["id"] for t in football_data["pro_schedule"]["settings"]["proTeams"]
        if t["id"] and t.get("byeWeek") == 10
    )
    kicker = entry_named(football_data, "Tyler Bass")
    player_of(kicker)["proTeamId"] = bye_team
    out = hub_for(football_data).lineup_check()
    crit = [i for i in out["issues"] if i["level"] == "critical"]
    assert any("has no game this week (BYE)" in i["problem"] for i in crit)
    assert any("free agent" in i["fix"] for i in crit)  # no kicker on the bench


def test_lineup_empty_slot(football_data):
    entry_named(football_data, "Tee Higgins")["lineupSlotId"] = 20  # bench him
    out = hub_for(football_data).lineup_check()
    empty = [i for i in out["issues"] if i["problem"].startswith("Empty WR")]
    assert empty and "Tee Higgins" in empty[0]["fix"]


def test_lineup_upgrade_from_bench(football_data):
    goff = entry_named(football_data, "Jared Goff")
    find_split(player_of(goff), 1, 1, period=10)["appliedTotal"] = 30.0
    out = hub_for(football_data).lineup_check()
    ups = [i for i in out["issues"] if i["level"] == "upgrade"]
    assert ups and "Jared Goff" in ups[0]["fix"] and "Tyler Shough" in ups[0]["fix"]


def test_lineup_healthy_player_on_ir(football_data):
    set_injury(entry_named(football_data, "Kyle Monangai"), "ACTIVE")
    out = hub_for(football_data).lineup_check()
    assert any("IR slot" in i["problem"] for i in out["issues"] if i["level"] == "warning")


def test_league_resolution(football_data, basketball_data):
    hub = make_hub(
        [
            (LeagueConfig(sport="football", league_id=1086064, season=2026, team_id=1), football_data),
            (LeagueConfig(sport="football", league_id=42, season=2026, team_id=1, name="Work league"), football_data),
        ],
        FOOTBALL_NOW,
    )
    with pytest.raises(UserError):
        hub.resolve(None)  # two leagues: must say which
    with pytest.raises(UserError):
        hub.resolve("football")  # ambiguous sport
    assert hub.resolve("work league").cfg.league_id == 42
    assert hub.resolve("1086064").cfg.league_id == 1086064


def test_lineup_open_ir_slot_tip(football_data):
    # Move an injured IR player to the bench: IR slot opens up, he wastes a bench spot.
    entry_named(football_data, "RJ Harvey")["lineupSlotId"] = 20
    out = hub_for(football_data).lineup_check()
    tips = [i for i in out["issues"] if i["level"] == "tip"]
    assert tips and "RJ Harvey" in tips[0]["fix"] and "IR" in tips[0]["fix"]
    assert out["lineup_ok"] is True  # a tip isn't a problem


def test_lineup_bye_hole_gets_free_agent_options(football_data):
    bye_team = next(
        t["id"] for t in football_data["pro_schedule"]["settings"]["proTeams"]
        if t["id"] and t.get("byeWeek") == 10
    )
    player_of(entry_named(football_data, "Tyler Bass"))["proTeamId"] = bye_team
    out = hub_for(football_data).lineup_check()
    crit = next(i for i in out["issues"] if "has no game" in i["problem"])
    assert "_needs_pickup_slot" not in crit
    opts = crit["free_agent_options"]
    assert opts and all(o["game"] != "BYE" and o["game"] != "no game" for o in opts)
    values = [o.get("value", 0) for o in opts]
    assert values == sorted(values, reverse=True)


def test_pickups_status_shown(football_hub):
    team = football_hub.roster()["team"]
    assert team["pickups"]["pickups_this_matchup"] >= 0
    assert "add_limit" not in football_hub.league_settings()["waivers_and_free_agents"]  # unlimited league
