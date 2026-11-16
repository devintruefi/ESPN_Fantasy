"""League memory and track record (pure: no network).

- ``league_intel``: every manager's situation (playoff odds), activity, trade block,
  positional needs and surplus, recent moves, trade history and FAAB bids, plus the
  teams whose needs fit yours best (trade targets).
- ``football_track_record``: how well the win odds this project uses have predicted this
  league's matchups (Brier score, calibration buckets) and an audit of your lineups:
  points scored vs the best lineup by ESPN projections at the time and in hindsight.
- ``category_track_record``: category win rates for every team from completed matchups.
"""

from __future__ import annotations

import math
import statistics as st
from collections import Counter, defaultdict

from .views import LeagueView, compact, ms_to_dt, num, player_of, rnd

SITUATIONS = ((0.75, "contender"), (0.25, "bubble"), (0.0, "long shot"))


def _situation(p: float | None) -> str | None:
    if p is None:
        return None
    return next(label for cut, label in SITUATIONS if p >= cut)


def _name(names: dict, pid) -> str:
    return names.get(pid) or f"player {pid}"


# ---- transactions -------------------------------------------------------------------
def parse_transactions(raws: list[dict]) -> tuple[list[dict], dict]:
    """(executed adds, drops and trades from every period, de-duplicated, oldest first;
    every transaction by id, any status: an accepted trade's players are on its proposal)."""
    by_id: dict = {}
    for raw in raws:
        for tx in raw.get("transactions") or []:
            by_id.setdefault(tx.get("id"), tx)
    out = [tx for tx in by_id.values() if tx.get("status") == "EXECUTED"]
    out.sort(key=lambda t: t.get("processDate") or t.get("proposedDate") or 0)
    return out, by_id


def _trade_items(tx: dict, by_id: dict) -> list[dict]:
    items = [it for it in tx.get("items") or [] if it.get("type") == "TRADE"]
    if not items:
        related = by_id.get(tx.get("relatedTransactionId")) or {}
        items = [it for it in related.get("items") or [] if it.get("type") == "TRADE"]
    return items


def roster_trades(view: LeagueView) -> list[dict]:
    """Trades rebuilt from rosters: ESPN stamps every player who arrived by trade with the
    trade's time, so players sharing a stamp were one deal. Players since dropped don't show."""
    groups: dict[int, dict[int, list[str]]] = defaultdict(lambda: defaultdict(list))
    for tid in view.teams:
        for e in view.roster_entries(tid):
            if e.get("acquisitionType") == "TRADE" and e.get("acquisitionDate"):
                groups[e["acquisitionDate"]][tid].append(player_of(e).get("fullName") or f"player {e.get('playerId')}")
    rows = []
    for stamp, sides in sorted(groups.items()):
        when = ms_to_dt(stamp, view.pro.now.tzinfo)
        rows.append({"date": f"{when:%b %d}" if when else None, "_ms": stamp,
                     "gets": {view.team_label(t): sorted(n) for t, n in sides.items()}, "_teams": set(sides)})
    return rows


def _trades(view: LeagueView, txs: list[dict], by_id: dict, names: dict) -> list[dict]:
    rows = []
    for tx in txs:
        if tx.get("type") not in ("TRADE_ACCEPT", "TRADE_UPHOLD"):
            continue
        gets: dict[str, list[str]] = defaultdict(list)
        for it in _trade_items(tx, by_id):
            gets[view.team_label(it.get("toTeamId"))].append(_name(names, it.get("playerId")))
        if not gets:
            teams = {view.team_label(tx.get("teamId"))} if tx.get("teamId") else set()
            gets = {t: ["(players not in ESPN's activity log)"] for t in teams}
        ms = tx.get("processDate") or tx.get("proposedDate")
        when = ms_to_dt(ms, view.pro.now.tzinfo)
        rows.append({"date": f"{when:%b %d}" if when else None, "_ms": ms, "gets": dict(gets)})
    return rows


def faab_bids(view: LeagueView, txs: list[dict], names: dict) -> list[dict]:
    """Winning FAAB claims: who paid what for whom."""
    if not view.uses_faab:
        return []
    out = []
    for tx in txs:
        if tx.get("type") != "WAIVER":
            continue
        adds = [it.get("playerId") for it in tx.get("items") or [] if it.get("type") == "ADD"]
        if adds:
            out.append({"period": tx.get("scoringPeriodId"), "team": view.team_label(tx.get("teamId")),
                        "player": _name(names, adds[0]), "bid": num(tx.get("bidAmount"))})
    return out


