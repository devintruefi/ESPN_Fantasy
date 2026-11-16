from espn_fantasy_hub.service import UserError

import pytest


def test_power_rankings_football(football_hub):
    out = football_hub.power_rankings()
    rows = out["rankings"]
    assert len(rows) == 12 and out["periods_counted"] == 9
    assert [r["power_rank"] for r in rows] == list(range(1, 13))
    for r in rows:
        w, l, *t = map(int, r["all_play_record"].split("-"))
        assert w + l + sum(t) == 9 * 11  # every team vs every other team, every week
        assert r["avg_pts"] > 0
    assert abs(sum(r["luck_wins"] for r in rows)) < 1.5  # luck roughly cancels out


def test_power_rankings_categories(basketball_hub):
    rows = basketball_hub.power_rankings()["rankings"]
    assert len(rows) == 10
    assert all(0 <= r["all_play_pct"] <= 1 for r in rows)
    assert "avg_pts" not in rows[0]


def test_category_report(basketball_hub):
    out = basketball_hub.category_report()
    assert len(out["categories"]) == 8
    assert all(1 <= c["rank"] <= 10 for c in out["categories"])
    assert set(out["strengths"]).isdisjoint(out["weaknesses"])


def test_category_report_points_league(football_hub):
    assert "note" in football_hub.category_report()


def test_waiver_upgrades(football_hub, basketball_hub):
    out = football_hub.waiver_upgrades()
    for s in out["upgrades"]:
        assert s["better_options"]
        for b in s["better_options"]:
            assert b["season"] > s["your_weakest"]["season"]
    bb = basketball_hub.waiver_upgrades()
    assert "values" in bb


def test_trade_analyzer(football_hub, football_data):
    partner = next(t for t in football_data["core"]["teams"] if t["id"] == 2)
    their_player = partner["roster"]["entries"][0]["playerPoolEntry"]["player"]["fullName"]
    out = football_hub.trade_analyzer(give=["Tee Higgins"], receive=[their_player])
    assert out["partner"].startswith("Team 2")
    assert out["you_give"][0]["name"] == "Tee Higgins"
    assert out["roster_check"]["you"]["after_trade"] <= out["roster_check"]["you"]["roster_spots"]
    assert "by_projections" in out["starting_lineup_change_per_week"]["you"]
    assert out["verdict"]
    with pytest.raises(UserError):
        football_hub.trade_analyzer(give=["Not A Real Guy"], receive=[their_player])


def test_trade_analyzer_categories(basketball_hub, basketball_data):
    partner = next(t for t in basketball_data["core"]["teams"] if t["id"] == 2)
    name = partner["roster"]["entries"][0]["playerPoolEntry"]["player"]["fullName"]
    out = basketball_hub.trade_analyzer(give=["James Harden"], receive=[name])
    assert set(out["your_category_change_per_game"]) <= {"FT%", "REB", "PTS", "BLK", "3PM", "STL", "AST", "FG%"}


def test_text_valued_stats_dont_crash():
    # ESPN sometimes sends stats like minutes as "34:12" strings.
    from espn_fantasy_hub.strategy import _period_raw, _cat_values
    side = {
        "rosterForMatchupPeriod": {"entries": [
            {"playerPoolEntry": {"player": {"stats": [
                {"id": "05null", "statSourceId": 0, "statSplitTypeId": 5, "scoringPeriodId": 0,
                 "stats": {"0": 20.0, "40": "34:12", "13": 8}}]}}},
        ]},
        "cumulativeScore": {"scoreByStat": {"0": {"score": "20"}, "40": {"score": "34:12"}}},
    }
    assert _period_raw(side) == {"0": 20.0, "40": 0.0, "13": 8.0}
    assert _cat_values(side) == {"0": 20.0}
