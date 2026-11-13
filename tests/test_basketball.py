import datetime as dt
import json

from espn_fantasy_hub.config import LeagueConfig
from espn_fantasy_hub.views import ProSchedule, player_of
from espn_fantasy_hub.sports import BASKETBALL_SPORT

from conftest import BASKETBALL_NOW, make_hub


def small(out, limit=30000):
    assert len(json.dumps(out, separators=(",", ":"))) < limit
    return out


def test_settings_categories(basketball_hub):
    s = basketball_hub.league_settings()
    assert s["format"] == "Head-to-head each category"
    assert len(s["categories"]) == 8
    assert "FG%" in s["categories"]
    assert s["roster"]["starters"] == 10


def test_standings_category_totals(basketball_hub):
    rows = basketball_hub.standings()["standings"]
    assert len(rows) == 10
    assert all("points_for" not in r for r in rows)
    assert set(rows[0]["season_category_totals"]) <= {"FT%", "REB", "PTS", "BLK", "3PM", "STL", "AST", "FG%"}


def test_roster_rater_and_averages(basketball_hub):
    out = small(basketball_hub.roster(detail=True), 40000)
    p = out["players"][0]
    assert "rater" in p and "season" in p["rater"]
    assert "fpts" not in p  # category league
    assert set(p["season_avg"]) >= {"PTS", "REB", "AST", "FG%"}
    assert p["season_avg"]["FG%"] < 1
    assert "l15_avg" in p
    for row in out["players"]:
        assert all(e in ("PG", "SG", "SF", "PF", "C", "G", "F", "UT") for e in row.get("eligible", []))


def test_scoreboard_categories(basketball_hub):
    out = basketball_hub.scoreboard(period=10)
    assert len(out["matchups"]) == 5
    m = out["matchups"][0]
    assert len(m["categories"]) == 8
    assert m["home"]["cats"].count("-") == 2


def test_matchup_without_lineups(basketball_hub):
    out = basketball_hub.matchup(period=10)
    assert out["status"] == "final"
    assert "note" in out["you"]  # ESPN keeps no box scores for old seasons
    assert {c["leader"] for c in out["categories"]} <= {"you", "opp", "tied"}


def test_player_card_game_log(basketball_hub):
    out = basketball_hub.player(name="James Harden")
    assert out["name"] == "James Harden"
    assert 1 <= len(out["last_10_games"]) <= 10
    assert all(g["line"] == "DNP" or "PTS" in g["line"] for g in out["last_10_games"])


def test_free_agents_sorted_by_rater(basketball_hub):
    rows = basketball_hub.free_agents(limit=10)["players"]
    vals = [r.get("rater", {}).get("l15") for r in rows if r.get("rater", {}).get("l15") is not None]
    assert vals == sorted(vals, reverse=True)


def _in_season_hub(data, day: dt.date):
    pro = ProSchedule(data["pro_schedule"], BASKETBALL_SPORT, dt.timezone.utc)
    period = next(p for p, d in pro.period_date.items() if d == day)
    data["core"]["scoringPeriodId"] = period
    for t in data["core"]["teams"]:
        for e in t["roster"]["entries"]:
            e["playerPoolEntry"]["lineupLocked"] = False
    for w in data["pool"]["players"]:  # fixture is from the season's last day: all locked
        w["lineupLocked"] = False
    now = dt.datetime.combine(day, dt.time(17, 0), tzinfo=dt.timezone.utc)  # noon US Eastern
    cfg = LeagueConfig(sport="basketball", league_id=1631984064, season=2023, team_id=1)
    return make_hub([(cfg, data)], now), period


def test_daily_lineup_check_flags_idle_starters(basketball_data):
    hub, period = _in_season_hub(basketball_data, dt.date(2023, 1, 10))
    out = hub.lineup_check()
    view = hub.view(hub.handles()[0])
    starters = out["starters"]
    no_game = [s for s in starters if s["game"] == "no game"]
    for s in no_game:
        assert any(s["name"] in i["problem"] for i in out["issues"]), s
    # every suggested starter actually plays that day
    bench_games = {b["name"]: b["game"] for b in out["bench"]}
    for move in out["suggested_moves"]:
        assert bench_games[move["start"]] != "no game"
    # games-left counts are computed from the real NBA schedule
    roster = hub.roster()
    assert any(p["games"]["left_this_week"] > 0 for p in roster["players"])
    assert all(p["games"]["left_this_week"] <= p["games"]["next_7_days"] for p in roster["players"])