def faab_summary(bids: list[dict], budget: float) -> dict | None:
    if not bids:
        return None
    amounts = sorted(b["bid"] for b in bids)
    big = sorted(bids, key=lambda b: -b["bid"])[:5]
    q = lambda f: amounts[min(len(amounts) - 1, int(f * len(amounts)))]  # noqa: E731
    return compact({
        "winning_claims": len(amounts),
        "won_for_0_or_1": sum(a <= 1 for a in amounts),
        "median_winning_bid": q(0.5),
        "75th_percentile": q(0.75),
        "90th_percentile": q(0.9),
        "share_of_budget_median": f"{100 * q(0.5) / budget:.0f}%" if budget else None,
        "biggest": [f"{b['player']} ${b['bid']:g} ({b['team']}, period {b['period']})" for b in big if b["bid"] > 0],
    })


# ---- needs and surplus ------------------------------------------------------------------
def football_needs(world) -> dict[int, dict]:
    """Per team: starter strength by position (rank 1 = best), needs (bottom third) and
    surplus (healthy bench players who'd start for a typical team)."""
    from .ffmodel import best_lineup

    view = world.view
    teams = list(view.teams)
    pos_order = ["QB", "RB", "WR", "TE", "D/ST"]
    strength: dict[int, dict[str, float]] = {}
    starters: dict[int, set] = {}
    last_starter: dict[str, list[float]] = defaultdict(list)
    for t in teams:
        team = [p for p in world.team(t) if p["status"] != "INJURY_RESERVE" and not p["in_ir"]]
        _, lineup = best_lineup(view, team, view.scoring_period, value=lambda p: p["base"])
        names = {n for _, n, _ in lineup}
        starters[t] = names
        by_pos: dict[str, list[float]] = defaultdict(list)
        for p in team:
            if p["name"] in names:
                by_pos[p["pos"]].append(p["base"])
        strength[t] = {pos: sum(by_pos.get(pos, [])) for pos in pos_order}
        for pos, vals in by_pos.items():
            last_starter[pos].append(min(vals))
    bar = {pos: st.median(v) for pos, v in last_starter.items() if v}
    out = {}
    n = len(teams)
    for t in teams:
        ranks = {pos: 1 + sum(strength[o][pos] > strength[t][pos] for o in teams) for pos in pos_order}
        need = [pos for pos in pos_order if ranks[pos] > n - n // 3]
        surplus = sorted((p for p in world.team(t) if p["name"] not in starters[t] and not p["in_ir"]
                          and p["pos"] in bar and p["pos"] != "K" and p["status"] not in ("INJURY_RESERVE", "OUT")
                          and p["base"] >= bar[p["pos"]]), key=lambda p: -p["base"])
        out[t] = {"position_rank": ranks, "needs": need,
                  "surplus": [f"{p['name']} ({p['pos']}, {p['base']:.1f})" for p in surplus[:3]],
                  "_surplus_pos": {p["pos"] for p in surplus[:3]}}
    return out


def category_needs(model, teams) -> dict[int, dict]:
    """Per team: weakest and strongest categories by average win chance against every team."""
    out = {}
    weeks = {t: model.week(model.roster(t, include_injured=True)) for t in teams}
    for t in teams:
        odds = {c: st.mean(model.cat_probs(weeks[t], weeks[b])[c] for b in teams if b != t) for c in model.cats}
        order = sorted(odds, key=odds.get)
        out[t] = {"category_odds": {c: round(100 * odds[c]) for c in model.cats},
                  "needs": order[:3], "strengths": order[-3:][::-1], "_surplus_pos": set(order[-3:])}
    return out


def _fit_score(mine: dict, theirs: dict) -> float:
    """How well two teams complement each other: their edge where you're weak plus your
    edge where they're weak (position ranks in football, category odds in basketball)."""
    if "category_odds" in mine:
        a, b = mine["category_odds"], theirs["category_odds"]
        return (sum(max(0, b[c] - a[c]) for c in mine["needs"]) + sum(max(0, a[c] - b[c]) for c in theirs["needs"]))
    a, b = mine["position_rank"], theirs["position_rank"]
    return (sum(max(0, a[p] - b[p]) for p in mine["needs"]) + sum(max(0, b[p] - a[p]) for p in theirs["needs"]))


# ---- the report -----------------------------------------------------------------------
def league_intel(view: LeagueView, me, tx_raws: list[dict], names: dict, odds: dict | None = None,
                 needs: dict | None = None, periods_covered: str = "") -> dict:
    txs, by_id = parse_transactions(tx_raws)
    rebuilt = roster_trades(view)
    trades = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rebuilt]
    day = 36 * 3600 * 1000
    for t in _trades(view, txs, by_id, names):   # trades whose players have all left the rosters
        ms = t.pop("_ms", None)
        if not any(ms and abs(ms - r["_ms"]) < day for r in rebuilt) and not any(
                "(players not in" in str(v) for v in t["gets"].values()):
            trades.append(t)
    bids = faab_bids(view, txs, names)
    recent_cut = view.scoring_period - (2 if view.is_football else 7)
    recent: dict[int, list[str]] = defaultdict(list)
    for tx in txs:
        if tx.get("type") in ("FREEAGENT", "WAIVER") and (tx.get("scoringPeriodId") or 0) >= recent_cut:
            for it in tx.get("items") or []:
                if it.get("type") == "ADD":
                    recent[tx.get("teamId")].append("+" + _name(names, it.get("playerId")))
                elif it.get("type") == "DROP":
                    recent[tx.get("teamId")].append("-" + _name(names, it.get("playerId")))
    partners: dict[int, Counter] = defaultdict(Counter)
    deals = [r["_teams"] for r in rebuilt]
    for tx in txs:
        if tx.get("type") in ("TRADE_ACCEPT", "TRADE_UPHOLD"):
            sides = {it.get("toTeamId") for it in _trade_items(tx, by_id)}
            if sides and sides not in deals:
                deals.append(sides)
    for sides in deals:
        for a in sides:
            for b in sides - {a}:
                partners[a][view.team_label(b)] += 1
    remaining_vs_me = defaultdict(list)
    for g in view.core.get("schedule") or []:
        mp = g.get("matchupPeriodId") or 0
        h, a = (g.get("home") or {}).get("teamId"), (g.get("away") or {}).get("teamId")
        if mp >= view.current_matchup_period and me in (h, a) and (g.get("winner") or "UNDECIDED") == "UNDECIDED":
            remaining_vs_me[a if h == me else h].append(mp)

    budget = view.faab_budget if view.uses_faab else 0
    rows = []
    for tid, t in view.teams.items():
        c = t.get("transactionCounter") or {}
        per = c.get("matchupAcquisitionTotals") or {}
        last3 = sum(v for k, v in per.items() if int(k) > view.current_matchup_period - 3)
        block = [_name(names, int(pid)) for pid, s in ((t.get("tradeBlock") or {}).get("players") or {}).items()
                 if s == "ON_THE_BLOCK"]
        o = (odds or {}).get(tid)
        nd = (needs or {}).get(tid) or {}
        rows.append(compact({
            "team": view.team_label(tid),
            "record": view.record_str(tid),
            "situation": _situation(o.get("playoffs")) if o else None,
            "playoffs_pct": round(100 * o["playoffs"], 1) if o else None,
            "title_pct": round(100 * o["title"], 1) if o else None,
            "moves_season": c.get("acquisitions"),
            "moves_last_3_matchups": last3,
            "trades_made": c.get("trades") or None,
            "traded_with": dict(partners[tid]) or None,
            "faab_left": (budget - num(c.get("acquisitionBudgetSpent"))) if budget else None,
            "on_trade_block": block or None,
            "needs": nd.get("needs") or None,
            "strengths" if not view.is_football else "surplus": (nd.get("strengths") if not view.is_football else nd.get("surplus")) or None,
            "position_rank": nd.get("position_rank"),
            "category_odds": nd.get("category_odds"),
            "recent_moves": recent.get(tid, [])[-6:] or None,
            "you_play_them": remaining_vs_me.get(tid) or None,
        }))
    order = {r["team"]: i for i, r in enumerate(rows)}
    rows.sort(key=lambda r: (-(r.get("playoffs_pct") or 0), order[r["team"]]))

    targets = []
    if needs and me in needs:
        my_need = set(needs[me]["needs"])
        my_extra = needs[me]["_surplus_pos"]
        for tid in view.teams:
            if tid == me or tid not in needs:
                continue
            they_have = needs[tid]["_surplus_pos"] & my_need
            they_want = set(needs[tid]["needs"]) & my_extra
            if they_have or they_want:
                o = (odds or {}).get(tid)
                targets.append(compact({
                    "team": view.team_label(tid),
                    "they_can_give_you": sorted(they_have) or None,
                    "you_can_give_them": sorted(they_want) or None,
                    "fit": "both ways" if they_have and they_want else "one way",
                    "rival_for_a_playoff_spot": True if o and 0.15 < o["playoffs"] < 0.9 else None,
                    "_score": _fit_score(needs[me], needs[tid]),
                }))
        targets.sort(key=lambda r: (r["fit"] != "both ways", bool(r.get("rival_for_a_playoff_spot")), -r["_score"]))
        targets = targets[:5]
        for r in targets:
            r.pop("_score")
    return {
        "covering": periods_covered,
        "managers": rows,
        "trade_targets": targets or None,
        "trades_this_season": trades or "none found",
        "faab": faab_summary(bids, budget) if budget else None,
        "most_active": [r["team"] for r in sorted(rows, key=lambda r: -(r.get("moves_last_3_matchups") or 0))[:3]],
        "how_to_use": "Needs/surplus come from each team's projected starters (football: position ranks, 1 = best; "
                      "basketball: category odds). Trade targets pair your needs with their surplus and vice versa; "
                      "prefer teams out of the race over playoff rivals. Trade-block players are ones the manager "
                      "flagged in ESPN.",
    }


