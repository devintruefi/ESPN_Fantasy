"""Category leagues (basketball): rest-of-season simulation, stress tests and trades.

Built on ``catmodel.CategoryModel`` (per-game G-scores and a weekly category matchup model
calibrated on real weekly results). On top of it:
- Each remaining matchup's win probability comes from both teams' expected weekly category
  totals; the current matchup starts from the live category totals plus the games left.
- A season is simulated with persistent team-strength uncertainty (probit shocks drawn once
  per season), the remaining schedule, the playoff bracket and byes.
- Every result is re-run under stress tests (more randomness, last-15-day form, slower injury
  returns, injured players not returning). A move is a clear win only if it helps in all of them.
- Rosters are compared at full size: a team that ends up over the roster limit drops its
  lowest-value players; a team left short adds a typical free agent.
Pure functions (no network).
"""

from __future__ import annotations

import itertools
import math
import random
import statistics as st
import time
from collections import defaultdict
from statistics import NormalDist

from .catmodel import G_WEEK, LAMBDA, PRIOR_RATIO, RATIO_PARTS, CategoryModel, _phi
from .sports import OUT_STATUSES
from .views import LeagueView, compact, num, rnd

_ND = NormalDist()

SCENARIOS = [
    {"key": "model", "label": "Season averages", "recent": False, "shrink": 1.0, "sigma": 0.3, "return_weeks": 2},
    {"key": "noise", "label": "More randomness", "recent": False, "shrink": 0.6, "sigma": 0.5, "return_weeks": 2},
    {"key": "recent", "label": "Last-15-day form", "recent": True, "shrink": 1.0, "sigma": 0.3, "return_weeks": 2},
    {"key": "slow", "label": "Injured back 2 weeks later", "recent": False, "shrink": 1.0, "sigma": 0.3, "return_weeks": 4},
    {"key": "none", "label": "Injured players don't return", "recent": False, "shrink": 1.0, "sigma": 0.3, "return_weeks": None},
]
MIN_GAIN = 0.005   # half a point of weekly all-play win %: smaller changes are noise


def _probit(p: float) -> float:
    return _ND.inv_cdf(min(max(p, 1e-6), 1 - 1e-6))


