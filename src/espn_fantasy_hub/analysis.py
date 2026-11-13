"""Lineup check: catch starters who won't play and better options on the bench."""

from __future__ import annotations

from typing import Any

from .sports import OUT_STATUSES, RISK_STATUSES
from .views import (
    LeagueView,
    compact,
    find_split,
    fmt_time,
    player_of,
    ppe_of,
    rnd,
)

# Minimum projected gain before we suggest a bench/starter swap that isn't
# fixing a problem (avoid noise from tiny projection differences).
FOOTBALL_MIN_GAIN = 2.0
BASKETBALL_POINTS_MIN_GAIN = 3.0
BASKETBALL_RATER_MIN_GAIN = 1.0


def _value(view: LeagueView, wrap: dict, period: int) -> tuple[float | None, str]:
    p = player_of(wrap)
    ppe = ppe_of(wrap)
    if view.is_football:
        proj = find_split(p, 1, 1, period=period)
        return (proj.get("appliedTotal") if proj else None), "projected pts"
    if not view.is_categories:
        for split_id, label in ((2, "15-day avg fpts"), (0, "season avg fpts")):
            s = find_split(p, 0, split_id, view.season)
            if s and s.get("appliedAverage"):
                return s["appliedAverage"], label
        return None, "avg fpts"
    ratings = ppe.get("ratings") or {}
    for key, label in (("2", "15-day player rater"), ("0", "season player rater")):
        r = ratings.get(key) or {}
        if r.get("totalRating") is not None:
            return r["totalRating"], label
    return None, "player rater"


def _player_state(view: LeagueView, entry: dict, period: int) -> dict:
    p = player_of(entry)
    ppe = ppe_of(entry)
    game = view.pro.game(p.get("proTeamId"), period)
    started = bool(game and game["start"] and game["start"] <= view.pro.now)
    value, value_label = _value(view, entry, period)
    if game is None:
        game_label = "BYE" if (view.is_football and view.pro.bye_week.get(p.get("proTeamId")) == period) else "no game"
    else:
        game_label = f"{view.pro.matchup_label(game)} {fmt_time(game['start']) or ''}".strip()
    return {
        "id": p.get("id"),
        "name": p.get("fullName"),
        "slot_id": entry.get("lineupSlotId"),
        "slot": view.sport.slot(entry.get("lineupSlotId")),
        "eligible": set(p.get("eligibleSlots") or []),
        "injury": view._injury(entry, p),
        "has_game": game is not None,
        "game": game_label,
        "start": game["start"] if game else None,
        "locked": bool(ppe.get("lineupLocked")) or started,
        "value": value,
        "value_label": value_label,
    }


def _brief(s: dict) -> dict:
    return compact(
        {
            "slot": s["slot"],
            "name": s["name"],
            "injury": s["injury"],
            "game": s["game"],
            "value": rnd(s["value"], 2 if s["value_label"].endswith("rater") else 1),
            "locked": True if s["locked"] else None,
        }
    )