# ---- track record (football) -------------------------------------------------------------
def _box_players(view: LeagueView, side: dict, week: int) -> list[dict]:
    out = []
    for e in (side.get("rosterForCurrentScoringPeriod") or {}).get("entries") or []:
        p = player_of(e)
        act = proj = None
        for s in p.get("stats") or []:
            if s.get("seasonId") != view.season or s.get("scoringPeriodId") != week or s.get("statSplitTypeId") != 1:
                continue
            if s.get("statSourceId") == 0:
                act = num(s.get("appliedTotal"))
            elif s.get("statSourceId") == 1:
                proj = num(s.get("appliedTotal"))
        starting = {int(x) for x in (p.get("eligibleSlots") or []) if view.sport.is_starting_slot(x)}
        out.append({"id": p.get("id"), "name": p.get("fullName"), "slots": starting & set(view.slot_counts),
                    "slot": e.get("lineupSlotId"), "actual": act or 0.0, "proj": proj or 0.0})
    return out


def _best(view: LeagueView, players: list[dict], key: str) -> tuple[float, set]:
    from .ffmodel import SLOT_SPECIFICITY

    slots = sorted(((sid, n) for sid, n in view.slot_counts.items() if view.sport.is_starting_slot(sid)),
                   key=lambda x: (SLOT_SPECIFICITY.get(x[0], 3), x[0]))
    ranked = sorted(players, key=lambda p: -p[key])
    used, names = set(), set()
    for sid, n in slots:
        for _ in range(n):
            pick = next((p for p in ranked if p["id"] not in used and sid in p["slots"]), None)
            if pick:
                used.add(pick["id"])
                names.add(pick["name"])
    return sum(p["actual"] for p in players if p["name"] in names), names


