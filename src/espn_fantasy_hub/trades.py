"""Trade evaluation and trade search for football points leagues.

Every judgment here is about *starting lineups* and *season odds*, never one week:
- Starting-lineup change for both teams, under two views: ESPN projections (the backtested
  best predictor) and season averages (what managers see in the app).
- Season odds (playoffs while in doubt, else the title) for both teams under every stress
  test in ``ffmodel.SCENARIOS``, simulated with common random numbers so "before" and
  "after" differ only by the trade.
A move is called a clear win only if it helps under every test. Pure functions (no network).
"""

from __future__ import annotations

import itertools
import time

from .catseason import kind, rank_results
from .ffmodel import SCENARIOS, SLOT_SPECIFICITY, FootballWorld, objective, regular_starters
from .views import compact, ms_to_dt, norm_name, rnd
from .writes import roster_limit

MIN_GAIN = 0.3        # points a week of starting lineup that counts as a real improvement
SIZES = [(1, 1), (1, 2), (2, 1), (2, 2), (2, 3), (3, 2), (3, 3)]


# ---- helpers ----------------------------------------------------------------------
def resolve(world: FootballWorld, names: list[str], tid=None) -> list[dict]:
    """Match names to players (on team ``tid`` when given). Raises ValueError with choices."""
    out = []
    pool = [world.players[i] for i in world.rosters.get(tid, [])] if tid is not None else list(world.players.values())
    for name in names:
        q = norm_name(name)
        exact = [p for p in pool if norm_name(p["name"] or "") == q]
        part = exact or [p for p in pool if q and q in norm_name(p["name"] or "")]
        if len(part) != 1:
            where = "on that roster" if tid is not None else "in this league"
            choices = ", ".join(sorted(p["name"] for p in pool if p["pos"] != "K"))[:600]
            raise ValueError(f"Couldn't match {name!r} to exactly one player {where}."
                             + (f" Options: {choices}" if tid is not None else ""))
        out.append(part[0])
    return out


def replacements(world: FootballWorld) -> list[dict]:
    """The best healthy free agent at each position, available to every team. A trade that
    leaves a hole (no D/ST, a thin bench) is then judged against a waiver pickup, not an empty
    slot. Shown as '<name> (free agent)'."""
    cache = world.__dict__.get("_replacements")
    if cache is None:
        best: dict[str, dict] = {}
        for pid in world.free_agents:
            p = world.players[pid]
            if p["status"] in ("OUT", "INJURY_RESERVE", "SUSPENSION") or not p["slots"]:
                continue
            if p["pos"] not in best or p["base"] > best[p["pos"]]["base"]:
                best[p["pos"]] = p
        cache = world.__dict__["_replacements"] = [dict(p, name=f"{p['name']} (free agent)") for p in best.values()]
    return cache


def team_plus(world: FootballWorld, tid) -> list[dict]:
    """A team's roster plus the free-agent replacements (for lineup math in trades)."""
    return world.team(tid) + replacements(world)


def _after(world: FootballWorld, me, partner, give: list[dict], get: list[dict]) -> dict[int, list[dict]]:
    rosters = {t: team_plus(world, t) for t in world.view.teams}
    gi, ri = {p["id"] for p in give}, {p["id"] for p in get}
    rosters[me] = [p for p in rosters[me] if p["id"] not in gi] + list(get)
    rosters[partner] = [p for p in rosters[partner] if p["id"] not in ri] + list(give)
    return rosters


def _base(world: FootballWorld, sims: int) -> dict:
    """Stress-test strengths and odds with today's rosters, cached on the world."""
    cache = world.__dict__.setdefault("_trade_base", {})
    if sims not in cache:
        rosters = {t: team_plus(world, t) for t in world.view.teams}
        out = {}
        for sc in SCENARIOS:
            STR = world.strengths(rosters, sc["value"], sc["injury"])
            out[sc["key"]] = (STR, world.simulate(STR, sims=sims, seed=11, tau=sc["tau"], sd_scale=sc["sd_scale"]))
        cache[sims] = out
    return cache[sims]


