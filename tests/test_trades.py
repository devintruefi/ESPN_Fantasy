"""Trade evaluator and trade finder: football (lineups + season sim) and basketball (categories)."""

import random

import pytest

from espn_fantasy_hub import catseason, trades
from espn_fantasy_hub.service import UserError


# ---- football -----------------------------------------------------------------------
@pytest.fixture
def world(football_hub):
    h = football_hub._handles[0]
    view = football_hub.view(h)
    return football_hub.football_world(h, view)


def test_fast_scorer_matches_world_lineup_score(world):
    """The search's incremental scorer must give exactly the model's lineup scores."""
    sc = trades._Scorer(world, list(world.view.teams))
    rng = random.Random(3)
    for _ in range(40):
        a, b = rng.sample(list(world.view.teams), 2)
        give = rng.sample(world.team(a), rng.randint(0, 2))
        get = rng.sample(world.team(b), rng.randint(0, 2))
        gi = frozenset(p["id"] for p in give)
        after = [p for p in trades.team_plus(world, a) if p["id"] not in gi] + get
        for mode in trades.MODES:
            assert sc.score(a, mode, gi, get) == pytest.approx(world.lineup_score(after, mode), abs=1e-9)


def test_evaluate_trade_is_consistent(world):
    me, partner = 1, 2
    star = max(world.team(me), key=lambda p: p["base"])
    ev = trades.evaluate_trade(world, me, partner, [star], [], sims=800)
    # giving away your best player for nothing can't help your lineup, and helps theirs
    assert ev["starting_lineup_change_per_week"]["you"]["by_projections"] < 0
    assert ev["starting_lineup_change_per_week"]["them"]["by_projections"] >= 0
    assert ev["your_odds"]["worst_change"] <= 0
    assert "Don't make it" in ev["verdict"] or "depends" in ev["verdict"]
    # "before" odds are the same draws every time (common random numbers)
    again = trades.evaluate_trade(world, me, partner, [star], [], sims=800)
    assert again["your_odds"] == ev["your_odds"]
    assert {r["test"] for r in ev["your_odds"]["by_test"]} == {s["label"] for s in trades.SCENARIOS}
    # roster check: they take one more player than they give
    active_them = len([p for p in world.team(partner) if not p["in_ir"]])
    assert ev["roster_check"]["them"]["after_trade"] == active_them + 1
    if active_them + 1 > ev["roster_check"]["them"]["roster_spots"]:
        assert ev["roster_check"]["them"]["must_drop"] == 1 and ev["roster_check"]["them"]["suggested_drops"]


def test_find_trades_only_starters_and_helps_both_views(football_hub, world):
    out = football_hub.find_trades(partner="Team 9", max_players=2)
    assert out["searched"]["complete"]
    sc = trades._Scorer(world, list(world.view.teams))
    me, partner = 1, world.view.find_team("Team 9")
    by_name = {p["name"]: p for p in world.players.values()}
    for r in out["best_trades"] + out["also_worth_a_look"]:
        give = [by_name[n] for n in r["you_give"]]
        get = [by_name[n] for n in r["you_get"]]
        gi, ri = frozenset(p["id"] for p in give), frozenset(p["id"] for p in get)
        for mode in trades.MODES:
            assert sc.score(me, mode, gi, get) > sc.score(me, mode) + trades.MIN_GAIN - 1e-9
        assert trades._all_start(sc, me, gi, get) and trades._all_start(sc, partner, ri, give)
    for r in out["best_trades"]:
        assert r["type"] in ("win-win", "helps you; costs them", "depends on assumptions")
        assert r["partner"].startswith("Team 9")


def test_find_trades_must_receive_and_errors(football_hub, world):
    target = max(world.team(9), key=lambda p: p["base"])
    out = football_hub.find_trades(must_receive=[target["name"]], max_players=2)
    assert all(target["name"] in r["you_get"] for r in out["best_trades"] + out["also_worth_a_look"])
    assert set(out["searched"]["partners"]) == {world.view.team_label(9)}
    with pytest.raises(UserError):
        football_hub.find_trades(partner="Team 1")
    other = max(world.team(2), key=lambda p: p["base"])
    with pytest.raises(UserError):
        football_hub.find_trades(must_receive=[target["name"], other["name"]])


def test_trade_analyzer_football_uses_season_odds(football_hub, world):
    mine = max(world.team(1), key=lambda p: p["base"])
    theirs = max(world.team(2), key=lambda p: p["base"])
    out = football_hub.trade_analyzer(give=[mine["name"]], receive=[theirs["name"]])
    assert out["partner"].startswith("Team 2")
    assert len(out["your_odds"]["by_test"]) == len(trades.SCENARIOS)
    assert "verdict" in out and "starting_lineup_change_per_week" in out


def test_trade_window_closed_after_deadline(football_hub, football_data):
    h = football_hub._handles[0]
    view = football_hub.view(h)
    assert trades.trade_window(view) is None
    view.settings["tradeSettings"]["deadlineDate"] = 1_700_000_000_000   # Nov 2023
    assert "deadline has passed" in trades.trade_window(view)