def football_track_record(view: LeagueView, me, past: dict[int, dict], sd: float, bias: float) -> dict:
    """``past`` = {matchup period: box-score scoreboard}. Win odds use starters' projections."""
    from .ffmodel import _phi

    briers, buckets = [], defaultdict(lambda: [0, 0.0, 0])   # bucket -> [n, sum p, wins]
    audit = []
    for mp, raw in sorted(past.items()):
        week = view._matchup_week(mp)
        for g in raw.get("schedule") or []:
            if g.get("matchupPeriodId") != mp:
                continue
            h, a = g.get("home") or {}, g.get("away") or {}
            if not h.get("teamId") or not a.get("teamId") or g.get("winner") in (None, "UNDECIDED"):
                continue
            ph, pa = _box_players(view, h, week), _box_players(view, a, week)
            proj_h = sum(p["proj"] for p in ph if view.sport.is_starting_slot(p["slot"]))
            proj_a = sum(p["proj"] for p in pa if view.sport.is_starting_slot(p["slot"]))
            if proj_h <= 0 or proj_a <= 0:
                continue
            p = _phi((proj_h - proj_a) / (sd * math.sqrt(2)))
            won = 1.0 if g.get("winner") == "HOME" else 0.0 if g.get("winner") == "AWAY" else 0.5
            briers.append((p - won) ** 2)
            fav_p, fav_won = (p, won) if p >= 0.5 else (1 - p, 1 - won)
            b = "50-60%" if fav_p < 0.6 else "60-70%" if fav_p < 0.7 else "70-80%" if fav_p < 0.8 else "80%+"
            buckets[b][0] += 1
            buckets[b][1] += fav_p
            buckets[b][2] += fav_won
            for side, players, opp in ((h, ph, a), (a, pa, h)):
                if side.get("teamId") != me:
                    continue
                actual = num(side.get("totalPoints"))
                opp_pts = num(opp.get("totalPoints"))
                by_proj, _ = _best(view, players, "proj")
                hindsight, best_names = _best(view, players, "actual")
                started = {p["name"] for p in players if view.sport.is_starting_slot(p["slot"])}
                audit.append(compact({
                    "week": week, "opponent": view.team_label(opp.get("teamId")),
                    "result": "W" if actual > opp_pts else "L" if actual < opp_pts else "T",
                    "score": f"{actual:.1f}-{opp_pts:.1f}",
                    "best_by_projection": rnd(by_proj, 1), "best_in_hindsight": rnd(hindsight, 1),
                    "left_on_bench": rnd(hindsight - actual, 1),
                    "should_have_started": sorted(best_names - started) or None,
                    "projection_lineup_would_flip": True if (actual < opp_pts) != (by_proj < opp_pts) else None,
                }))
    n = len(briers)
    calib = [{"favorite_odds": k, "games": v[0], "expected_wins": f"{100 * v[1] / v[0]:.0f}%",
              "actual_wins": f"{100 * v[2] / v[0]:.0f}%"} for k, v in sorted(buckets.items()) if v[0]]
    lost = [r for r in audit if r["result"] == "L"]
    return {
        "win_odds_accuracy": compact({
            "matchups_checked": n,
            "brier_score": rnd(st.mean(briers), 3) if n else None,
            "coin_flip_brier": 0.25,
            "verdict": (("Better than a coin flip" if st.mean(briers) < 0.24 else "About a coin flip")
                        + " on this league's results so far.") if n >= 6 else "Too few completed matchups to judge yet.",
            "calibration": calib or None,
        }),
        "your_lineups": audit or None,
        "your_lineup_summary": compact({
            "weeks": len(audit),
            "avg_points_left_on_bench": rnd(st.mean(r["left_on_bench"] for r in audit), 1) if audit else None,
            "avg_gap_to_projection_lineup": rnd(st.mean(
                r["best_by_projection"] - float(r["score"].split("-")[0]) for r in audit), 1) if audit else None,
            "losses_a_projection_lineup_would_have_won": sum(1 for r in lost if r.get("projection_lineup_would_flip")),
        }) if audit else None,
        "notes": [
            "Win odds here come from each side's starters' ESPN projections at the time, with this league's "
            f"weekly scoring noise (SD {sd:.0f}).",
            "'best in hindsight' is unknowable before kickoff; the useful number is the gap to the best lineup "
            "by projection, which you could have set.",
        ],
    }


