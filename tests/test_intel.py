"""League memory (league_intel) and track record."""

from espn_fantasy_hub import intel


def test_league_intel_football(football_hub, football_data):
    out = football_hub.league_intel()
    rows = out["managers"]
    assert len(rows) == len(football_data["core"]["teams"])
    # sorted by playoff odds, situations follow the odds
    pcts = [r.get("playoffs_pct", 0) for r in rows]
    assert pcts == sorted(pcts, reverse=True)
    for r in rows:
        if "situation" in r:
            p = r["playoffs_pct"] / 100
            assert r["situation"] == ("contender" if p >= 0.75 else "bubble" if p >= 0.25 else "long shot")
        assert set(r["position_rank"]) == {"QB", "RB", "WR", "TE", "D/ST"}
        assert all(1 <= v <= len(rows) for v in r["position_rank"].values())
    # the saved league has a team with three players on the trade block
    blocks = [r["on_trade_block"] for r in rows if r.get("on_trade_block")]
    assert any(len(b) == 3 and all(not n.startswith("player ") for n in b) for b in blocks)
    assert len(out["trade_targets"] or []) <= 5
    assert out["covering"] == "weeks 1-10"


def test_league_intel_basketball_categories(basketball_hub):
    out = basketball_hub.league_intel()
    me = next(r for r in out["managers"] if r["team"].endswith("(you)"))
    assert len(me["needs"]) == 3 and len(me["strengths"]) == 3
    assert set(me["needs"]).isdisjoint(me["strengths"])
    assert all(0 <= v <= 100 for v in me["category_odds"].values())
    assert "playoffs_pct" not in me   # regular season is over in the saved league: no odds


def test_faab_summary():
    bids = [{"period": 1, "team": "A", "player": "X", "bid": b} for b in (0, 0, 1, 3, 5, 8, 12, 40)]
    s = intel.faab_summary(bids, 100)
    assert s["winning_claims"] == 8 and s["won_for_0_or_1"] == 3
    assert s["median_winning_bid"] in (5, 3)
    assert s["biggest"][0].startswith("X $40")


def test_track_record_football(football_hub):
    out = football_hub.track_record()
    acc = out["win_odds_accuracy"]
    assert acc["matchups_checked"] >= 1
    assert 0 <= acc["brier_score"] <= 1
    week = out["your_lineups"][0]
    pts = float(week["score"].split("-")[0])
    # the hindsight-best lineup can never score less than what was played or the projection lineup
    assert week["best_in_hindsight"] >= pts - 0.05
    assert week["best_in_hindsight"] >= week["best_by_projection"] - 0.05
    assert week["left_on_bench"] >= -0.05


def test_track_record_categories(basketball_hub):
    out = basketball_hub.track_record()
    rates = out["your_category_win_rate_pct"]
    assert rates and all(0 <= v <= 100 for v in rates.values())
    assert set(out["your_rank_by_category"]) == set(rates)
    assert all(1 <= r <= 10 for r in out["your_rank_by_category"].values())
    assert {w["result"] for w in out["your_matchups"]} <= {"W", "L", "T"}


def test_trade_history_reads_players_from_the_proposal(football_hub, football_data):
    """ESPN keeps an accepted trade's players on the related proposal, not on the acceptance."""
    import copy
    view = football_hub.view(football_hub._handles[0])
    accept = {"id": "a1", "type": "TRADE_ACCEPT", "status": "EXECUTED", "relatedTransactionId": "p1", "processDate": 1,
              "scoringPeriodId": 4, "items": [{"type": "DROP", "playerId": 1, "fromTeamId": 2, "toTeamId": 0}]}
    proposal = {"id": "p1", "type": "TRADE_PROPOSAL", "status": "ACCEPTED", "scoringPeriodId": 4, "items": [
        {"type": "TRADE", "playerId": 10, "fromTeamId": 2, "toTeamId": 3},
        {"type": "TRADE", "playerId": 11, "fromTeamId": 3, "toTeamId": 2}]}
    out = intel.league_intel(view, 1, [{"transactions": [accept, proposal]}], {10: "Player Ten", 11: "Player Eleven"})
    gets = next(t["gets"] for t in out["trades_this_season"] if "Player Ten" in str(t["gets"]))
    assert gets == {view.team_label(3): ["Player Ten"], view.team_label(2): ["Player Eleven"]}
    row = next(r for r in out["managers"] if r["team"] == view.team_label(2))
    assert row["traded_with"] == {view.team_label(3): 1}


def test_trades_rebuilt_from_rosters(football_hub):
    """The saved league has three trades visible on rosters (shared acquisition stamps)."""
    out = football_hub.league_intel()
    trades = out["trades_this_season"]
    assert isinstance(trades, list) and len(trades) >= 3
    golden = next(t for t in trades if any("Matthew Golden" in v for v in t["gets"].values()))
    assert len(golden["gets"]) == 2   # both sides of the deal
    me = next(r for r in out["managers"] if r["team"].endswith("(you)"))
    assert me["traded_with"]