def _lineup_scores(world: FootballWorld) -> dict:
    cache = world.__dict__.setdefault("_lineup_base", {})
    if not cache:
        for t in world.view.teams:
            for mode in ("proj", "avg"):
                cache[(t, mode)] = world.lineup_score(team_plus(world, t), mode)
    return cache


def trade_window(view) -> str | None:
    """A warning when trades are closed (deadline passed or season over), else None."""
    if view.season_over:
        return "The season is over; trades are closed."
    now = view.pro.now
    deadline = ms_to_dt((view.settings.get("tradeSettings") or {}).get("deadlineDate"), now.tzinfo)
    if deadline and now > deadline:
        return f"The trade deadline has passed ({deadline:%b %d}); ESPN won't accept new trades."
    return None


def roster_check(world: FootballWorld, tid, give: list[dict], get: list[dict]) -> dict:
    """Roster-size check after the trade for team ``tid`` (which gives ``give`` and gets ``get``)."""
    view = world.view
    limit = roster_limit(view)
    roster = world.team(tid)
    active = [p for p in roster if not p["in_ir"]]
    gi = {p["id"] for p in give}
    after = len([p for p in active if p["id"] not in gi]) + len(get)
    over = max(0, after - limit)
    out = {"roster_spots": limit, "after_trade": after}
    if over:
        core = regular_starters(view, [p for p in roster if p["id"] not in gi] + list(get))
        cands = sorted((p for p in active if p["id"] not in gi and p["name"] not in core and p["pos"] != "K"),
                       key=lambda p: (p["status"] not in ("INJURY_RESERVE", "OUT"), p["base"]))
        out["must_drop"] = over
        out["suggested_drops"] = [f"{p['name']} ({p['pos']}{', ' + p['status'] if p['status'] else ''})" for p in cands[:over + 1]]
    return out


def _player_card(world: FootballWorld, p: dict) -> dict:
    view = world.view
    byes = [w for w in world.reg_weeks + world.po_weeks if view.pro.game(p["pro_id"], w) is None]
    nxt = view.scoring_period + 1
    return compact({
        "name": p["name"], "pos": p["pos"], "team": p["nfl"], "status": p["status"],
        "projected_per_game": rnd(p["base"], 1),
        "next_week_projection": rnd(p["by_week"].get(nxt), 1) if nxt in p["by_week"] else None,
        "season_avg": rnd(p["avg"], 1), "games": p["games"] or None,
        "byes_left": byes or None,
    })


def _schedule_notes(world: FootballWorld, me, partner) -> list[str]:
    view = world.view
    notes = []
    for mp, games in sorted(world.sched.items()):
        for h, a in games:
            if {h, a} == {me, partner}:
                weeks = world.weeks_of(mp)
                if mp == world.cur_mp and not view.season_over:
                    notes.append(f"{view.team_label(partner)} is your opponent this week: wait until this week's "
                                 "games are over before trading with them, or players could switch sides mid-matchup.")
                else:
                    notes.append(f"You play {view.team_label(partner)} in week {weeks[0]}; this trade changes that game too.")
    return notes


def lineup_by_week(world: FootballWorld, before: list[dict], after: list[dict], mode: str = "proj") -> list[dict]:
    """Week-by-week best lineup totals before/after a trade and who enters or leaves it."""
    rows = []
    for w in world.reg_weeks + world.po_weeks:
        b = {n: v for _, n, v in world.lineup(before, w, mode)}
        a = {n: v for _, n, v in world.lineup(after, w, mode)}
        rows.append(compact({
            "week": w, "playoffs": True if w in world.po_weeks else None,
            "before": rnd(world.lineup_total(before, w, mode), 1), "after": rnd(world.lineup_total(after, w, mode), 1),
            "starts": [f"{n} ({v:g})" for n, v in a.items() if n not in b] or None,
            "drops_out": [f"{n} ({v:g})" for n, v in b.items() if n not in a] or None,
        }))
    return rows


