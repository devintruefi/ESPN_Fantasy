"""Complete weekly projection histories for football players, scored with each league's rules.

Why: a league's player cards only carry this week's projection, and box scores only carry
projections for players who were on a fantasy roster that week. Values built from those
gaps misled the model (a recent pickup looked better than he was). ESPN's public player
feed (no login) has every player's weekly projected *stats*; scoring them with the league's
own scoring items reproduces the league's projected points (validated within 0.05 points
for every player at every position in two live leagues, Nov 2026).

Pure functions here; the fetching lives in ``espn.py`` (``public_projections``).
"""

from __future__ import annotations

from .views import LeagueView, num

# The lineup slot whose point overrides apply to each position (ESPN keys overrides by slot).
POSITION_SLOT = {"QB": 0, "RB": 2, "WR": 4, "TE": 6, "D/ST": 16, "K": 17}


def scoring_index(view: LeagueView) -> dict[int, tuple[float, dict[str, float]]]:
    """{stat id: (points, {slot id: override points})} from the league's settings."""
    items = ((view.settings.get("scoringSettings") or {}).get("scoringItems")) or []
    out: dict[int, tuple[float, dict[str, float]]] = {}
    for it in items:
        sid = it.get("statId")
        if sid is None:
            continue
        overrides = {str(k): num(v) for k, v in (it.get("pointsOverrides") or {}).items()}
        out[int(sid)] = (num(it.get("points")), overrides)
    return out


def score_stats(stats: dict, index: dict[int, tuple[float, dict[str, float]]], position: str) -> float:
    """Fantasy points for one stat line under the league's scoring."""
    slot = str(POSITION_SLOT.get(position, -1))
    total = 0.0
    for k, v in (stats or {}).items():
        try:
            sid = int(k)
        except (TypeError, ValueError):
            continue
        rule = index.get(sid)
        if not rule:
            continue
        pts, overrides = rule
        total += num(v) * (overrides[slot] if slot in overrides else pts)
    return total


def weekly_points(view: LeagueView, raw: dict[int, dict[int, dict]], positions: dict[int, str]) -> dict[int, dict[int, float]]:
    """{player id: {week: projected points}} from raw projected stat lines."""
    index = scoring_index(view)
    out: dict[int, dict[int, float]] = {}
    for pid, weeks in raw.items():
        pos = positions.get(pid, "")
        out[pid] = {int(w): round(score_stats(stats, index, pos), 2) for w, stats in weeks.items()}
    return out


def parse_public_players(payload, season: int) -> dict[int, dict[int, dict]]:
    """ESPN public player feed -> {player id: {week: projected raw stats}}."""
    out: dict[int, dict[int, dict]] = {}
    for item in payload or []:
        p = item.get("player", item) if isinstance(item, dict) else {}
        pid = p.get("id")
        if pid is None:
            continue
        weeks = out.setdefault(int(pid), {})
        for s in p.get("stats") or []:
            if (s.get("statSourceId") == 1 and s.get("statSplitTypeId") == 1
                    and s.get("seasonId") == season and s.get("scoringPeriodId")):
                weeks[int(s["scoringPeriodId"])] = s.get("stats") or {}
    return out
