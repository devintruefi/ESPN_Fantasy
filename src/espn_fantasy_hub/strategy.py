"""Strategy analytics: power rankings, category report, waiver upgrades, trades.

All functions are pure: they work on a LeagueView (and raw pool data) so they
can be tested offline.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from .sports import OUT_STATUSES
from .views import (
    PCT_STATS,
    LeagueView,
    compact,
    find_split,
    player_of,
    ppe_of,
    rnd,
)

# --------------------------------------------------------------------------
# player value
# --------------------------------------------------------------------------


def season_value(view: LeagueView, wrap: dict) -> float | None:
    """One rest-of-season 'how good is he' number, sport/format aware.

    Football: fantasy points per game this season (ESPN's weekly projection
    when he has fewer than 2 games). Basketball points: fantasy points per
    game. Basketball categories: ESPN player rater (season).
    """
    p = player_of(wrap)
    if view.is_football:
        s = find_split(p, 0, 0, view.season)
        if s and s.get("appliedAverage") and (s.get("appliedTotal") or 0) / max(s["appliedAverage"], 0.01) >= 2:
            return s["appliedAverage"]
        proj = find_split(p, 1, 1, period=view.scoring_period)
        return proj.get("appliedTotal") if proj else (s or {}).get("appliedAverage")
    if not view.is_categories:
        s = find_split(p, 0, 0, view.season)
        return s.get("appliedAverage") if s else None
    r = (ppe_of(wrap).get("ratings") or {}).get("0") or {}
    return r.get("totalRating")


def recent_value(view: LeagueView, wrap: dict) -> float | None:
    """Recent form: football = this week's projection; basketball = last 15 days."""
    p = player_of(wrap)
    if view.is_football:
        proj = find_split(p, 1, 1, period=view.scoring_period)
        return proj.get("appliedTotal") if proj else None
    if not view.is_categories:
        s = find_split(p, 0, 2, view.season)
        return s.get("appliedAverage") if s else None
    r = (ppe_of(wrap).get("ratings") or {}).get("2") or {}
    return r.get("totalRating")


def value_labels(view: LeagueView) -> tuple[str, str]:
    if view.is_football:
        return "season fantasy pts/game", "this week's projection"
    if view.is_categories:
        return "ESPN player rater (season)", "player rater (last 15 days)"
    return "fantasy pts/game (season)", "fantasy pts/game (last 15 days)"


# --------------------------------------------------------------------------
# optimal lineup
# --------------------------------------------------------------------------


def optimal_lineup(view: LeagueView, wraps: list[dict], value: Callable[[dict], float | None]) -> tuple[float, list[tuple[str, str, float]]]:
    """Greedy best starting lineup: fill the most restrictive slots first."""
    sport = view.sport
    slots: list[int] = []
    for sid, count in view.slot_counts.items():
        if sport.is_starting_slot(sid):
            slots += [sid] * count
    pool = [
        w for w in wraps
        if (player_of(w).get("injuryStatus") or w.get("injuryStatus")) not in OUT_STATUSES
        and w.get("lineupSlotId") != sport.ir_slot
    ]

    def eligible_count(sid):
        return sum(1 for w in pool if sid in (player_of(w).get("eligibleSlots") or []))

    slots.sort(key=eligible_count)
    used: set = set()
    total = 0.0
    picks = []
    for sid in slots:
        best = None
        for w in pool:
            pid = player_of(w).get("id")
            if pid in used or sid not in (player_of(w).get("eligibleSlots") or []):
                continue
            v = value(w)
            if v is None:
                continue
            if best is None or v > best[1]:
                best = (w, v)
        if best:
            used.add(player_of(best[0]).get("id"))
            total += best[1]
            picks.append((sport.slot(sid), player_of(best[0]).get("fullName"), best[1]))
    return total, picks


# --------------------------------------------------------------------------
# power rankings
# --------------------------------------------------------------------------


def _completed_periods(view: LeagueView) -> dict[int, list[dict]]:
    by_period: dict[int, list[dict]] = {}
    for m in view.core.get("schedule") or []:
        if (m.get("winner") or "UNDECIDED") == "UNDECIDED":
            continue
        by_period.setdefault(m.get("matchupPeriodId"), []).append(m)
    return by_period


def _cat_values(side: dict) -> dict[str, float]:
    return {
        k: v.get("score")
        for k, v in ((side.get("cumulativeScore") or {}).get("scoreByStat") or {}).items()
        if isinstance(v, dict) and v.get("score") is not None
    }


