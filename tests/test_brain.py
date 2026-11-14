import datetime as dt

import pytest

from espn_fantasy_hub import catmodel
from espn_fantasy_hub.service import UserError

from conftest import fixture_data
from test_basketball import _in_season_hub


def test_calendar_merges_all_star_break(basketball_hub):
    view = basketball_hub.view(basketball_hub.handles()[0])
    cal = catmodel.nba_calendar(view)
    # 2022-23: 25 calendar weeks; ESPN numbers 24 units (All-Star fortnight merged),
    # matching the league's matchupPeriods mapping that runs to unit 24.
    assert len(cal) == 24
    long_units = [u for u in cal if (u["end"] - u["start"]).days == 13]
    assert len(long_units) == 1 and long_units[0]["start"].month == 2
    assert max(max(v) for v in view.matchup_periods.values()) == 24


def test_category_strategy(basketball_data):
    hub, _ = _in_season_hub(basketball_data, dt.date(2023, 1, 10))
    out = hub.category_strategy()
    assert out["to_win_a_week"] == "every category counts"  # fixture league is each-category
    assert len(out["your_category_odds_now"]) == 8
    roles = [p["role"] for p in out["your_players"]]
    assert roles.count("rotation") == 2 and "core" in roles
    assert out["rotation_spots"] and all(p["replaces"] in out["rotation_spots"] for p in out["best_pickups_for_you"])
    assert len(out["model_strength"]) == 10
    pcts = [t["weekly_win_pct"] for t in out["model_strength"]]
    assert pcts == sorted(pcts, reverse=True)


def test_most_categories_majority_rule(basketball_data):
    basketball_data["core"]["settings"]["scoringSettings"]["scoringType"] = "H2H_MOST_CATEGORIES"
    hub, _ = _in_season_hub(basketball_data, dt.date(2023, 1, 10))
    out = hub.category_strategy()
    assert out["to_win_a_week"] == "win 5 of 8 categories"
    m = catmodel.CategoryModel(hub.view(hub.handles()[0]), [])
    assert m.p_win({c: 1.0 for c in m.cats}) == pytest.approx(1.0)
    assert m.p_win({c: 0.5 for c in m.cats}) == pytest.approx(0.5)


def test_player_values_punt(basketball_hub):
    allv = basketball_hub.player_values(limit=20)
    punt = basketball_hub.player_values(punt=["FT%", "ast", "TO"], limit=20)
    assert punt["punting"] == ["FT%", "AST"]
    assert punt["ignored_not_league_categories"] == ["TO"]  # this test league has no TO category
    assert all("FT%" not in p["by_category"] and "AST" not in p["by_category"] for p in punt["players"])
    vals = [p["value"] for p in allv["players"]]
    assert vals == sorted(vals, reverse=True)
    free = basketball_hub.player_values(who="free", limit=10)
    assert all(p["fantasy_team"] in ("free agent", "on waivers") for p in free["players"])


def test_schedule_planner_basketball(basketball_data):
    hub, _ = _in_season_hub(basketball_data, dt.date(2023, 1, 10))
    view = hub.view(hub.handles()[0])
    view.core["status"]["currentMatchupPeriod"] = 12
    hub.handles()[0].client.d["core"]["status"]["currentMatchupPeriod"] = 12
    out = hub.schedule_planner()
    assert out["matchups"][0]["matchup"] == 12
    assert any(m.get("playoffs") for m in out["matchups"])
    assert out["best_nba_schedules_for_your_playoffs"]
    assert out["next_days"] and all(d["open_starting_slots"] >= 0 for d in out["next_days"])


def test_schedule_planner_football(football_hub):
    out = football_hub.schedule_planner()
    assert out["playoff_weeks"] == [15, 16, 17]
    assert out["weeks"][0]["week"] == 10


def test_playbook_filled(basketball_hub, football_hub):
    pb = basketball_hub.strategy_playbook()["playbook"]
    assert "{" not in pb and "Head-to-head EACH category" in pb and "Pickup limit" in pb
    assert "Playoff weeks:" in pb


def test_category_tools_refuse_points_leagues(football_hub):
    with pytest.raises(UserError, match="category leagues"):
        football_hub.category_strategy()