# ---- evaluate one trade -------------------------------------------------------------
def evaluate_trade(world: FootballWorld, me, partner, give: list[dict], get: list[dict], sims: int = 2500) -> dict:
    view = world.view
    if not give and not get:
        raise ValueError("Name the players you'd give and/or receive.")
    base = _base(world, sims)
    after = _after(world, me, partner, give, get)
    lb = _lineup_scores(world)
    lineup = {}
    for who, tid in (("you", me), ("them", partner)):
        lineup[who] = {
            "by_projections": rnd(world.lineup_score(after[tid], "proj") - lb[(tid, "proj")], 1),
            "by_season_averages": rnd(world.lineup_score(after[tid], "avg") - lb[(tid, "avg")], 1),
        }
    tests = []
    for sc in SCENARIOS:
        STR_b, odds_b = base[sc["key"]]
        STR_a = dict(STR_b)
        STR_a[me] = world.strength(after[me], sc["value"], sc["injury"])
        STR_a[partner] = world.strength(after[partner], sc["value"], sc["injury"])
        odds_a = world.simulate(STR_a, sims=sims, seed=11, tau=sc["tau"], sd_scale=sc["sd_scale"])
        tests.append({"test": sc["label"], "b": odds_b["teams"], "a": odds_a["teams"]})
    my_obj = objective(tests[0]["b"][me])
    th_obj = objective(tests[0]["b"][partner])

    def rows(tid, obj):
        return [{"test": t["test"], "before_pct": rnd(100 * t["b"][tid][obj], 1), "after_pct": rnd(100 * t["a"][tid][obj], 1),
                 "change": rnd(100 * (t["a"][tid][obj] - t["b"][tid][obj]), 1)} for t in tests]

    mine, theirs = rows(me, my_obj), rows(partner, th_obj)
    my_changes = [r["change"] for r in mine]
    th_changes = [r["change"] for r in theirs]
    helps_all = min(my_changes) > 0
    hurts_all = max(my_changes) < 0
    n_help = sum(1 for c in my_changes if c > 0)
    their_lineup_ok = lineup["them"]["by_projections"] > -0.5 and lineup["them"]["by_season_averages"] > -0.5
    their_odds_ok = min(th_changes) > -1.0
    if helps_all and their_lineup_ok and their_odds_ok:
        verdict = "Win-win: it helps you in every stress test and doesn't hurt them, so it's a fair offer."
    elif helps_all:
        verdict = ("It helps you in every stress test, but it costs them"
                   + (" on the season averages they see in the app" if lineup["them"]["by_season_averages"] <= -0.5 else "")
                   + ": expect pushback or a counter.")
    elif hurts_all:
        verdict = "It hurts you in every stress test. Don't make it."
    else:
        verdict = (f"It depends on assumptions: it helps you in {n_help} of {len(my_changes)} stress tests. "
                   "Look at which tests it fails before deciding.")

    gi = {p["id"] for p in give}
    my_core = regular_starters(view, world.team(me))
    notes = _schedule_notes(world, me, partner)
    for p in get:
        if p["status"] in ("OUT", "INJURY_RESERVE", "DOUBTFUL", "SUSPENSION"):
            notes.append(f"{p['name']} is {p['status']}: the odds assume ESPN's status timeline; check the news.")
    big = [p["name"] for p in give if p["name"] in my_core]
    if big:
        notes.append("You'd give up regular starters: " + ", ".join(big) + ". The numbers above already count that.")
    th_po = tests[0]["b"][partner]["playoffs"]
    if 0.15 < th_po < 0.9 and 0.15 < tests[0]["b"][me]["playoffs"] < 0.9:
        notes.append(f"{view.team_label(partner)} is in the playoff race with you; their odds change is in the table.")
    avg_give = sum(p["avg"] for p in give)
    avg_get = sum(p["avg"] for p in get)
    return {
        "trade": f"You give {', '.join(p['name'] for p in give) or 'nothing'}; you get {', '.join(p['name'] for p in get) or 'nothing'} "
                 f"({view.team_label(partner)})",
        "verdict": verdict,
        "your_odds": {"measure": my_obj, "worst_change": min(my_changes), "best_change": max(my_changes), "by_test": mine},
        "their_odds": {"measure": th_obj, "worst_change": min(th_changes), "best_change": max(th_changes), "by_test": theirs},
        "starting_lineup_change_per_week": lineup,
        "your_lineup_by_week": lineup_by_week(world, team_plus(world, me), after[me]),
        "how_it_looks_in_the_app": {
            "they_get_season_avg": rnd(avg_give, 1), "they_give_season_avg": rnd(avg_get, 1),
            "note": "Managers mostly judge by season averages and names; ESPN projections are the better predictor.",
        },
        "you_get": [_player_card(world, p) for p in get],
        "you_give": [_player_card(world, p) for p in give],
        "roster_check": {"you": roster_check(world, me, give, get), "them": roster_check(world, partner, get, give)},
        "notes": notes,
        "method": f"{sims} simulated seasons per stress test, same random draws before and after; "
                  "'measure' is playoff odds while a team is under 90% to make it, otherwise title odds. "
                  "Every team can fill a hole with the best free agent at that position (shown as '(free agent)').",
    }


