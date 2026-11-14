"""Category-league model: player values (z / G-scores), weekly matchup win
probabilities, team fit of free agents, and the basketball season calendar.

Method (see the research report in the README's strategy section):
- Per-game production; FG%/FT%/3PT% use volume-weighted impact (makes above the
  league-average rate), turnovers count against.
- G-score (Rosenof 2023, arXiv 2307.02188) = z-score shrunk by each category's
  week-to-week noise. Weights below were calibrated in Nov 2026 from 248 NBA
  players' last-10-game logs: noisy categories (FG%, STL) count less.
- Weekly team totals ~ normal with per-game variance = PRIOR_RATIO * (|mean| + .25);
  category probabilities are tempered by LAMBDA (fit to real weekly results), and
  a most-categories matchup is won by taking a majority of categories.
Pure functions only: no network.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics as st
from collections import defaultdict
from typing import Any

from .sports import OUT_STATUSES
from .views import LeagueView, compact, find_split, num, player_of, ppe_of, rnd

G_WEEK = 3.4  # average games per player per week
LAMBDA = 1.7  # probability temperature fit to weekly category results
G_WEIGHT = {"FG%": 0.648, "FT%": 0.702, "3PT%": 0.70, "3PM": 0.834, "REB": 0.876, "AST": 0.894,
            "STL": 0.661, "BLK": 0.764, "TO": 0.796, "PTS": 0.875}
PRIOR_RATIO = {"FG%": 2.805, "FT%": 0.937, "3PT%": 0.9, "3PM": 0.843, "REB": 1.069, "AST": 0.974,
               "STL": 0.75, "BLK": 0.744, "TO": 0.777, "PTS": 2.538}
RATIO_PARTS = {"FG%": ("FGM", "FGA"), "FT%": ("FTM", "FTA"), "3PT%": ("3PM", "3PA")}


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


class CategoryModel:
    def __init__(self, view: LeagueView, pool_wraps: list[dict]):
        if not view.is_categories:
            raise ValueError("category model needs a category league")
        self.view = view
        self.cats = [c["name"] for c in view.categories]
        self.low = {c["name"] for c in view.categories if c["lower_is_better"]}
        self.sign = {c: (-1 if c in self.low else 1) for c in self.cats}
        self.players: list[dict] = []
        seen = set()
        for tid in view.teams:
            for e in view.roster_entries(tid):
                self._add(e, tid, e.get("lineupSlotId"), seen)
        for w in pool_wraps:
            self._add(w, ppe_of(w).get("onTeamId") or None, None, seen)
        self._normalize()

    # ---- players -------------------------------------------------------------
    def _avgs(self, p: dict, split: int) -> dict[str, float]:
        s = find_split(p, 0, split, self.view.season) or {}
        src = s.get("averageStats") or {}
        return {self.view.sport.stat(k): (num(v) or 0.0) for k, v in src.items()}

    def _add(self, wrap: dict, owner, slot, seen: set) -> None:
        p = player_of(wrap)
        pid = p.get("id")
        if not pid or pid in seen:
            return
        avg = self._avgs(p, 0)
        if not avg.get("GP"):
            return
        seen.add(pid)
        ratings = ppe_of(wrap).get("ratings") or {}
        self.players.append({
            "id": pid, "name": p.get("fullName"), "owner": owner, "slot": slot,
            "injury": self.view._injury(wrap, p), "pro": p.get("proTeamId"),
            "eligible": self.view.sport.eligible_positions(p.get("eligibleSlots"), allowed=self.view.slot_counts),
            "avg": avg, "l15": self._avgs(p, 2) or avg,
            "gp": avg.get("GP", 0), "min": avg.get("MIN", 0),
            "rater": (ratings.get("0") or {}).get("totalRating"),
            "status": ppe_of(wrap).get("status"),
        })

    def _per_game(self, avg: dict) -> dict[str, float]:
        out = {}
        for c in self.cats:
            if c in RATIO_PARTS:
                m, a = RATIO_PARTS[c]
                out[c] = avg.get(m, 0.0) - self.bar[c] * avg.get(a, 0.0)
            else:
                out[c] = avg.get(c, 0.0)
        return out

    def _normalize(self) -> None:
        n_pool = max(60, len(self.view.teams) * 13)
        pool = sorted(
            [p for p in self.players if p["gp"] >= 4 and p["min"] >= 15],
            key=lambda p: (p["rater"] if p["rater"] is not None else -99), reverse=True,
        )[:n_pool] or self.players
        self.bar = {}
        for c, (m, a) in RATIO_PARTS.items():
            att = sum(p["avg"].get(a, 0.0) for p in pool)
            self.bar[c] = (sum(p["avg"].get(m, 0.0) for p in pool) / att) if att else 0.0
        for p in self.players:
            p["pg"] = self._per_game(p["avg"])
            p["pg15"] = self._per_game(p["l15"])
            p["var"] = {c: PRIOR_RATIO.get(c, 1.0) * (abs(p["pg"][c]) + 0.25) for c in self.cats}
        self.mu = {c: st.mean(p["pg"][c] for p in pool) for c in self.cats}
        self.sd = {c: (st.pstdev([p["pg"][c] for p in pool]) or 1.0) for c in self.cats}
        for p in self.players:
            p["g"] = self.g_scores(p["pg"])
            p["g15"] = self.g_scores(p["pg15"])

    def g_scores(self, pg: dict) -> dict[str, float]:
        return {c: self.sign[c] * (pg[c] - self.mu[c]) / self.sd[c] * G_WEIGHT.get(c, 0.8) for c in self.cats}

    def value(self, p: dict, punt=(), recent=False) -> float:
        g = p["g15"] if recent else p["g"]
        return sum(v for c, v in g.items() if c not in punt)

    # ---- teams -----------------------------------------------------------------
    def roster(self, tid, include_injured=False) -> list[dict]:
        ir = self.view.sport.ir_slot
        return [p for p in self.players if p["owner"] == tid
                and (include_injured or (p["slot"] != ir and p["injury"] not in OUT_STATUSES))]

    def week(self, plist: list[dict], games: float = G_WEEK) -> dict:
        mean = {c: 0.0 for c in self.cats}
        var = {c: 0.0 for c in self.cats}
        att = defaultdict(float)
        for p in plist:
            for c in self.cats:
                mean[c] += games * p["pg"][c]
                var[c] += games * p["var"][c]
            for c, (_, a) in RATIO_PARTS.items():
                att[c] += games * p["avg"].get(a, 0.0)
        return {"mean": mean, "var": var, "att": att}

    def cat_probs(self, A: dict, B: dict) -> dict[str, float]:
        out = {}
        for c in self.cats:
            if c in RATIO_PARTS:
                aa, ab = max(A["att"][c], 1e-6), max(B["att"][c], 1e-6)
                diff = A["mean"][c] / aa - B["mean"][c] / ab
                v = A["var"][c] / aa ** 2 + B["var"][c] / ab ** 2
            else:
                diff = A["mean"][c] - B["mean"][c]
                v = A["var"][c] + B["var"][c]
            out[c] = _phi(self.sign[c] * diff / math.sqrt(max(v, 1e-9)) / LAMBDA)
        return out

    def p_win(self, probs: dict[str, float]) -> float:
        """Most categories: P(win a majority). Each category: expected share won."""
        if self.view.scoring_type != "H2H_MOST_CATEGORIES":
            return st.mean(probs.values())
        dist = [1.0]
        for q in probs.values():
            nxt = [0.0] * (len(dist) + 1)
            for k, pr in enumerate(dist):
                nxt[k] += pr * (1 - q)
                nxt[k + 1] += pr * q
            dist = nxt
        need = len(probs) // 2 + 1
        ties = dist[len(probs) // 2] if len(probs) % 2 == 0 else 0.0
        return sum(dist[need:]) + 0.5 * ties

    def team_weeks(self) -> dict:
        return {t: self.week(self.roster(t)) for t in self.view.teams}

    def allplay(self, plist: list[dict], tid, weeks: dict) -> float:
        tw = self.week(plist)
        return st.mean(self.p_win(self.cat_probs(tw, weeks[b])) for b in self.view.teams if b != tid)


# ------------------------------------------------------------------------------
def category_strategy(view: LeagueView, pool_wraps: list[dict], team_id) -> dict:
    m = CategoryModel(view, pool_wraps)
    weeks = m.team_weeks()
    teams = list(view.teams)
    strength = sorted(((t, m.allplay(m.roster(t), t, weeks)) for t in teams), key=lambda x: -x[1])
    mine_now = m.roster(team_id)
    mine_full = m.roster(team_id, include_injured=True)

    def cat_odds(plist):
        tw = m.week(plist)
        return {c: st.mean(m.cat_probs(tw, weeks[b])[c] for b in teams if b != team_id) for c in m.cats}

    odds_now = cat_odds(mine_now)
    injured = [p for p in mine_full if p not in mine_now]
    odds_full = cat_odds(mine_full) if injured else None
    ap_now = m.allplay(mine_now, team_id, weeks)
    ap_full = m.allplay(mine_full, team_id, weeks) if injured else None

    fa = [p for p in m.players if not p["owner"] and p["injury"] not in OUT_STATUSES and p["gp"] >= 3]
    fa_sorted = sorted(fa, key=m.value, reverse=True)
    repl = fa_sorted[min(10, len(fa_sorted) - 1)] if fa_sorted else None

    marginal = []
    base_full = m.allplay(mine_full, team_id, weeks)
    for p in mine_full:
        trial = [q for q in mine_full if q is not p] + ([repl] if repl else [])
        marginal.append((p, base_full - m.allplay(trial, team_id, weeks)))
    marginal.sort(key=lambda x: x[1])
    rotation = [p for p, _ in marginal if p["injury"] not in OUT_STATUSES and p["slot"] != view.sport.ir_slot][:2]

    fits = []
    for p in fa:
        best = None
        for r in rotation:
            gain = m.allplay([q for q in mine_now if q is not r] + [p], team_id, weeks) - ap_now
            if best is None or gain > best[0]:
                best = (gain, r)
        if best:
            fits.append((best[0], p, best[1]))
    fits.sort(key=lambda x: -x[0])

    def strengths(p, k=3):
        return [c for c, _ in sorted(p["g"].items(), key=lambda kv: -kv[1])[:k]]

    def tier(o):
        return "strong" if o >= 0.6 else "swing" if o >= 0.4 else "weak"

    ref = odds_full or odds_now
    focus = [c for c, o in sorted(ref.items(), key=lambda kv: -kv[1])]
    need = len(m.cats) // 2 + 1
    stream_gain = None
    if repl:
        tw = m.week(mine_now)
        for c in m.cats:
            tw["mean"][c] += repl["pg"][c]
            tw["var"][c] += repl["var"][c]
        for c, (_, a) in RATIO_PARTS.items():
            tw["att"][c] += repl["avg"].get(a, 0.0)
        stream_gain = st.mean(m.p_win(m.cat_probs(tw, weeks[b])) for b in teams if b != team_id) - ap_now

    out: dict[str, Any] = {
        "format": view.settings_view().get("format"),
        "to_win_a_week": f"win {need} of {len(m.cats)} categories" if view.scoring_type == "H2H_MOST_CATEGORIES" else "every category counts",
        "model_strength": [{"team": view.team_label(t), "weekly_win_pct": round(100 * v)} for t, v in strength],
        "your_weekly_win_pct": {"now": round(100 * ap_now), "full_strength": round(100 * ap_full) if ap_full is not None else None},
        "your_category_odds_now": {c: f"{round(100 * o)}% ({tier(o)})" for c, o in odds_now.items()},
        "your_category_odds_full_strength": {c: f"{round(100 * o)}% ({tier(o)})" for c, o in odds_full.items()} if odds_full else None,
        "suggested_build": {
            "win_these": focus[:need + 1],
            "concede_if_needed": [c for c in focus[need + 1:] if ref[c] < 0.45],
            "why": "In most-categories you only need a majority each week, so pour resources into the "
                   f"{need}-{need + 1} categories you're best at and stop chasing the weakest ones.",
        },
        "your_players": [
            compact({
                "name": p["name"], "role": "injured" if p["injury"] in OUT_STATUSES else ("rotation" if p in rotation else "core"),
                "injury": p["injury"], "g_value": rnd(m.value(p), 2), "g_value_last15": rnd(m.value(p, recent=True), 2),
                "weekly_win_pct_contribution": round(100 * dv, 1), "best_categories": strengths(p),
            })
            for p, dv in sorted(marginal, key=lambda x: -x[1])
        ],
        "rotation_spots": [p["name"] for p in rotation],
        "best_pickups_for_you": [
            compact({
                "name": p["name"], "status": "on waivers" if p["status"] == "WAIVERS" else "free agent",
                "eligible": p["eligible"], "injury": p["injury"],
                "weekly_win_pct_gain": round(100 * g, 1), "replaces": r["name"],
                "best_categories": strengths(p), "games_next_7": view.bball_games(p["pro"]).get("next_7_days"),
                "g_value": rnd(m.value(p), 2), "g_value_last15": rnd(m.value(p, recent=True), 2),
            })
            for g, p, r in fits[:10] if g > 0
        ],
        "each_extra_game_per_week_adds": f"~{round(100 * stream_gain, 1)} points of weekly win %" if stream_gain else None,
        "method": "Per-game G-scores (z-scores discounted for week-to-week noise) and a weekly matchup model "
                  "calibrated on this league's results. Role 'rotation' = your two players worth least "
                  "over a typical free agent; core players are never suggested as drops.",
    }
    return compact(out)


def player_values(view: LeagueView, pool_wraps: list[dict], punt: list[str] | None = None,
                  who: str = "all", limit: int = 30, recent: bool = False) -> dict:
    m = CategoryModel(view, pool_wraps)
    asked = [str(c).strip().upper().replace("FG", "FG").replace("TOV", "TO") for c in (punt or [])]
    punt = [c for c in asked if c in m.cats]
    ignored = [c for c in asked if c not in m.cats]
    rows = []
    for p in m.players:
        if who == "free" and p["owner"]:
            continue
        if who == "rostered" and not p["owner"]:
            continue
        rows.append(p)
    rows.sort(key=lambda p: m.value(p, punt, recent), reverse=True)
    return compact({
        "punting": punt or None,
        "ignored_not_league_categories": ignored or None,
        "basis": "last 15 days" if recent else "season",
        "players": [
            compact({
                "name": p["name"],
                "fantasy_team": view.team_label(p["owner"]) if p["owner"] else ("on waivers" if p["status"] == "WAIVERS" else "free agent"),
                "injury": p["injury"], "eligible": p["eligible"],
                "value": rnd(m.value(p, punt, recent), 2),
                "by_category": {c: rnd((p["g15"] if recent else p["g"])[c], 2) for c in m.cats if c not in punt},
                "gp": p["gp"],
            })
            for p in rows[:limit]
        ],
    })


# ------------------------------------------------------------------------------
def nba_calendar(view: LeagueView) -> list[dict]:
    """ESPN basketball matchup units -> date ranges.

    Units are Mon-Sun weeks from the season's first game week; February weeks
    with well under a normal week's games (All-Star break) are merged with the
    next week, matching how ESPN numbers basketball matchup weeks.
    """
    pro = view.pro
    days = sorted({d for d in pro.period_date.values()})
    if not days:
        return []
    week0 = days[0] - dt.timedelta(days=days[0].weekday())
    games_per_week: dict[int, int] = defaultdict(int)
    for tid, periods in pro.by_team.items():
        for period, games in periods.items():
            d = pro.period_date.get(period)
            if d:
                games_per_week[(d - week0).days // 7 + 1] += len(games)
    weeks = sorted(games_per_week)
    median = st.median(games_per_week[w] for w in weeks)
    units, i = [], 0
    while i < len(weeks):
        w = weeks[i]
        start = week0 + dt.timedelta(days=7 * (w - 1))
        light = games_per_week[w] < 0.7 * median and start.month == 2
        nxt = weeks[i + 1] if i + 1 < len(weeks) else None
        if light and nxt == w + 1 and games_per_week[nxt] < 0.7 * median:
            units.append((start, start + dt.timedelta(days=13)))
            i += 2
        else:
            units.append((start, start + dt.timedelta(days=6)))
            i += 1
    return [{"unit": n + 1, "start": s, "end": e} for n, (s, e) in enumerate(units)]


def schedule_planner(view: LeagueView, team_id, days_ahead: int = 14) -> dict:
    cal = nba_calendar(view)
    by_unit = {u["unit"]: u for u in cal}
    pro = view.pro
    roster = [e for e in view.roster_entries(team_id)]
    healthy = [e for e in roster if e.get("lineupSlotId") != view.sport.ir_slot and view._injury(e, player_of(e)) not in OUT_STATUSES]
    starters = sum(c for s, c in view.slot_counts.items() if view.sport.is_starting_slot(s))

    def games_between(pro_team, start, end):
        return sum(1 for p, d in pro.period_date.items() if start <= d <= end and pro.game(pro_team, p))

    team_ids = [t for t in pro.by_team if t]
    rows = []
    reg = view.reg_season_periods or 0
    for mp in sorted(view.matchup_periods):
        if mp < view.current_matchup_period:
            continue
        units = [by_unit[u] for u in view.matchup_periods[mp] if u in by_unit]
        if not units:
            continue
        start, end = units[0]["start"], units[-1]["end"]
        mine = sum(games_between(player_of(e).get("proTeamId"), start, end) for e in roster)
        avg_team = st.mean(games_between(t, start, end) for t in team_ids)
        rows.append(compact({
            "matchup": mp, "dates": f"{start:%b %d} - {end:%b %d}", "playoffs": True if mp > reg else None,
            "your_player_games": mine, "league_avg_for_same_roster_size": rnd(avg_team * len(roster), 1),
        }))
    playoff_units = [u for mp in view.matchup_periods if mp > reg for u in view.matchup_periods[mp] if u in by_unit]
    best_playoff_teams = None
    if playoff_units:
        spans = [(by_unit[u]["start"], by_unit[u]["end"]) for u in sorted(playoff_units)]
        tot = {t: [games_between(t, s, e) for s, e in spans] for t in team_ids}
        best_playoff_teams = [
            {"team": view.sport.pro_team(t), "games_by_playoff_week": g}
            for t, g in sorted(tot.items(), key=lambda kv: -sum(kv[1]))[:8]
        ]
    today = pro.today
    daily = []
    for i in range(days_ahead):
        d = today + dt.timedelta(days=i)
        periods = [p for p, pd in pro.period_date.items() if pd == d]
        playing = [player_of(e).get("fullName") for e in healthy if any(pro.game(player_of(e).get("proTeamId"), p) for p in periods)]
        if not periods:
            continue
        daily.append({"date": f"{d:%a %b %d}", "your_healthy_players_with_games": len(playing),
                      "open_starting_slots": max(0, starters - len(playing))})
    light = sorted(daily, key=lambda x: -x["open_starting_slots"])[:4]
    return compact({
        "matchups": rows,
        "best_nba_schedules_for_your_playoffs": best_playoff_teams,
        "next_days": daily,
        "best_streaming_days": [x["date"] for x in light if x["open_starting_slots"] >= 3],
        "note": "Matchup dates follow ESPN's Mon-Sun weeks (the All-Star break week is merged with the next). "
                "Early-December NBA Cup games may be added to the schedule later.",
    })