def lineup_check(view: LeagueView, team_id: int, entries: list[dict], period: int) -> dict:
    sport = view.sport
    states = [_player_state(view, e, period) for e in entries]
    starters = [s for s in states if sport.is_starting_slot(s["slot_id"])]
    bench = [s for s in states if s["slot_id"] == sport.bench_slot]
    ir = [s for s in states if s["slot_id"] == sport.ir_slot]

    issues: list[dict[str, Any]] = []
    moves: list[dict[str, Any]] = []
    used_bench: set = set()

    def can_fill(candidate: dict, slot_id) -> bool:
        return (
            candidate["id"] not in used_bench
            and not candidate["locked"]
            and candidate["has_game"]
            and candidate["injury"] not in OUT_STATUSES
            and candidate["injury"] != "DOUBTFUL"
            and slot_id in candidate["eligible"]
        )

    def best_bench(slot_id) -> dict | None:
        options = [b for b in bench if can_fill(b, slot_id)]
        if not options:
            return None
        return max(options, key=lambda b: (b["value"] is not None, b["value"] or 0, b["injury"] is None))

    def describe(s: dict) -> str:
        if s["value"] is None:
            return s["name"]
        digits = 2 if s["value_label"].endswith("rater") else 1
        return f"{s['name']} ({s['value_label']} {rnd(s['value'], digits)})"

    # 1. Starters who won't (or might not) play.
    problems = []
    for s in starters:
        if s["injury"] in OUT_STATUSES:
            problems.append((0, s, f"{s['name']} is {s['injury']}"))
        elif not s["has_game"]:
            when = "this week (BYE)" if s["game"] == "BYE" else ("this week" if view.is_football else "today")
            problems.append((0, s, f"{s['name']} has no game {when}"))
        elif s["injury"] == "DOUBTFUL":
            problems.append((1, s, f"{s['name']} is DOUBTFUL"))
        elif s["injury"] in RISK_STATUSES:
            problems.append((2, s, f"{s['name']} is {s['injury']}"))
    for severity, s, text in sorted(problems, key=lambda x: x[0]):
        level = ("critical", "warning", "watch")[severity]
        issue = {"level": level, "slot": s["slot"], "problem": text}
        if s["locked"]:
            issue["note"] = "Already locked (game started) — can't be changed now."
        elif severity == 2:
            when = fmt_time(s["start"]) if s["start"] else None
            issue["note"] = f"Check news before lock{(' at ' + when) if when else ''}."
            alt = best_bench(s["slot_id"])
            if alt:
                issue["backup_option"] = describe(alt)
        else:
            alt = best_bench(s["slot_id"])
            if alt:
                used_bench.add(alt["id"])
                issue["fix"] = f"Start {describe(alt)} instead"
                moves.append({"start": alt["name"], "bench": s["name"], "slot": s["slot"], "reason": text})
            else:
                issue["fix"] = "No eligible healthy bench player with a game — consider a free agent pickup."
        issues.append(issue)

    # 2. Empty starting slots.
    filled: dict[int, int] = {}
    for s in starters:
        filled[s["slot_id"]] = filled.get(s["slot_id"], 0) + 1
    for slot_id, count in view.slot_counts.items():
        if not sport.is_starting_slot(slot_id):
            continue
        for _ in range(count - filled.get(slot_id, 0)):
            issue = {"level": "critical", "slot": sport.slot(slot_id), "problem": f"Empty {sport.slot(slot_id)} slot"}
            alt = best_bench(slot_id)
            if alt:
                used_bench.add(alt["id"])
                issue["fix"] = f"Start {describe(alt)}"
                moves.append({"start": alt["name"], "slot": sport.slot(slot_id), "reason": "empty slot"})
            else:
                issue["fix"] = "Nobody eligible on the bench — pick up a free agent."
            issues.append(issue)

    # 3. Upgrades: healthy bench players projected well above a starter.
    if view.is_football:
        min_gain = FOOTBALL_MIN_GAIN
    elif view.is_categories:
        min_gain = BASKETBALL_RATER_MIN_GAIN
    else:
        min_gain = BASKETBALL_POINTS_MIN_GAIN
    problem_ids = {s["id"] for _, s, _ in problems}
    for s in sorted(starters, key=lambda x: (x["value"] is None, x["value"] or 0)):
        if s["locked"] or s["id"] in problem_ids or s["value"] is None:
            continue
        alt = best_bench(s["slot_id"])
        if alt and alt["value"] is not None and alt["value"] - s["value"] >= min_gain:
            used_bench.add(alt["id"])
            moves.append(
                {
                    "start": alt["name"],
                    "bench": s["name"],
                    "slot": s["slot"],
                    "reason": f"{describe(alt)} vs {describe(s)}",
                }
            )
            issues.append(
                {
                    "level": "upgrade",
                    "slot": s["slot"],
                    "problem": f"{alt['name']} on your bench looks better than {s['name']}",
                    "fix": f"Start {alt['name']} over {s['name']}",
                }
            )

    # 4. IR housekeeping.
    for s in ir:
        if s["injury"] is None:
            issues.append(
                {
                    "level": "warning",
                    "slot": s["slot"],
                    "problem": f"{s['name']} is in an IR slot but is no longer listed as injured",
                    "fix": "Move him off IR — ESPN blocks adds and trades while an ineligible player sits in IR.",
                }
            )

    # 5. Basketball: bench players with games while starters sit idle.
    idle_bench_with_games = []
    if not view.is_football:
        idle_bench_with_games = [
            b["name"] for b in bench
            if b["has_game"] and b["injury"] not in OUT_STATUSES and b["id"] not in used_bench
        ]

    order = {"critical": 0, "warning": 1, "watch": 2, "upgrade": 3}
    issues.sort(key=lambda i: order.get(i["level"], 9))
    out = {
        "team": view.team_label(team_id),
        "checking": (
            f"Week {period}" if view.is_football
            else f"{view.pro.period_date.get(period, '')} (scoring period {period})".strip()
        ),
        "lineup_ok": not any(i["level"] in ("critical", "warning") for i in issues),
        "issues": issues,
        "suggested_moves": moves,
        "value_metric": (starters or bench or [{"value_label": None}])[0]["value_label"],
        "starters": [_brief(s) for s in starters],
        "bench": [_brief(s) for s in bench],
    }
    if ir:
        out["ir"] = [_brief(s) for s in ir]
    if idle_bench_with_games:
        out["bench_players_with_games_not_starting"] = idle_bench_with_games
    # Keep these keys even when empty so the shape is always the same.
    return compact(out) | {"lineup_ok": out["lineup_ok"], "issues": issues, "suggested_moves": moves}