def p_majority(probs) -> float:
    """P(winning more categories than the opponent), ties counted half."""
    probs = list(probs)
    dist = [1.0]
    for q in probs:
        nxt = [0.0] * (len(dist) + 1)
        for k, pr in enumerate(dist):
            nxt[k] += pr * (1 - q)
            nxt[k + 1] += pr * q
        dist = nxt
    n = len(probs)
    return sum(dist[n // 2 + 1:]) + (0.5 * dist[n // 2] if n % 2 == 0 else 0.0)


class CategorySeason:
    def __init__(self, view: LeagueView, pool: list[dict], live_raw: dict | None = None):
        if not view.is_categories or view.scoring_type == "ROTO":
            raise ValueError("CategorySeason needs a head-to-head category league")
        self.view = view
        self.each = view.scoring_type != "H2H_MOST_CATEGORIES"   # every category is its own win/loss
        self.m = m = CategoryModel(view, pool)
        self.by_id = {p["id"]: p for p in m.players}
        self.teams = list(view.teams)
        self.reg_end = view.reg_season_periods or 0
        self.cur = view.current_matchup_period
        self.sched: dict[int, list[tuple[int, int]]] = {}
        for g in view.core.get("schedule") or []:
            mp = g.get("matchupPeriodId") or 0
            h, a = (g.get("home") or {}).get("teamId"), (g.get("away") or {}).get("teamId")
            if h and a and self.cur <= mp <= self.reg_end:
                self.sched.setdefault(mp, []).append((h, a))
        self.playoff_mps = sorted(mp for mp in view.matchup_periods if mp > self.reg_end)
        self.n_playoff = int((view.settings.get("scheduleSettings") or {}).get("playoffTeamCount") or 0)
        self.limit = sum(c for sid, c in view.slot_counts.items() if sid != view.sport.ir_slot)
        fa = sorted((p for p in m.players if not p["owner"] and p["injury"] not in OUT_STATUSES and p["gp"] >= 3),
                    key=m.value, reverse=True)
        self.replacement = fa[min(10, len(fa) - 1)] if fa else None
        self.live = live_probs(view, m, live_raw) if live_raw else {}
        self._wk: dict = {}

    # ---- rosters ----------------------------------------------------------------
    def roster(self, tid) -> list[dict]:
        return [p for p in self.m.players if p["owner"] == tid]

    def rosters(self) -> dict[int, list[dict]]:
        return {t: self.roster(t) for t in self.teams}

    def injured(self, p: dict) -> bool:
        return p["injury"] in OUT_STATUSES or p["slot"] == self.view.sport.ir_slot

    def fit_roster(self, plist: list[dict], recent: bool = False) -> list[dict]:
        """Full-size roster: drop the lowest-value players over the limit (IR doesn't count),
        or fill open spots with a typical free agent."""
        ir = [p for p in plist if p["slot"] == self.view.sport.ir_slot]
        rest = [p for p in plist if p["slot"] != self.view.sport.ir_slot]
        rest.sort(key=lambda p: self.m.value(p, recent=recent), reverse=True)
        if len(rest) > self.limit:
            rest = rest[:self.limit]
        elif self.replacement is not None:
            rest += [self.replacement] * (self.limit - len(rest))
        return rest + ir

    def active(self, plist: list[dict], mp: int, sc: dict) -> list[dict]:
        back = sc["return_weeks"]
        return [p for p in plist if not self.injured(p) or (back is not None and mp >= self.cur + back)]

    # ---- weekly strength ----------------------------------------------------------
    def week(self, plist: list[dict], games: float, recent: bool) -> dict:
        m = self.m
        mean = {c: 0.0 for c in m.cats}
        var = {c: 0.0 for c in m.cats}
        att = defaultdict(float)
        for p in plist:
            pg = p["pg15"] if recent else p["pg"]
            src = p["l15"] if recent else p["avg"]
            for c in m.cats:
                mean[c] += games * pg[c]
                var[c] += games * PRIOR_RATIO.get(c, 1.0) * (abs(pg[c]) + 0.25)
            for c, (_, a) in RATIO_PARTS.items():
                att[c] += games * src.get(a, 0.0)
        return {"mean": mean, "var": var, "att": att}

    def units(self, mp: int) -> int:
        return max(1, len(self.view.matchup_periods.get(mp) or [mp]))

    def p_game(self, A: dict, B: dict) -> float:
        """P(A wins the matchup: more categories than B)."""
        return p_majority(self.m.cat_probs(A, B).values())

    def cat_share(self, A: dict, B: dict) -> float:
        """Expected share of categories A wins (how each-category leagues count a week)."""
        return st.mean(self.m.cat_probs(A, B).values())

    def _team_week(self, plist: list[dict], mp: int, sc: dict) -> dict:
        act = self.active(plist, mp, sc)
        key = (tuple(sorted(p["id"] for p in act)), self.units(mp), sc["recent"])
        hit = self._wk.get(key)
        if hit is None:
            hit = self._wk[key] = self.week(act, G_WEEK * self.units(mp), sc["recent"])
        return hit

    # ---- the season ---------------------------------------------------------------
    def simulate(self, rosters: dict[int, list[dict]], sc: dict, sims: int = 4000, seed: int = 7) -> dict:
        """Season odds for every team under one stress test (same seed = same random draws)."""
        view = self.view
        teams = self.teams
        rosters = {t: self.fit_roster(pl, sc["recent"]) for t, pl in rosters.items()}
        base_w = {t: num(view.record(t).get("wins")) + 0.5 * num(view.record(t).get("ties")) for t in teams}
        base_l = {t: num(view.record(t).get("losses")) + 0.5 * num(view.record(t).get("ties")) for t in teams}
        shrink = sc["shrink"]
        games = []   # (home, away, [probits], live P(home) or None); one probit per category when each counts
        for mp, pairs in sorted(self.sched.items()):
            for h, a in pairs:
                live = self.live.get((h, a)) if mp == self.cur else None
                if live is not None:
                    games.append((h, a, None, live))
                    continue
                A, B = self._team_week(rosters[h], mp, sc), self._team_week(rosters[a], mp, sc)
                if self.each:
                    zs = [_probit(0.5 + shrink * (q - 0.5)) for q in self.m.cat_probs(A, B).values()]
                else:
                    zs = [_probit(0.5 + shrink * (self.p_game(A, B) - 0.5))]
                games.append((h, a, zs, None))
        zcache: dict = {}

        def zp(x, y, mp):
            k = (x, y, mp)
            if k not in zcache:
                p = self.p_game(self._team_week(rosters[x], mp, sc), self._team_week(rosters[y], mp, sc))
                zcache[k] = _probit(0.5 + sc["shrink"] * (p - 0.5))
            return zcache[k]

        rng = random.Random(seed)
        sigma = sc["sigma"]
        cdf = _ND.cdf
        tally = {t: {"playoffs": 0, "bye": 0, "final": 0, "title": 0, "wins": 0.0, "seed": 0} for t in teams}
        by_wins: dict[int, dict[float, list[int]]] = {t: {} for t in teams}
        byes = 2 if self.n_playoff == 6 else 0
        n_live = len(self.m.cats) if self.each else 1
        for _ in range(sims):
            s = {t: rng.gauss(0, sigma) for t in teams}
            W, L = dict(base_w), dict(base_l)
            for h, a, zs, live_p in games:
                if zs is None:   # live matchup: draw the matchup (most) or each category's share (each)
                    k = sum(rng.random() < live_p for _ in range(n_live))
                    W[h] += k
                    L[h] += n_live - k
                    W[a] += n_live - k
                    L[a] += k
                    continue
                d = s[h] - s[a]
                for z in zs:
                    if rng.random() < cdf(z + d):
                        W[h] += 1
                        L[a] += 1
                    else:
                        W[a] += 1
                        L[h] += 1
            order = sorted(teams, key=lambda t: (W[t] / max(W[t] + L[t], 1), rng.random()), reverse=True)
            for i, t in enumerate(order):
                tally[t]["wins"] += W[t] - base_w[t]
                tally[t]["seed"] += i + 1
            made = order[:self.n_playoff] if self.n_playoff else []
            for t in made:
                tally[t]["playoffs"] += 1
            for t in made[:byes]:
                tally[t]["bye"] += 1
            for t in teams:
                slot = by_wins[t].setdefault((W[t], L[t]), [0, 0])
                slot[1] += 1
                slot[0] += t in made

            def game(x, y, mp):
                return x if rng.random() < cdf(zp(x, y, mp) + s[x] - s[y]) else y

            rounds, seeds = self.playoff_mps, list(made)
            if self.n_playoff == 6 and len(rounds) >= 3:
                w1, w2 = game(seeds[2], seeds[5], rounds[0]), game(seeds[3], seeds[4], rounds[0])
                low, high = sorted([w1, w2], key=seeds.index)
                f1, f2, final_mp = game(seeds[0], high, rounds[1]), game(seeds[1], low, rounds[1]), rounds[2]
            elif self.n_playoff == 4 and len(rounds) >= 2:
                f1, f2, final_mp = game(seeds[0], seeds[3], rounds[0]), game(seeds[1], seeds[2], rounds[0]), rounds[1]
            elif self.n_playoff == 8 and len(rounds) >= 3:
                q = [game(seeds[0], seeds[7], rounds[0]), game(seeds[3], seeds[4], rounds[0]),
                     game(seeds[1], seeds[6], rounds[0]), game(seeds[2], seeds[5], rounds[0])]
                f1, f2, final_mp = game(q[0], q[1], rounds[1]), game(q[2], q[3], rounds[1]), rounds[2]
            else:
                continue
            tally[f1]["final"] += 1
            tally[f2]["final"] += 1
            tally[game(f1, f2, final_mp)]["title"] += 1
        out = {t: {k: v / sims for k, v in r.items()} for t, r in tally.items()}
        return {"teams": out, "by_wins": by_wins, "sims": sims, "has_bye": bool(byes)}

    def stress_test(self, rosters, sims: int = 2000) -> dict[str, dict]:
        return {sc["key"]: self.simulate(rosters, sc, sims=sims, seed=11) for sc in SCENARIOS}

    # ---- quick strength (for search) --------------------------------------------------
    def allplay_weeks(self, rosters, recent: bool) -> dict[int, dict]:
        """Full-strength weekly totals (injured players counted) for a one-week all-play."""
        return {t: self.week(self.fit_roster(pl, recent), G_WEEK, recent) for t, pl in rosters.items()}

    def allplay(self, tid, wk: dict, weeks: dict) -> float:
        """Average weekly result against every other team: P(win) in most-categories leagues,
        expected share of categories won when each category counts."""
        f = self.cat_share if self.each else self.p_game
        return st.mean(f(wk, weeks[b]) for b in self.teams if b != tid)


def objective(odds: dict) -> str:
    return "title" if odds.get("playoffs", 0) >= 0.9 else "playoffs"


KINDS = {"Win-win": "win-win", "It helps you in every": "helps you; costs them", "It depends": "depends on assumptions",
         "It hurts": "hurts you"}
_KIND_ORDER = ["win-win", "helps you; costs them", "depends on assumptions", "hurts you"]


def kind(verdict: str) -> str:
    return next((v for k, v in KINDS.items() if verdict.startswith(k)), "depends on assumptions")


def rank_results(results: list[dict], top: int) -> list[dict]:
    """Win-wins first, then deals that surely help you, then the rest; within a group by your
    worst case across the stress tests. Deals that hurt you are dropped."""
    results = [r for r in results if r["type"] != "hurts you"]
    results.sort(key=lambda r: (_KIND_ORDER.index(r["type"]), -r["_worst"]))
    for r in results:
        r.pop("_worst", None)
    return results[:top]


# ---- the current matchup, from live category totals ---------------------------------------
def live_probs(view: LeagueView, m: CategoryModel, raw: dict) -> dict[tuple[int, int], float]:
    """{(home, away): P(home wins)} for this matchup period: categories so far plus the
    expected production of games not yet started (same method as matchup_projection)."""
    from .strategy import RAW, _cat_values, _period_raw, _remaining_production

    out = {}
    for g in (raw or {}).get("schedule") or []:
        if g.get("matchupPeriodId") != view.current_matchup_period:
            continue
        sides = [g.get("home") or {}, g.get("away") or {}]
        if not all(s.get("teamId") for s in sides):
            continue
        proj, var = [], []
        for side in sides:
            tid = side["teamId"]
            add, n_games, _ = _remaining_production(view, view.roster_entries(tid))
            cur, tot = _cat_values(side), _period_raw(side)
            act = [p for p in m.players if p["owner"] == tid and p["injury"] not in OUT_STATUSES] or m.players
            pv, vv = {}, {}
            for c in view.categories:
                name = c["name"]
                per_game_var = st.mean(p["var"][name] for p in act) if name in m.cats else 0.0
                if name in RATIO_PARTS:
                    mk, at = RATIO_PARTS[name]
                    cur_att = tot.get(RAW[at], 0.0)
                    a_m, a_a = add.get(mk, 0.0), add.get(at, 0.0)
                    r = cur.get(str(c["id"]))
                    pv[name] = ((r or 0.0) * cur_att + a_m) / (cur_att + a_a) if cur_att + a_a > 0 else (r or 0.0)
                    vv[name] = n_games * per_game_var / max(cur_att + a_a, 1.0) ** 2
                else:
                    pv[name] = (cur.get(str(c["id"])) or 0.0) + add.get(name, 0.0)
                    vv[name] = n_games * per_game_var
            proj.append(pv)
            var.append(vv)
        q = {}
        for c in m.cats:
            diff = m.sign[c] * (proj[0].get(c, 0.0) - proj[1].get(c, 0.0))
            v = var[0].get(c, 0.0) + var[1].get(c, 0.0)
            q[c] = _phi(diff / math.sqrt(v) / LAMBDA) if v > 1e-9 else (0.5 if abs(diff) < 1e-9 else float(diff > 0))
        out[(sides[0]["teamId"], sides[1]["teamId"])] = (
            p_majority(q.values()) if view.scoring_type == "H2H_MOST_CATEGORIES" else st.mean(q.values()))
    return out


# ---- season outlook ------------------------------------------------------------------
def season_outlook(season: CategorySeason, team_id, sims: int = 5000) -> dict:
    view = season.view
    if view.season_over or season.cur > season.reg_end:
        return {"team": view.team_label(team_id),
                "note": "The regular season is over, so there are no playoff odds to simulate; "
                        "use matchup_projection for the current matchup."}
    rosters = season.rosters()
    base_sc = SCENARIOS[0]
    res = season.simulate(rosters, base_sc, sims=sims, seed=7)
    tally = res["teams"]
    stress = season.stress_test(rosters, sims=max(1500, sims // 3))

    def pct(x):
        return round(100.0 * x, 1)

    lo = {k: min(stress[s["key"]]["teams"][team_id][k] for s in SCENARIOS) for k in ("playoffs", "bye", "title")}
    hi = {k: max(stress[s["key"]]["teams"][team_id][k] for s in SCENARIOS) for k in ("playoffs", "bye", "title")}
    by_test = [{"test": s["label"], "playoffs_pct": pct(stress[s["key"]]["teams"][team_id]["playoffs"]),
                "bye_pct": pct(stress[s["key"]]["teams"][team_id]["bye"]) if res["has_bye"] else None,
                "title_pct": pct(stress[s["key"]]["teams"][team_id]["title"])} for s in SCENARIOS]

    full = {t: season.fit_roster(pl) for t, pl in rosters.items()}
    league = []
    weeks_full = season.allplay_weeks(rosters, False)
    for t in sorted(season.teams, key=lambda t: (-tally[t]["playoffs"], -tally[t]["title"])):
        league.append(compact({
            "team": view.team_label(t), "record": view.record_str(t),
            "weekly_win_pct_full_strength": round(100 * season.allplay(t, weeks_full[t], weeks_full)),
            "playoffs_pct": pct(tally[t]["playoffs"]), "bye_pct": pct(tally[t]["bye"]) if res["has_bye"] else None,
            "title_pct": pct(tally[t]["title"]), "expected_wins_left": rnd(tally[t]["wins"], 1),
        }))

    my_weeks = []
    for mp in sorted(season.sched):
        g = next(((h, a) for h, a in season.sched[mp] if team_id in (h, a)), None)
        if not g:
            continue
        opp = g[1] if g[0] == team_id else g[0]
        if mp == season.cur and g in season.live:
            p = season.live[g] if g[0] == team_id else 1 - season.live[g]
            basis = "live categories + games left"
        else:
            A = season._team_week(full[team_id], mp, base_sc)
            B = season._team_week(full[opp], mp, base_sc)
            p = season.cat_share(A, B) if season.each else season.p_game(A, B)
            basis = None
        key = "expected_category_share_pct" if season.each else "win_pct"
        my_weeks.append(compact({"matchup": mp, "opponent": view.team_label(opp), "opponent_record": view.record_str(opp),
                                 key: round(100 * p), "basis": basis}))

    injured = [p["name"] for p in rosters[team_id] if season.injured(p)]
    wins_needed = []
    for (w, l_), (made, n) in sorted(res["by_wins"][team_id].items()):
        if n / sims >= 0.01:
            wins_needed.append({"final_record": f"{w:g}-{l_:g}", "playoff_pct": pct(made / n),
                                "share_of_seasons_pct": pct(n / sims)})
    if season.each and len(wins_needed) > 12:   # category records spread wide: keep the decisive range
        wins_needed = [r for r in wins_needed if 2 <= r["playoff_pct"] <= 98] or wins_needed[:12]
    return {
        "team": view.team_label(team_id),
        "your_outlook": compact({
            "playoffs_pct": pct(tally[team_id]["playoffs"]),
            "bye_pct": pct(tally[team_id]["bye"]) if res["has_bye"] else None,
            "reach_final_pct": pct(tally[team_id]["final"]),
            "title_pct": pct(tally[team_id]["title"]),
            "expected_wins_left": rnd(tally[team_id]["wins"], 1),
            "avg_final_seed": rnd(tally[team_id]["seed"], 1),
            "stress_test_range": {"playoffs_pct": f"{pct(lo['playoffs'])}-{pct(hi['playoffs'])}",
                                  "bye_pct": f"{pct(lo['bye'])}-{pct(hi['bye'])}" if res["has_bye"] else None,
                                  "title_pct": f"{pct(lo['title'])}-{pct(hi['title'])}"},
        }),
        "stress_tests": by_test,
        "your_matchups": my_weeks,
        "wins_needed": wins_needed,
        "league": league,
        "sims": sims,
        "notes": [
            "Weekly odds come from each team's per-game category production (season averages; the "
            "'last-15-day form' test uses recent games) and this league's calibrated category model; the "
            "current matchup starts from the live category totals.",
            f"Injured players ({', '.join(injured) or 'none on your team'}) are assumed back in 2 matchups; the stress "
            "tests try 4 and never. Tell the model what the news says to refine it.",
            "Every team plays its full roster each week (about 3.4 games a player); daily lineup limits and "
            "each NBA team's schedule volume are in schedule_planner, not here.",
            "Standings ties are broken at random (ESPN's own tiebreak isn't modeled).",
        ],
    }


# ---- trades ---------------------------------------------------------------------------
def _swap(rosters, me, partner, give_ids, get_ids):
    out = dict(rosters)
    give = [p for p in rosters[me] if p["id"] in give_ids]
    get = [p for p in rosters[partner] if p["id"] in get_ids]
    out[me] = [p for p in rosters[me] if p["id"] not in give_ids] + [dict(p, owner=me, slot=None) for p in get]
    out[partner] = [p for p in rosters[partner] if p["id"] not in get_ids] + [dict(p, owner=partner, slot=None) for p in give]
    return out


def _rater(p: dict) -> float:
    return num(p.get("rater")) or 0.0


def evaluate_trade(season: CategorySeason, me, partner, give_ids: list, get_ids: list, sims: int = 2000) -> dict:
    view = season.view
    rosters = season.rosters()
    gi, ri = set(give_ids), set(get_ids)
    give = [p for p in rosters[me] if p["id"] in gi]
    get = [p for p in rosters[partner] if p["id"] in ri]
    if len(give) != len(gi) or len(get) != len(ri):
        raise ValueError("Every player you give must be on your roster and every player you get on theirs.")
    after = _swap(rosters, me, partner, gi, ri)
    tests = []
    for sc in SCENARIOS:
        b = season.simulate(rosters, sc, sims=sims, seed=11)["teams"]
        a = season.simulate(after, sc, sims=sims, seed=11)["teams"]
        tests.append({"test": sc["label"], "b": b, "a": a})
    my_obj, th_obj = objective(tests[0]["b"][me]), objective(tests[0]["b"][partner])

    def rows(tid, obj):
        return [{"test": t["test"], "before_pct": rnd(100 * t["b"][tid][obj], 1), "after_pct": rnd(100 * t["a"][tid][obj], 1),
                 "change": rnd(100 * (t["a"][tid][obj] - t["b"][tid][obj]), 1)} for t in tests]

    mine, theirs = rows(me, my_obj), rows(partner, th_obj)
    mc, tc = [r["change"] for r in mine], [r["change"] for r in theirs]
    weekly = {}
    for label, recent in (("season", False), ("last_15_days", True)):
        wb, wa = season.allplay_weeks(rosters, recent), season.allplay_weeks(after, recent)
        weekly[label] = {
            "you": rnd(100 * (season.allplay(me, wa[me], wa) - season.allplay(me, wb[me], wb)), 1),
            "them": rnd(100 * (season.allplay(partner, wa[partner], wa) - season.allplay(partner, wb[partner], wb)), 1),
        }
    m = season.m
    fb = season.week(season.fit_roster(rosters[me]), G_WEEK, False)
    fa = season.week(season.fit_roster(after[me]), G_WEEK, False)
    weeks_b = season.allplay_weeks(rosters, False)
    cats = {}
    for c in m.cats:
        ob = st.mean(m.cat_probs(fb, weeks_b[t])[c] for t in season.teams if t != me)
        oa = st.mean(m.cat_probs(fa, weeks_b[t])[c] for t in season.teams if t != me)
        cats[c] = rnd(100 * (oa - ob), 1)
    helps_all, hurts_all = min(mc) > 0, max(mc) < 0
    their_ok = weekly["season"]["them"] > -0.5 and min(tc) > -1.0
    if helps_all and their_ok:
        verdict = "Win-win: it helps you in every stress test and doesn't hurt them, so it's a fair offer."
    elif helps_all:
        verdict = "It helps you in every stress test, but it costs them: expect pushback or a counter."
    elif hurts_all:
        verdict = "It hurts you in every stress test. Don't make it."
    else:
        verdict = (f"It depends on assumptions: it helps you in {sum(c > 0 for c in mc)} of {len(mc)} stress tests. "
                   "Look at which tests it fails before deciding.")
    notes = []
    for p in get:
        if season.injured(p):
            notes.append(f"{p['name']} is {p['injury'] or 'on IR'}: the odds assume he's back in 2 matchups "
                         "(the stress tests try 4 and never).")
    n_drop = len(after[me]) - len([p for p in after[me] if p["slot"] == view.sport.ir_slot]) - season.limit
    if n_drop > 0:
        kept = {id(p) for p in season.fit_roster(after[me])}
        drops = [p["name"] for p in after[me] if id(p) not in kept]
        notes.append(f"You'd need to drop {n_drop}: the model drops your lowest-value player(s) ({', '.join(drops)}).")
    return {
        "trade": f"You give {', '.join(p['name'] for p in give) or 'nothing'}; you get "
                 f"{', '.join(p['name'] for p in get) or 'nothing'} ({view.team_label(partner)})",
        "verdict": verdict,
        "your_odds": {"measure": my_obj, "worst_change": min(mc), "best_change": max(mc), "by_test": mine},
        "their_odds": {"measure": th_obj, "worst_change": min(tc), "best_change": max(tc), "by_test": theirs},
        "weekly_win_pct_change_full_strength": weekly,
        "your_category_win_pct_change": cats,
        "how_it_looks_in_the_app": {"they_get_player_rater": rnd(sum(_rater(p) for p in give), 1),
                                    "they_give_player_rater": rnd(sum(_rater(p) for p in get), 1),
                                    "note": "Managers mostly judge by ESPN's player rater and names."},
        "notes": notes,
        "method": f"{sims} simulated seasons per stress test, same random draws before and after; 'measure' is "
                  "playoff odds while a team is under 90% to make it, otherwise title odds.",
    }


def find_trades(season: CategorySeason, me, partners: list, must_get: list = (), must_give: list = (),
                max_size: int = 2, budget_s: float = 20.0, top: int = 4, sims: int = 1500) -> dict:
    """Search 1-for-1 up to 2-for-2 deals that raise your full-strength weekly all-play win % under
    both views (season and last 15 days) and don't cost the partner; best ones get the full stress test."""
    view = season.view
    t0 = time.monotonic()
    m = season.m
    rosters = season.rosters()
    mg, mr = set(must_give), set(must_get)
    base = {r: season.allplay_weeks(rosters, r) for r in (False, True)}
    base_ap = {(t, r): season.allplay(t, base[r][t], base[r]) for t in [me] + list(partners) for r in (False, True)}

    def cands(tid):
        pl = sorted(rosters[tid], key=m.value, reverse=True)[:10]
        return pl + [p for p in rosters[tid] if p["id"] in (mg | mr) and p not in pl]

    sizes = [(g, r) for g, r in ((1, 1), (1, 2), (2, 1), (2, 2)) if max(g, r) <= max(1, min(max_size, 2))]
    mine = cands(me)
    found, coverage, out_of_time = [], {}, False
    for partner in partners:
        if time.monotonic() - t0 > budget_s:
            out_of_time = True
            coverage[view.team_label(partner)] = "skipped (out of time)"
            continue
        th = cands(partner)
        done = True
        for g, r in sizes:
            for give in itertools.combinations(mine, g):
                gi = {p["id"] for p in give}
                if mg and not mg <= gi:
                    continue
                for get in itertools.combinations(th, r):
                    ri = {p["id"] for p in get}
                    if mr and not mr <= ri:
                        continue
                    after = _swap(rosters, me, partner, gi, ri)
                    gains = []
                    ok = True
                    for recent in (False, True):
                        wk = season.week(season.fit_roster(after[me], recent), G_WEEK, recent)
                        d = season.allplay(me, wk, base[recent]) - base_ap[(me, recent)]
                        if d <= MIN_GAIN:
                            ok = False
                            break
                        gains.append(d)
                    if not ok:
                        continue
                    theirs = []
                    for recent in (False, True):
                        wk = season.week(season.fit_roster(after[partner], recent), G_WEEK, recent)
                        theirs.append(season.allplay(partner, wk, base[recent]) - base_ap[(partner, recent)])
                    if min(theirs) <= -0.01:
                        continue
                    found.append({"partner": partner, "give": list(give), "get": list(get), "me": gains, "them": theirs,
                                  "win_win": min(theirs) > MIN_GAIN, "score": min(gains + theirs)})
                if time.monotonic() - t0 > budget_s:
                    done = False
                    break
            if not done:
                break
        coverage[view.team_label(partner)] = ("1-for-1 to 2-for-2" if max_size >= 2 else "1-for-1") + ("" if done else " (partly; out of time)")
        out_of_time |= not done
    found.sort(key=lambda r: (len(r["give"]) + len(r["get"]), -r["score"]))
    kept = []
    for r in found:
        gi, ri = {p["id"] for p in r["give"]}, {p["id"] for p in r["get"]}
        if any(k["partner"] == r["partner"] and {p["id"] for p in k["give"]} <= gi and {p["id"] for p in k["get"]} <= ri
               and r["score"] < k["score"] + 0.01 for k in kept):
            continue
        kept.append(r)
    kept.sort(key=lambda r: (-r["win_win"], -r["score"]))
    shortlist = []
    for r in kept:
        if len(shortlist) >= 2 * top:
            break
        if any(c["partner"] == r["partner"] and {p["id"] for p in c["give"]} & {p["id"] for p in r["give"]}
               and {p["id"] for p in c["get"]} & {p["id"] for p in r["get"]} for c in shortlist):
            continue
        shortlist.append(r)
    results, evaluated = [], set()
    for i, r in enumerate(shortlist):
        if i >= top and time.monotonic() - t0 > budget_s + 10:
            break   # the first `top` are always checked; extra candidates only while there's time
        ev = evaluate_trade(season, me, r["partner"], [p["id"] for p in r["give"]], [p["id"] for p in r["get"]], sims=sims)
        evaluated.add(id(r))
        results.append({
            "partner": view.team_label(r["partner"]), "you_give": [p["name"] for p in r["give"]],
            "you_get": [p["name"] for p in r["get"]], "type": kind(ev["verdict"]),
            "verdict": ev["verdict"],
            "your_odds_change": f"{ev['your_odds']['worst_change']:+.1f} to {ev['your_odds']['best_change']:+.1f} pts of "
                                f"{ev['your_odds']['measure']} odds across stress tests",
            "their_odds_change": f"{ev['their_odds']['worst_change']:+.1f} to {ev['their_odds']['best_change']:+.1f} pts of "
                                 f"{ev['their_odds']['measure']} odds",
            "weekly_win_pct_change_full_strength": ev["weekly_win_pct_change_full_strength"],
            "how_it_looks_in_the_app": ev["how_it_looks_in_the_app"],
            "notes": ev["notes"], "_worst": ev["your_odds"]["worst_change"],
        })
    results = rank_results(results, top)
    more = [{"partner": view.team_label(r["partner"]), "you_give": [p["name"] for p in r["give"]],
             "you_get": [p["name"] for p in r["get"]], "weekly_lineups": "both improve" if r["win_win"] else "you improve, about even for them",
             "weekly_win_pct_change_you": f"{100 * r['me'][0]:+.1f} / {100 * r['me'][1]:+.1f}",
             "weekly_win_pct_change_them": f"{100 * r['them'][0]:+.1f} / {100 * r['them'][1]:+.1f}"}
            for r in kept if id(r) not in evaluated][:8]
    return {
        "best_trades": results,
        "also_worth_a_look": more,
        "searched": {"partners": coverage, "deals_that_help_you": len(kept),
                     "win_win_found": sum(r["win_win"] for r in kept),
                     "seconds": round(time.monotonic() - t0, 1), "complete": not out_of_time},
        "how_to_read": "Weekly win % changes are full-strength all-play (season / last 15 days). 'win-win' = both "
                       "teams improve under both views. Best trades are ranked by your worst case across the stress "
                       "tests. Each is judged on its own; overlapping deals can't all be made.",
    }