def category_track_record(view: LeagueView, me) -> dict:
    """Category win rates from completed matchups (from the league's schedule)."""
    wins: dict[int, Counter] = defaultdict(Counter)
    games: dict[int, Counter] = defaultdict(Counter)
    cat_names = {str(c["id"]): c["name"] for c in view.categories}
    weekly = []
    for g in view.core.get("schedule") or []:
        if (g.get("winner") or "UNDECIDED") == "UNDECIDED":
            continue
        for side in (g.get("home") or {}, g.get("away") or {}):
            tid = side.get("teamId")
            for k, v in ((side.get("cumulativeScore") or {}).get("scoreByStat") or {}).items():
                if k in cat_names and isinstance(v, dict) and v.get("result") in ("WIN", "LOSS", "TIE"):
                    games[tid][cat_names[k]] += 1
                    wins[tid][cat_names[k]] += {"WIN": 1, "TIE": 0.5}.get(v["result"], 0)
        if me in ((g.get("home") or {}).get("teamId"), (g.get("away") or {}).get("teamId")):
            mine, opp, _ = view._side(g, me)
            c = mine.get("cumulativeScore") or {}
            weekly.append({"matchup": g.get("matchupPeriodId"), "opponent": view.team_label(opp.get("teamId")),
                           "categories": f"{c.get('wins', 0)}-{c.get('losses', 0)}-{c.get('ties', 0)}",
                           "result": "W" if g.get("winner") == ("HOME" if (g.get("home") or {}).get("teamId") == me else "AWAY")
                           else "T" if g.get("winner") == "TIE" else "L"})
    rate = {t: {c: round(100 * wins[t][c] / games[t][c]) for c in games[t]} for t in games}
    mine = rate.get(me) or {}
    rank = {c: 1 + sum(rate[t].get(c, 0) > v for t in rate if t != me) for c, v in mine.items()}
    return {
        "your_matchups": weekly or None,
        "your_category_win_rate_pct": mine or None,
        "your_rank_by_category": rank or None,
        "completed_matchups": len(weekly),
        "notes": ["Daily-lineup decisions aren't audited here (ESPN doesn't keep every day's bench); the GM "
                  "journal grades decisions and predictions week by week instead."],
    }