# ---- basketball ---------------------------------------------------------------------
@pytest.fixture
def season(basketball_hub):
    """The saved basketball league rewound to three matchups before its playoffs (mechanics only)."""
    h = basketball_hub._handles[0]
    view = basketball_hub.view(h)
    view.current_matchup_period = view.reg_season_periods - 3
    view.season_over = False
    pool = h.client.player_pool(view.scoring_period, limit=150).get("players") or []
    return catseason.CategorySeason(view, pool, None)


def test_category_season_sums_and_determinism(season):
    out = catseason.season_outlook(season, 1, sims=1500)
    lg = out["all_teams"]
    n = season.n_playoff
    assert sum(t["playoffs_pct"] for t in lg) == pytest.approx(100 * n, abs=1)
    assert sum(t["title_pct"] for t in lg) == pytest.approx(100, abs=1)
    you = out["your_outlook"]
    assert you["title_pct"] <= you["reach_final_pct"] <= you["playoffs_pct"]
    assert len(out["stress_tests"]) == len(catseason.SCENARIOS)
    # each-category league: a matchup gives one result per category
    assert season.each
    assert all("expected_category_share_pct" in w for w in out["your_matchups"])
    assert catseason.season_outlook(season, 1, sims=1500)["your_outlook"] == you


def test_p_majority():
    assert catseason.p_majority([0.5] * 9) == pytest.approx(0.5)
    assert catseason.p_majority([1.0] * 5 + [0.0] * 4) == pytest.approx(1.0)
    assert catseason.p_majority([0.5, 0.5]) == pytest.approx(0.5)   # 1-1 ties count half


def test_fit_roster_keeps_full_size(season):
    r = season.roster(1)
    assert len([p for p in season.fit_roster(r) if p["slot"] != season.view.sport.ir_slot]) == season.limit
    fewer = r[:-2]
    assert len([p for p in season.fit_roster(fewer) if p["slot"] != season.view.sport.ir_slot]) == season.limit


def test_category_trade_eval_and_search(season):
    me = 1
    star = max(season.roster(me), key=season.m.value)
    ev = catseason.evaluate_trade(season, me, 2, [star["id"]], [], sims=600)
    assert ev["weekly_win_pct_change_full_strength"]["season"]["you"] < 0
    assert ev["your_odds"]["worst_change"] <= 0.5
    with pytest.raises(ValueError):
        catseason.evaluate_trade(season, me, 2, [season.roster(3)[0]["id"]], [], sims=100)
    out = catseason.find_trades(season, me, [2, 3], max_size=1, budget_s=10, top=2, sims=400)
    assert out["searched"]["complete"]
    for r in out["best_trades"]:
        assert r["type"] != "hurts you"
        assert len(r["you_give"]) == len(r["you_get"]) == 1


def test_lineup_by_week_explains_the_lineup_change(world):
    me, partner = 1, 2
    give = [max(world.team(me), key=lambda p: p["base"])]
    get = [max(world.team(partner), key=lambda p: p["base"])]
    ev = trades.evaluate_trade(world, me, partner, give, get, sims=300)
    rows = ev["your_lineup_by_week"]
    assert [r["week"] for r in rows] == world.reg_weeks + world.po_weeks
    # the weekly totals reproduce the summary number (60% regular season, 40% playoffs)
    reg = [r["after"] - r["before"] for r in rows if not r.get("playoffs")]
    po = [r["after"] - r["before"] for r in rows if r.get("playoffs")]
    summary = 0.6 * sum(reg) / len(reg) + 0.4 * sum(po) / len(po)
    assert summary == pytest.approx(ev["starting_lineup_change_per_week"]["you"]["by_projections"], abs=0.15)
    # whoever is given away never appears as starting afterwards
    assert all(give[0]["name"] not in " ".join(r.get("starts", [])) for r in rows)


def test_a_hole_is_filled_from_free_agents(world):
    """Trading away your only D/ST costs the gap to the best free-agent D/ST, not a whole D/ST."""
    me, partner = 1, 2
    dsts = [p for p in world.team(me) if p["pos"] == "D/ST"]
    ev = trades.evaluate_trade(world, me, partner, dsts, [], sims=200)
    if len(dsts) == len([p for p in world.team(me) if p["pos"] == "D/ST"]):
        fa = [r for r in trades.replacements(world) if r["pos"] == "D/ST"]
        assert fa and fa[0]["name"].endswith("(free agent)")
        starts = " ".join(" ".join(r.get("starts", [])) for r in ev["your_lineup_by_week"])
        assert "(free agent)" in starts
        # the lineup loses at most the gap between the D/STs, never the D/ST's whole value
        gap = max(d["base"] for d in dsts) - fa[0]["base"]
        assert ev["starting_lineup_change_per_week"]["you"]["by_projections"] >= -max(gap, 0) - 3