def test_points_league_variant(basketball_data):
    core = basketball_data["core"]
    core["settings"]["scoringSettings"]["scoringType"] = "H2H_POINTS"
    for t in core["teams"]:
        for e in t["roster"]["entries"]:
            for s in player_of(e).get("stats") or []:
                if s["statSourceId"] == 0 and s["statSplitTypeId"] in (0, 2):
                    s["appliedAverage"] = 30.0 if s["statSplitTypeId"] == 2 else 25.0
                    s["appliedTotal"] = 25.0 * 60
    cfg = LeagueConfig(sport="basketball", league_id=1631984064, season=2023, team_id=1)
    hub = make_hub([(cfg, basketball_data)], BASKETBALL_NOW)
    p = hub.roster()["players"][0]
    assert p["fpts"]["season_avg"] == 25 and p["fpts"]["l15_avg"] == 30
    assert "categories" not in hub.league_settings()
    out = hub.lineup_check()
    assert out.get("note") or out["value_metric"] == "15-day avg fpts"


def test_matchup_total_split_prefers_null_tagged():
    from espn_fantasy_hub.views import matchup_total_split
    player = {"stats": [
        {"id": "012023", "statSourceId": 0, "statSplitTypeId": 1, "scoringPeriodId": 0, "stats": {"0": 99}},
        {"id": "05null", "statSourceId": 0, "statSplitTypeId": 5, "scoringPeriodId": 0, "stats": {"0": 42}},
    ]}
    assert matchup_total_split(player)["stats"]["0"] == 42
    assert matchup_total_split({"stats": [{"id": "01null", "statSourceId": 0, "statSplitTypeId": 1, "scoringPeriodId": 0}]})


def test_tomorrow_wording_and_limits(basketball_data):
    hub, period = _in_season_hub(basketball_data, dt.date(2023, 1, 10))
    out = hub.lineup_check(days_ahead=1)
    assert all("today" not in i["problem"] for i in out["issues"])
    assert any("tomorrow" in i["problem"] for i in out["issues"] if "no game" in i["problem"]) or not any(
        "no game" in i["problem"] for i in out["issues"])
    s = hub.league_settings()
    assert s["waivers_and_free_agents"]["add_limit"] == "1 pickup(s) per day"
    assert out["pickups"]["limit"] == "1 per day"


def test_daily_pickup_options_play_that_day(basketball_data):
    hub, period = _in_season_hub(basketball_data, dt.date(2023, 1, 10))
    out = hub.lineup_check()
    with_options = 0
    for issue in out["issues"]:
        assert "_needs_pickup_slot" not in issue
        for o in issue.get("free_agent_options", []):
            assert o["game"] != "no game"
        with_options += bool(issue.get("free_agent_options"))
    assert with_options >= 1


def test_matchup_projection_categories(basketball_data):
    core = basketball_data["core"]
    core["status"]["currentMatchupPeriod"] = 10
    for m in core["schedule"]:
        if m["matchupPeriodId"] >= 10:
            m["winner"] = "UNDECIDED"
    for m in basketball_data["scoreboard_10"]["schedule"]:
        m["winner"] = "UNDECIDED"
    hub, period = _in_season_hub(basketball_data, dt.date(2023, 1, 10))
    out = hub.matchup_projection()
    assert out["remaining_games_counted"]["you"] > 0
    cats = out["categories"]
    assert len(cats) == 8
    for c in cats:
        assert c["projected_leader"] in ("you", "opp", "tied")
        if c["cat"] not in ("FG%", "FT%"):
            assert c["you_projected"] >= c["you_now"]  # counting stats only go up
        else:
            assert 0 <= c["you_projected"] <= 1
    w, l, t = map(int, out["projected_score"].split("-"))
    assert w + l + t == 8
    assert "assumptions" in out


def test_matchup_projection_points_variant(basketball_data):
    core = basketball_data["core"]
    core["settings"]["scoringSettings"]["scoringType"] = "H2H_POINTS"
    core["status"]["currentMatchupPeriod"] = 10
    for m in core["schedule"]:
        if m["matchupPeriodId"] >= 10:
            m["winner"] = "UNDECIDED"
    for t in core["teams"]:
        for e in t["roster"]["entries"]:
            for s in player_of(e).get("stats") or []:
                if s["statSourceId"] == 0 and s["statSplitTypeId"] == 0:
                    s["appliedAverage"] = 30.0
    hub, period = _in_season_hub(basketball_data, dt.date(2023, 1, 10))
    out = hub.matchup_projection()
    pts = out["points"]
    assert pts["you_projected"] >= pts["you_now"]
    assert out["projected_result"]
