"""Complete projection histories: scoring raw projected stats with the league's rules must
reproduce the league's own projected points."""

import pytest

from espn_fantasy_hub import ffmodel, projections
from espn_fantasy_hub.views import player_of


def test_scoring_reproduces_espn_projections(football_hub):
    h = football_hub._handles[0]
    view = football_hub.view(h)
    index = projections.scoring_index(view)
    checked, worst = 0, 0.0
    for tid in view.teams:
        for e in view.roster_entries(tid):
            p = player_of(e)
            pos = view.sport.position(p)
            for s in p.get("stats") or []:
                if s.get("statSourceId") == 1 and s.get("statSplitTypeId") == 1 and s.get("stats"):
                    mine = projections.score_stats(s["stats"], index, pos)
                    worst = max(worst, abs(mine - s["appliedTotal"]))
                    checked += 1
    assert checked > 100
    assert worst < 0.06, worst


def test_parse_public_players_keeps_only_this_seasons_weekly_projections():
    payload = [{"id": 7, "stats": [
        {"seasonId": 2026, "statSourceId": 1, "statSplitTypeId": 1, "scoringPeriodId": 9, "stats": {"53": 5}},
        {"seasonId": 2026, "statSourceId": 0, "statSplitTypeId": 1, "scoringPeriodId": 9, "stats": {"53": 7}},
        {"seasonId": 2025, "statSourceId": 1, "statSplitTypeId": 1, "scoringPeriodId": 9, "stats": {"53": 1}},
        {"seasonId": 2026, "statSourceId": 1, "statSplitTypeId": 0, "scoringPeriodId": 0, "stats": {"53": 80}},
    ]}]
    assert projections.parse_public_players(payload, 2026) == {7: {9: {"53": 5}}}


def test_world_uses_complete_projections(football_hub):
    h = football_hub._handles[0]
    view = football_hub.view(h)
    world = football_hub.football_world(h, view)
    assert world.full_history
    assert any(c[0] == "public_projections" for c in h.client.calls)
    # last week's projections now exist for players who weren't in a box score too
    with_two = sum(1 for p in world.players.values() if len([w for w, v in p["by_week"].items() if v > 0]) >= 2)
    assert with_two > 50


def test_world_falls_back_without_public_feed(football_data):
    import sys
    sys.path.insert(0, "tests")
    from conftest import FOOTBALL_NOW, make_hub
    from espn_fantasy_hub.config import LeagueConfig

    football_data["public_unavailable"] = True
    hub = make_hub([(LeagueConfig(sport="football", league_id=1086064, season=2026, team_id=1), football_data)], FOOTBALL_NOW)
    h = hub._handles[0]
    world = hub.football_world(h, hub.view(h))
    assert not world.full_history
    out = hub.season_outlook()
    assert "box-score history only" in out["notes"][0]


def test_point_value_modes():
    p = {"by_week": {11: 12.0}, "base": 10.0, "avg": 16.0, "pos": "WR", "games": 8}
    assert ffmodel.point_value(p, 11, "proj") == 12.0
    assert ffmodel.point_value(p, 12, "proj") == 10.0
    assert ffmodel.point_value(p, 12, "avg") == 16.0
    assert ffmodel.point_value(p, 12, "blend") == 13.0
    dst = dict(p, pos="D/ST")
    assert ffmodel.point_value(dst, 12, "avg") == 10.0   # defenses never use season averages
