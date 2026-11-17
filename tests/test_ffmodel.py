"""Football brain: season simulation, lineups and playbook on the saved public league."""

import time

import pytest

from espn_fantasy_hub import ffmodel
from espn_fantasy_hub.service import UserError


def test_season_outlook_shape_and_sanity(football_hub):
    t0 = time.time()
    out = football_hub.season_outlook()
    assert time.time() - t0 < 8  # must fit a serverless request
    you = out["your_outlook"]
    for k in ("playoffs_pct", "bye_pct", "title_pct", "reach_final_pct"):
        assert 0 <= you[k] <= 100
    assert you["title_pct"] <= you["reach_final_pct"] <= you["playoffs_pct"]
    # every remaining regular-season week (10-14) is listed, current week with the live score
    assert [w["week"] for w in out["your_weeks"]] == [10, 11, 12, 13, 14]
    assert "score_now" in out["your_weeks"][0]
    assert all(0 <= w["win_pct"] <= 100 for w in out["your_weeks"])
    # the league table: 12 teams, 6 playoff spots, 2 byes, one champion
    lg = out["all_teams"]
    assert len(lg) == 12
    assert sum(t["playoffs_pct"] for t in lg) == pytest.approx(600, abs=1)
    assert sum(t["bye_pct"] for t in lg) == pytest.approx(200, abs=1)
    assert sum(t["title_pct"] for t in lg) == pytest.approx(100, abs=1)
    # records needed: more wins never lowers the playoff chance much
    pcts = [r["playoff_pct"] for r in out["wins_needed"]]
    assert pcts == sorted(pcts) or max(a - b for a, b in zip(pcts, pcts[1:])) < 5
    assert out["your_best_lineup_next_week"]
    assert any("SD" in n for n in out["notes"])


def test_season_outlook_is_deterministic(football_hub):
    a = football_hub.season_outlook()["your_outlook"]
    b = football_hub.season_outlook()["your_outlook"]
    assert a == b


def test_best_lineup_is_legal(football_hub):
    h = football_hub._handles[0]
    view = football_hub.view(h)
    players = [ffmodel.make_player(view, e, 1, {}) for e in view.roster_entries(1)]
    total, lineup = ffmodel.best_lineup(view, players, 11)
    starters = sum(n for sid, n in view.slot_counts.items() if view.sport.is_starting_slot(sid))
    names = [n for _, n, _ in lineup]
    assert len(names) == len(set(names)) <= starters
    by_name = {p["name"]: p for p in players}
    slot_ids = {view.sport.slot(s): s for s in view.slot_counts}
    for slot, name, _ in lineup:
        assert slot_ids[slot] in by_name[name]["slots"]
    assert total == pytest.approx(sum(v for *_, v in lineup), abs=0.6)


def test_byes_and_injuries_zero_out_players(football_hub):
    h = football_hub._handles[0]
    view = football_hub.view(h)
    p = next(ffmodel.make_player(view, e, 1, {}) for e in view.roster_entries(1))
    bye = view.pro.bye_week.get(p["pro_id"])
    if bye and bye > view.scoring_period:
        assert ffmodel.availability(view, p, bye) == 0
    hurt = dict(p, status="INJURY_RESERVE")
    week = next(w for w in range(view.scoring_period, view.scoring_period + 3) if view.pro.game(p["pro_id"], w))
    assert ffmodel.availability(view, hurt, week) == 0
    assert ffmodel.week_value(view, hurt, week) == 0


def test_projection_history_and_noise(football_data, football_hub):
    h = football_hub._handles[0]
    view = football_hub.view(h)
    past = [football_data["scoreboard_prev"]]
    hist = ffmodel.projection_history(view, past)
    assert hist and all(9 in v for v in hist.values())
    sd, bias, n = ffmodel.score_noise(view, past)
    assert n >= 10 and 10 < sd < 35 and abs(bias) < 15


def test_football_playbook_is_filled(football_hub):
    text = football_hub.strategy_playbook()["playbook"]
    assert "{" not in text and "}" not in text
    assert "Head-to-head points" in text and "Playoff weeks: NFL weeks 15-17" in text
    assert "Kickers here average" in text


def test_season_outlook_after_regular_season(football_hub):
    h = football_hub._handles[0]
    core = h.client.d["core"]
    core["status"]["currentMatchupPeriod"] = 15
    out = football_hub.season_outlook()
    assert "regular season is over" in out["note"]


def test_basketball_outlook_after_regular_season_says_so(basketball_hub):
    out = basketball_hub.season_outlook()
    assert "regular season is over" in out["note"]


def test_unprojected_defense_gets_typical_value(football_hub, football_data):
    """A D/ST's season average is nearly noise: an unprojected defense with a big average must not be
    valued like one ESPN actually projects that high."""
    import copy

    h = football_hub._handles[0]
    view = football_hub.view(h)
    wrap = next(copy.deepcopy(e) for t in view.teams for e in view.roster_entries(t)
                if view.sport.position(e["playerPoolEntry"]["player"]) == "D/ST")
    past = [football_data["scoreboard_prev"]]

    def outlook_with(stats):
        w = copy.deepcopy(wrap["playerPoolEntry"])
        w["player"].update(id=-999, fullName="Test D/ST", stats=stats)
        return ffmodel.season_outlook(view, None, past, [w], 1, sims=200)["best_adds"]

    season = {"seasonId": view.season, "statSourceId": 0, "statSplitTypeId": 0, "scoringPeriodId": 0,
              "appliedTotal": 135.0, "appliedAverage": 15.0, "stats": {}}
    proj = {"seasonId": view.season, "statSourceId": 1, "statSplitTypeId": 1,
            "scoringPeriodId": view.scoring_period, "appliedTotal": 15.0, "stats": {}}
    projected = [a for a in outlook_with([season, proj]) if a["name"] == "Test D/ST"]
    unprojected = [a for a in outlook_with([season]) if a["name"] == "Test D/ST"]
    assert projected and projected[0]["value_per_game"] == 15.0
    assert not unprojected or unprojected[0]["value_per_game"] < 10


def test_stress_range_contains_headline_numbers(football_hub):
    you = football_hub.season_outlook()["your_outlook"]
    for k in ("playoffs", "title"):
        lo, hi = (float(x) for x in you["stress_test_range"][f"{k}_pct"].split("-"))
        assert lo <= you[f"{k}_pct"] <= hi
