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
    assert {w["result"] for w in out["your_matchups"]} <= {"W", "L", "T"}
