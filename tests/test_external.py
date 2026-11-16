"""Outside data (Sleeper, nflverse, ESPN lines, weather) -> tools, offline with saved samples."""

import copy
import gzip
import json
from pathlib import Path

import pytest

from espn_fantasy_hub import external, insights

EXT = json.load(gzip.open(Path(__file__).parent / "fixtures" / "external.json.gz", "rt"))


@pytest.fixture
def ext(monkeypatch):
    calls = []

    def board(season, week):
        calls.append(("nfl_scoreboard", week))
        if week == 11:
            return EXT["nfl_sb"]
        done = copy.deepcopy(EXT["nfl_sb"])     # the current week: every game already played
        for ev in done["events"]:
            ev["competitions"][0]["status"]["type"]["name"] = "STATUS_FINAL"
        return done

    monkeypatch.setattr(external, "sleeper_trending", lambda sport, kind, **k: EXT["sleeper_" + kind])
    monkeypatch.setattr(external, "nfl_id_map", lambda: EXT["idmap"])
    monkeypatch.setattr(external, "nflverse_weekly", lambda season: EXT["weekly"])
    monkeypatch.setattr(external, "nflverse_snaps", lambda season: EXT["snaps"])
    monkeypatch.setattr(external, "nfl_scoreboard", board)
    monkeypatch.setattr(external, "weather", lambda pts: [EXT["meteo"][0]] * len(pts))
    return calls


def test_market_trends_maps_sleeper_to_league(football_hub, ext, monkeypatch):
    adds_in = EXT["sleeper_add"][:10] + [{"player_id": "99999999", "count": 5}]   # last one: unknown to the id map
    monkeypatch.setattr(external, "sleeper_trending", lambda sport, kind, **k: adds_in if kind == "add" else EXT["sleeper_drop"])
    out = football_hub.market_trends()
    adds = out["most_added_24h"]
    assert adds and all(r["player"] and r["here"] for r in adds)
    assert len(adds) == 10 and all(r["managers_moving_24h"] != 5 for r in adds)
    for r in out.get("available_and_trending") or []:
        assert r["here"] in ("free agent", "on waivers")


def test_player_usage(football_hub, ext):
    out = football_hub.player_usage()
    rows = out["your_players"]
    assert rows and all(r["games"] >= 1 for r in rows)
    wr = next(r for r in rows if r["pos"] in ("WR", "TE"))
    season, last = (float(x.strip().rstrip("%")) for x in wr["target_share"].split("/"))
    assert 0 <= season <= 100 and 0 <= last <= 100
    for r in rows:
        for f in r.get("flags") or []:
            assert any(k in f for k in ("rising", "falling", "sell-high", "buy-low"))


def test_game_environment_moves_to_next_week_and_adds_weather(football_hub, ext):
    out = football_hub.game_environment()
    assert out["week"] == 11                      # week 10 is all final, so plan for week 11
    assert "still_to_play_last_week" not in out
    games = out["games"]
    assert len(games) == 13 and len(out["teams_on_bye"]) == 6
    totals = [g["total"] for g in games if "total" in g]
    assert totals == sorted(totals, reverse=True)
    g = next(g for g in games if "implied_points" in g)
    a, h = [float(x.split()[-1]) for x in g["implied_points"].split("/")]
    assert a + h == pytest.approx(g["total"], abs=0.11)
    assert any("weather" in g for g in games) and not any("weather" in g for g in games if g.get("indoor"))
    assert out["dst_streaming"] and all("espn_projection" in r for r in out["dst_streaming"])


def test_implied_totals_math():
    sb = {"events": [{"id": "1", "date": "2026-11-22T18:00Z", "competitions": [{
        "competitors": [{"homeAway": "home", "team": {"id": "2", "abbreviation": "BUF"}},
                        {"homeAway": "away", "team": {"id": "15", "abbreviation": "MIA"}}],
        "odds": [{"spread": -7.0, "overUnder": 47.0}], "venue": {"indoor": False, "address": {"country": "USA"}},
        "status": {"type": {"name": "STATUS_SCHEDULED"}}}]}]}
    g = insights.game_lines(sb)["games"][0]
    assert (g["home_implied"], g["away_implied"]) == (27.0, 20.0)
    assert insights.weather_points([g]) == [("1", insights.STADIUMS["BUF"])]


def test_weather_flags():
    game = {"start": "2026-11-22T18:00Z"}
    hours = [f"2026-11-22T{h:02d}:00" for h in range(24)]
    fc = {"hourly": {"time": hours, "wind_speed_10m": [22.0] * 24, "precipitation_probability": [70] * 24,
                     "temperature_2m": [30.0] * 24}}
    w = insights.weather_flags(game, fc)
    assert w["wind_mph"] == 22 and "very windy" in w["flags"][0] and "snow likely" in w["flags"]
    assert insights.weather_flags({"start": "2026-12-30T18:00Z"}, fc) == {"note": "beyond the 10-day forecast"}


def test_faab_bid_scales_with_gain_and_caps():
    hist = {"median_winning_bid": 3, "75th_percentile": 10, "90th_percentile": 14}
    assert insights.faab_bid(0.5, hist, 90, False) == "$0-$1"
    assert insights.faab_bid(1.5, hist, 90, False) == "$3-$10"
    assert insights.faab_bid(3.0, hist, 90, False) == "$10-$14"
    assert insights.faab_bid(3.0, hist, 90, True) == "$14-$18"
    assert insights.faab_bid(3.0, hist, 20, True) == "$8"     # never more than 40% of what's left
    assert insights.faab_bid(1.5, hist, 90, False, "D/ST") == "$0-$1"   # streamers stay cheap
    assert insights.faab_bid(1.5, hist, 90, True, "K") == "$1-$2"


def test_waiver_advisor_football_protects_core(football_hub, ext):
    from espn_fantasy_hub import ffmodel
    out = football_hub.waiver_plan()
    h = football_hub._handles[0]
    view = football_hub.view(h)
    world = football_hub.football_world(h, view)
    core = ffmodel.regular_starters(view, world.team(1))
    assert len(out["rotation_spots"]) <= 2
    assert not any(r.split(" (")[0] in core for r in out["rotation_spots"])
    if isinstance(out["best_moves"], list):
        gains = [m["lineup_gain_per_week"] for m in out["best_moves"]]
        assert gains == sorted(gains, reverse=True) and min(gains) > 0
        assert all(m["drop"] in out["rotation_spots"] for m in out["best_moves"])


def test_waiver_advisor_basketball(basketball_hub, monkeypatch):
    monkeypatch.setattr(external, "sleeper_trending", lambda *a, **k: None)
    out = basketball_hub.waiver_plan()
    assert len(out["rotation_spots"]) <= 2


def test_outside_source_down_is_reported(football_hub, monkeypatch):
    from espn_fantasy_hub.service import UserError
    monkeypatch.setattr(external, "sleeper_trending", lambda *a, **k: None)
    with pytest.raises(UserError, match="unavailable"):
        football_hub.market_trends()