# ---- search ---------------------------------------------------------------------------
MODES = ("proj", "avg")


class _Scorer:
    """Fast starting-lineup scores for the trade search. Same greedy and tie-breaking as
    ``ffmodel.best_lineup`` / ``FootballWorld.lineup_score``: each roster is ranked once per
    week and view, then a trade only removes and inserts rows."""

    def __init__(self, world: FootballWorld, tids):
        view = world.view
        self.world = world
        order = sorted(((sid, n) for sid, n in view.slot_counts.items() if view.sport.is_starting_slot(sid)),
                       key=lambda x: (SLOT_SPECIFICITY.get(x[0], 3), x[0]))
        self.slots = [sid for sid, n in order for _ in range(n)]
        self.reg, self.po = list(world.reg_weeks), list(world.po_weeks)
        self.weeks = self.reg + self.po
        self._out: dict = {}
        self.ranked = {}
        for tid in tids:
            team = team_plus(world, tid)
            for mode in MODES:
                for w in self.weeks:
                    rows = [(world.wv(p, w, mode), p["id"], p["slots"]) for p in team]
                    rows.sort(key=lambda r: -r[0])
                    self.ranked[(tid, mode, w)] = rows

    def _rows(self, tid, mode, w, out_ids):
        if not out_ids:
            return self.ranked[(tid, mode, w)]
        key = (tid, mode, w, out_ids)
        rows = self._out.get(key)
        if rows is None:
            if len(self._out) > 50000:
                self._out.clear()
            rows = self._out[key] = [r for r in self.ranked[(tid, mode, w)] if r[1] not in out_ids]
        return rows

    def _week(self, tid, mode, w, out_ids, add, starters=None) -> float:
        rows = self._rows(tid, mode, w, out_ids)
        if add:
            rows = list(rows)
        for p in add:
            v = self.world.wv(p, w, mode)
            i = next((j for j, r in enumerate(rows) if r[0] < v), len(rows))
            rows.insert(i, (v, p["id"], p["slots"]))
        used: set = set()
        total = 0.0
        fill = None
        for sid in self.slots:
            pick = next(((v, pid) for v, pid, sl in rows if sid in sl and pid not in used), None)
            if pick is None or pick[0] <= 0:   # empty slot: the team picks up a free agent
                fill = fill or self.world.hole_filler(w, mode)
                fv = fill(sid)[0]
                if fv > (pick[0] if pick else 0.0):
                    total += fv
                    continue
            if pick is None:
                continue
            used.add(pick[1])
            total += pick[0]
        if starters is not None:
            starters |= used
        return total

    def score(self, tid, mode, out_ids=frozenset(), add=(), starters=None) -> float:
        """Same as FootballWorld.lineup_score for (team - out_ids + add)."""
        reg = sum(self._week(tid, mode, w, out_ids, add, starters) for w in self.reg) / len(self.reg) if self.reg else 0.0
        po = sum(self._week(tid, mode, w, out_ids, add, starters) for w in self.po) / len(self.po) if self.po else 0.0
        if not self.reg:
            return po
        return 0.6 * reg + 0.4 * po if self.po else reg


def _relevant(world: FootballWorld, sc: _Scorer, tid, cap: int = 10) -> list[dict]:
    """Players who matter in a trade: anyone who starts for this team in some remaining week
    (either view), then the best bench values. Kickers are left out."""
    starters: set = set()
    for mode in MODES:
        sc.score(tid, mode, starters=starters)
    pool = [p for p in world.team(tid) if p["pos"] != "K"]
    pool.sort(key=lambda p: (p["id"] in starters, p["base"]), reverse=True)
    return pool[:cap]