def power_rankings(view: LeagueView) -> dict:
    periods = _completed_periods(view)
    teams = list(view.teams)
    n = len(teams)
    stats = {t: {"ap_w": 0.0, "ap_l": 0.0, "ap_t": 0.0, "scores": [], "weeks": 0} for t in teams}
    reverse = {str(c["id"]) for c in view.categories if c["lower_is_better"]}
    cat_ids = [str(c["id"]) for c in view.categories]

    for period in sorted(periods):
        entries = {}
        for m in periods[period]:
            for side in (m.get("home") or {}, m.get("away") or {}):
                if side.get("teamId"):
                    entries[side["teamId"]] = side
        if len(entries) < 2:
            continue
        for tid, side in entries.items():
            st = stats[tid]
            st["weeks"] += 1
            if view.is_categories:
                mine = _cat_values(side)
                for oid, other in entries.items():
                    if oid == tid:
                        continue
                    theirs = _cat_values(other)
                    won = lost = 0
                    for k in cat_ids:
                        if k not in mine or k not in theirs or mine[k] == theirs[k]:
                            continue
                        better = mine[k] < theirs[k] if k in reverse else mine[k] > theirs[k]
                        won += better
                        lost += not better
                    st["ap_w" if won > lost else "ap_l" if lost > won else "ap_t"] += 1
            else:
                score = side.get("totalPoints") or 0.0
                st["scores"].append(score)
                for oid, other in entries.items():
                    if oid == tid:
                        continue
                    o = other.get("totalPoints") or 0.0
                    st["ap_w" if score > o else "ap_l" if score < o else "ap_t"] += 1

    rows = []
    for tid in teams:
        st = stats[tid]
        games = st["ap_w"] + st["ap_l"] + st["ap_t"]
        ap_pct = (st["ap_w"] + st["ap_t"] / 2) / games if games else 0.0
        rec = view.record(tid)
        actual_w = rec.get("wins", 0) + rec.get("ties", 0) / 2
        played = rec.get("wins", 0) + rec.get("losses", 0) + rec.get("ties", 0)
        expected_w = ap_pct * played
        row: dict[str, Any] = {
            "team": view.team_label(tid),
            "record": view.record_str(tid),
            "all_play_record": f"{int(st['ap_w'])}-{int(st['ap_l'])}" + (f"-{int(st['ap_t'])}" if st["ap_t"] else ""),
            "all_play_pct": rnd(ap_pct, 3),
            "expected_wins": rnd(expected_w, 1),
            "luck_wins": rnd(actual_w - expected_w, 1),
        }
        if not view.is_categories and st["scores"]:
            scores = st["scores"]
            row["avg_pts"] = rnd(sum(scores) / len(scores), 1)
            row["last3_avg"] = rnd(sum(scores[-3:]) / len(scores[-3:]), 1)
            row["high"] = rnd(max(scores), 1)
            row["low"] = rnd(min(scores), 1)
        rows.append(row)

    if not view.is_categories and any("avg_pts" in r for r in rows):
        avgs = [r.get("avg_pts", 0) for r in rows]
        recents = [r.get("last3_avg", 0) for r in rows]
        lo_a, hi_a = min(avgs), max(avgs)
        lo_r, hi_r = min(recents), max(recents)
        for r in rows:
            na = (r.get("avg_pts", 0) - lo_a) / (hi_a - lo_a) if hi_a > lo_a else 0.5
            nr = (r.get("last3_avg", 0) - lo_r) / (hi_r - lo_r) if hi_r > lo_r else 0.5
            r["power_score"] = rnd(100 * (0.5 * r["all_play_pct"] + 0.3 * na + 0.2 * nr), 1)
    else:
        for r in rows:
            r["power_score"] = rnd(100 * r["all_play_pct"], 1)
    rows.sort(key=lambda r: r["power_score"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["power_rank"] = i
    return {
        "periods_counted": len(periods),
        "how_to_read": (
            "all_play = record if you had played every team every week (true strength). "
            "luck_wins = actual wins minus expected wins from all-play: positive means lucky. "
            + ("power_score = 50% all-play, 30% season scoring, 20% last-3 scoring." if not view.is_categories
               else "power_score = all-play category win rate.")
        ),
        "rankings": [compact(r) for r in rows],
    }


# --------------------------------------------------------------------------
# category report (category leagues)
# --------------------------------------------------------------------------


def category_report(view: LeagueView, team_id: int, matchup_raw: dict | None = None) -> dict:
    if not view.is_categories:
        return {"note": "This is a points league; category analysis doesn't apply. Try power_rankings."}
    cats = view.categories
    totals = {
        tid: {str(k): v for k, v in (t.get("valuesByStat") or {}).items()}
        for tid, t in view.teams.items()
    }
    games = {tid: (vals.get("42") or 0) for tid, vals in totals.items()}  # GP if ESPN provides it
    table = []
    for c in cats:
        k = str(c["id"])
        pct = c["name"] in PCT_STATS

        def metric(tid):
            v = totals[tid].get(k)
            if v is None:
                return None
            if pct or not games.get(tid):
                return v
            return v / games[tid]

        vals = {tid: metric(tid) for tid in view.teams if metric(tid) is not None}
        if team_id not in vals:
            continue
        ordered = sorted(vals, key=lambda t: vals[t], reverse=not c["lower_is_better"])
        rank = ordered.index(team_id) + 1
        table.append(
            {
                "cat": c["name"],
                "you": rnd(vals[team_id], 3 if pct else 1),
                "league_best": rnd(vals[ordered[0]], 3 if pct else 1),
                "rank": rank,
                "of": len(ordered),
                "lower_is_better": True if c["lower_is_better"] else None,
            }
        )
    table = [compact(r) for r in table]
    n = len(view.teams)
    third = max(1, n // 3)
    strengths = [r["cat"] for r in table if r["rank"] <= third]
    weaknesses = [r["cat"] for r in table if r["rank"] > n - third]
    by_rank = sorted(table, key=lambda r: r["rank"], reverse=True)
    out: dict[str, Any] = {
        "team": view.team_label(team_id),
        "basis": "season totals per game played" if any(games.values()) else "season totals",
        "categories": table,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "your_weakest_categories": [f"{r['cat']} (#{r['rank']})" for r in by_rank[:3]],
        "punt_candidates": weaknesses[:2],
        "how_to_read": f"rank 1 = best of {n}. Strengths = top third, weaknesses = bottom third.",
    }
    if matchup_raw:
        m = view.find_matchup(matchup_raw, view.current_matchup_period, team_id)
        if m:
            me, opp, _ = view._side(m, team_id)
            mine, theirs = _cat_values(me), _cat_values(opp)
            swing = []
            for c in cats:
                k = str(c["id"])
                if k not in mine or k not in theirs:
                    continue
                a, b = mine[k], theirs[k]
                denom = max(abs(a), abs(b), 1e-9)
                margin = (a - b) / denom * (-1 if c["lower_is_better"] else 1)
                status = "winning" if margin > 0 else "losing" if margin < 0 else "tied"
                swing.append({
                    "cat": c["name"],
                    "you": rnd(a, 3 if c["name"] in PCT_STATS else 1),
                    "opp": rnd(b, 3 if c["name"] in PCT_STATS else 1),
                    "status": status,
                    "margin_pct": rnd(abs(margin) * 100, 1),
                    "swingable": abs(margin) < 0.12,
                })
            out["this_matchup"] = swing
    return out


# --------------------------------------------------------------------------
# waiver upgrades
# --------------------------------------------------------------------------


def waiver_upgrades(view: LeagueView, team_id: int, pool: list[dict], top: int = 3) -> dict:
    mine = [e for e in view.roster_entries(team_id) if e.get("lineupSlotId") != view.sport.ir_slot]
    season_label, recent_label = value_labels(view)
    suggestions = []

    def describe(w):
        p = player_of(w)
        week_game = None
        if view.is_football:
            g = view.pro.game(p.get("proTeamId"), view.scoring_period)
            week_game = view.pro.matchup_label(g) if g else "BYE"
        return compact({
            "name": p.get("fullName"),
            "pos": view.sport.position(p),
            "team": view.sport.pro_team(p.get("proTeamId")),
            "injury": view._injury(w, p),
            "season": rnd(season_value(view, w), 2),
            "recent": rnd(recent_value(view, w), 2),
            "status": {"WAIVERS": "waivers", "FREEAGENT": "free agent"}.get(ppe_of(w).get("status")),
            "games_next_7": view.bball_games(p.get("proTeamId")).get("next_7_days") if not view.is_football else None,
            "this_week": week_game,
        })

    healthy_pool = [w for w in pool if view._injury(w, player_of(w)) not in OUT_STATUSES and not ppe_of(w).get("onTeamId")]
    if view.is_football:
        positions = sorted({view.sport.position(player_of(e)) for e in mine} | {"QB", "RB", "WR", "TE"})
        for pos in positions:
            my_pos = [e for e in mine if view.sport.position(player_of(e)) == pos]
            fa_pos = [w for w in healthy_pool if view.sport.position(player_of(w)) == pos]
            if not my_pos or not fa_pos:
                continue
            worst = min(my_pos, key=lambda e: season_value(view, e) or 0)
            wv = season_value(view, worst) or 0
            best = sorted(fa_pos, key=lambda w: season_value(view, w) or 0, reverse=True)[:top]
            better = [b for b in best if (season_value(view, b) or 0) > wv + 1.0]
            if better:
                suggestions.append({
                    "position": pos,
                    "your_weakest": describe(worst),
                    "better_options": [describe(b) for b in better],
                })
    else:
        ranked = sorted(mine, key=lambda e: season_value(view, e) if season_value(view, e) is not None else -99)
        weakest = ranked[:top]
        best = sorted(healthy_pool, key=lambda w: (recent_value(view, w) or -99) + (season_value(view, w) or -99), reverse=True)[: top * 3]
        for w in weakest:
            wv, wr = season_value(view, w) or -99, recent_value(view, w) or -99
            better = [b for b in best if (season_value(view, b) or -99) > wv and (recent_value(view, b) or -99) > wr][:top]
            if better:
                suggestions.append({"your_weakest": describe(w), "better_options": [describe(b) for b in better]})
    return {
        "team": view.team_label(team_id),
        "values": {"season": season_label, "recent": recent_label},
        "upgrades": suggestions,
        "note": None if suggestions else "No clear upgrades on the wire by these measures.",
    }


# --------------------------------------------------------------------------
# trade analysis
# --------------------------------------------------------------------------


def _bball_totals(view: LeagueView, wraps: Iterable[dict], top_n: int) -> dict[str, float]:
    """Per-game category production of the top-N players by value."""
    ranked = sorted(wraps, key=lambda w: season_value(view, w) or -99, reverse=True)[:top_n]
    sums: dict[str, float] = {}
    for w in ranked:
        s = find_split(player_of(w), 0, 0, view.season)
        avg = (s or {}).get("averageStats") or {}
        for k, v in avg.items():
            name = view.sport.stat(k)
            sums[name] = sums.get(name, 0.0) + (v or 0.0)
    out = {}
    for c in view.categories:
        name = c["name"]
        if name == "FG%" and sums.get("FGA"):
            out[name] = sums.get("FGM", 0) / sums["FGA"]
        elif name == "FT%" and sums.get("FTA"):
            out[name] = sums.get("FTM", 0) / sums["FTA"]
        elif name == "3PT%" and sums.get("3PA"):
            out[name] = sums.get("3PM", 0) / sums["3PA"]
        elif name in sums:
            out[name] = sums[name]
    return out


def trade_analysis(view: LeagueView, my_id: int, partner_id: int, give: list[dict], receive: list[dict]) -> dict:
    season_label, recent_label = value_labels(view)

    def line(w):
        p = player_of(w)
        return compact({
            "name": p.get("fullName"),
            "pos": view.sport.position(p),
            "team": view.sport.pro_team(p.get("proTeamId")),
            "injury": view._injury(w, p),
            "season_value": rnd(season_value(view, w), 2),
            "recent_value": rnd(recent_value(view, w), 2),
        })

    give_ids = {player_of(w).get("id") for w in give}
    recv_ids = {player_of(w).get("id") for w in receive}
    my_now = view.roster_entries(my_id)
    their_now = view.roster_entries(partner_id)
    my_after = [e for e in my_now if player_of(e).get("id") not in give_ids] + list(receive)
    their_after = [e for e in their_now if player_of(e).get("id") not in recv_ids] + list(give)

    def lineup_value(wraps):
        total, _ = optimal_lineup(view, wraps, lambda w: season_value(view, w))
        return total

    out: dict[str, Any] = {
        "you_give": [line(w) for w in give],
        "you_get": [line(w) for w in receive],
        "value_basis": season_label,
        "value_given": rnd(sum(season_value(view, w) or 0 for w in give), 2),
        "value_received": rnd(sum(season_value(view, w) or 0 for w in receive), 2),
        "your_starting_lineup_value": {"before": rnd(lineup_value(my_now), 1), "after": rnd(lineup_value(my_after), 1)},
        "their_starting_lineup_value": {"before": rnd(lineup_value(their_now), 1), "after": rnd(lineup_value(their_after), 1)},
        "partner": view.team_label(partner_id),
        "roster_spots": {"you": len(my_after) - len(my_now), "them": len(their_after) - len(their_now)},
    }
    if view.is_categories:
        starters = sum(c for s, c in view.slot_counts.items() if view.sport.is_starting_slot(s))
        before, after = _bball_totals(view, my_now, starters), _bball_totals(view, my_after, starters)
        out["your_category_change_per_game"] = {
            k: rnd(after.get(k, 0) - before.get(k, 0), 3 if k in PCT_STATS else 2) for k in before
        }
    d_me = out["your_starting_lineup_value"]["after"] - out["your_starting_lineup_value"]["before"]
    d_them = out["their_starting_lineup_value"]["after"] - out["their_starting_lineup_value"]["before"]
    out["summary"] = (
        f"Your best starting lineup {'improves' if d_me > 0 else 'gets worse' if d_me < 0 else 'is unchanged'} "
        f"({d_me:+.1f}); theirs {'improves' if d_them > 0 else 'gets worse' if d_them < 0 else 'is unchanged'} ({d_them:+.1f})."
    )
    return out