def _all_start(sc: _Scorer, tid, out_ids, add) -> bool:
    """Does every incoming player start for ``tid`` in some remaining week (either view)?"""
    starters: set = set()
    for mode in MODES:
        sc.score(tid, mode, out_ids, add, starters=starters)
    return all(p["id"] in starters for p in add)


TIER2_POOL = 9   # 3-player deals: each side's 9 most trade-relevant players (starters first)


def find_trades(world: FootballWorld, me, partners: list, must_get: list[dict] = (), must_give: list[dict] = (),
                max_size: int = 3, budget_s: float = 22.0, top: int = 4, sims: int = 1500) -> dict:
    """Search trades with every partner, starters only. Tier 1 = every 1-for-1 up to 2-for-2 with
    every partner; tier 2 = 3-player deals, most promising partners first, within the time budget.
    A deal is kept only if it helps your starting lineup under both views and every player in it
    starts for the team that receives him (no bench throw-ins)."""
    view = world.view
    t0 = time.monotonic()
    sc = _Scorer(world, [me] + list(partners))
    lb = {(t, m): sc.score(t, m) for t in [me] + list(partners) for m in MODES}
    mine = _relevant(world, sc, me)
    mine += [p for p in must_give if p["id"] not in {q["id"] for q in mine}]
    mg, mr = {p["id"] for p in must_give}, {p["id"] for p in must_get}
    found: list[dict] = []
    coverage: dict[str, list[str]] = {}
    tiers = [[s for s in SIZES if max(s) <= min(2, max_size)], [s for s in SIZES if max(s) == 3 and max_size >= 3]]
    tiers = [t for t in tiers if t]
    theirs_by = {}
    for partner in partners:
        th = _relevant(world, sc, partner)
        theirs_by[partner] = th + [p for p in must_get if p["owner"] == partner and p["id"] not in {q["id"] for q in th}]
    out_of_time = False
    for tier_i, tier in enumerate(tiers):
        order = list(partners) if tier_i == 0 else sorted(
            partners, key=lambda t: -max([r["score"] for r in found if r["partner"] == t] or [-99]))
        for partner in order:
            if time.monotonic() - t0 > budget_s:
                out_of_time = True
                coverage.setdefault(view.team_label(partner), []).append(
                    ", ".join(f"{g}-for-{r}" for g, r in tier) + " (skipped; out of time)")
                continue
            th = theirs_by[partner]
            pool_me = mine
            if tier_i == 1:   # bigger deals: the most relevant players on each side (plus required ones)
                pool_me = mine[:TIER2_POOL] + [p for p in mine[TIER2_POOL:] if p["id"] in mg]
                th = th[:TIER2_POOL] + [p for p in th[TIER2_POOL:] if p["id"] in mr]
            done = True
            for g, r in tier:
                for give in itertools.combinations(pool_me, g):
                    gi = frozenset(p["id"] for p in give)
                    if mg and not mg <= gi:
                        continue
                    for get in itertools.combinations(th, r):
                        ri = frozenset(p["id"] for p in get)
                        if mr and not mr <= ri:
                            continue
                        dm = sc.score(me, "proj", gi, get) - lb[(me, "proj")]
                        if dm <= MIN_GAIN:
                            continue
                        dma = sc.score(me, "avg", gi, get) - lb[(me, "avg")]
                        if dma <= MIN_GAIN:
                            continue
                        dt = sc.score(partner, "proj", ri, give) - lb[(partner, "proj")]
                        dta = sc.score(partner, "avg", ri, give) - lb[(partner, "avg")]
                        if min(dt, dta) <= -1.0:
                            continue   # clearly costs them: not an offer they'd take
                        if not (_all_start(sc, me, gi, get) and _all_start(sc, partner, ri, give)):
                            continue   # someone in the deal would sit on a bench: a throw-in
                        found.append({"partner": partner, "give": list(give), "get": list(get),
                                      "me": (dm, dma), "them": (dt, dta),
                                      "win_win": min(dt, dta) > MIN_GAIN,
                                      "score": min(dm, dma, dt, dta)})
                    if time.monotonic() - t0 > budget_s:
                        done = False
                        break
                if not done:
                    break
            label = ", ".join(f"{g}-for-{r}" for g, r in tier) + (f" (top {TIER2_POOL} players a side)" if tier_i == 1 else "")
            coverage.setdefault(view.team_label(partner), []).append(label if done else label + " (partly; out of time)")
            if not done:
                out_of_time = True

    # Prefer the simplest version of a deal: a bigger deal that contains a kept smaller one
    # survives only if it's clearly better for both sides.
    found.sort(key=lambda r: (len(r["give"]) + len(r["get"]), -r["score"]))
    kept: list[dict] = []
    for r in found:
        gi, ri = {p["id"] for p in r["give"]}, {p["id"] for p in r["get"]}
        if any(k["partner"] == r["partner"] and {p["id"] for p in k["give"]} <= gi and {p["id"] for p in k["get"]} <= ri
               and r["score"] < k["score"] + 0.5 for k in kept):
            continue
        kept.append(r)
    kept.sort(key=lambda r: (-r["win_win"], -r["score"]))
    win_win = [r for r in kept if r["win_win"]]
    near = sorted((r for r in kept if not r["win_win"]), key=lambda r: (-min(r["me"]), -min(r["them"])))

    def diverse(rows, n, chosen):
        out = []
        for r in rows:
            if len(out) >= n:
                break
            core = ({p["id"] for p in r["give"]}, {p["id"] for p in r["get"]})
            if any(c["partner"] == r["partner"] and core[0] & {p["id"] for p in c["give"]}
                   and core[1] & {p["id"] for p in c["get"]} for c in chosen + out):
                continue   # same core swap as one already listed
            out.append(r)
        return out

    shortlist = diverse(win_win, 2 * top, [])
    shortlist += diverse(near, 2 * top - len(shortlist), shortlist)

    results, evaluated = [], set()
    for i, r in enumerate(shortlist):
        if i >= top and time.monotonic() - t0 > budget_s + 10:
            break   # the first `top` are always checked; extra candidates only while there's time
        ev = evaluate_trade(world, me, r["partner"], r["give"], r["get"], sims=sims)
        evaluated.add(id(r))
        results.append({
            "partner": view.team_label(r["partner"]),
            "you_give": [p["name"] for p in r["give"]],
            "you_get": [p["name"] for p in r["get"]],
            "type": kind(ev["verdict"]),
            "verdict": ev["verdict"],
            "your_odds_change": f"{ev['your_odds']['worst_change']:+.1f} to {ev['your_odds']['best_change']:+.1f} pts of "
                                f"{ev['your_odds']['measure']} odds across stress tests",
            "their_odds_change": f"{ev['their_odds']['worst_change']:+.1f} to {ev['their_odds']['best_change']:+.1f} pts of "
                                 f"{ev['their_odds']['measure']} odds",
            "starting_lineup_change_per_week": ev["starting_lineup_change_per_week"],
            "roster_check": ev["roster_check"],
            "notes": ev["notes"],
            "_worst": ev["your_odds"]["worst_change"],
        })
    results = rank_results(results, top)
    more = [{"partner": view.team_label(r["partner"]), "you_give": [p["name"] for p in r["give"]],
             "you_get": [p["name"] for p in r["get"]], "weekly_lineups": "both improve" if r["win_win"] else "you improve, about even for them",
             "lineup_change_you": f"{r['me'][0]:+.1f} / {r['me'][1]:+.1f}",
             "lineup_change_them": f"{r['them'][0]:+.1f} / {r['them'][1]:+.1f}"}
            for r in (win_win + near) if id(r) not in evaluated][:8]
    return {
        "best_trades": results,
        "also_worth_a_look": more,
        "searched": {"partners": coverage, "deals_that_help_you": len(kept), "win_win_found": len(win_win),
                     "seconds": round(time.monotonic() - t0, 1), "complete": not out_of_time},
        "how_to_read": "Starters only: every player in a deal starts for the team that gets him, and lineup "
                       "changes are points a week (by ESPN projections / by season averages). 'win-win' = both "
                       "starting lineups improve under both views. Best trades are ranked by your worst case "
                       "across the stress tests. Each is judged on its own; overlapping deals can't all be made.",
    }
