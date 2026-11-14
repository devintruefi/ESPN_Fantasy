"""Football points-league brain: rest-of-season player values, weekly lineup strength,
and a Monte Carlo of the rest of the season and the playoff bracket.

What the model rests on (measured on two 12-team ESPN leagues, weeks 1-9 of 2026):
- ESPN's weekly projection beat the player's season average at every position, and
  blending in the average never helped (RMSE 7.5 vs 8.3 points per player-week).
- For weeks ESPN hasn't projected yet, the mean of a player's last few weekly
  projections predicts almost as well as the real projection (7.5 vs 7.3) and much
  better than his season average (8.1).
- Adjusting projections for the opponent's defense-vs-position record made them
  worse out of sample, so no matchup adjustment is applied.
- A team's weekly score lands within about 19-22 points (1 SD) of the sum of its
  starters' projections; projections ran 2-4 points high per team-week.

Everything here is pure (no network) so it can be tested on saved ESPN responses.
"""

from __future__ import annotations

import math
import random
import statistics as st
from typing import Any

from .views import LeagueView, compact, find_split, num, player_of, rnd

HIST_WEEKS = 3            # past weekly projections averaged into a player's value
DEFAULT_SD = 20.0         # prior for the weekly team-score noise around projections
PRIOR_N = 24              # weight of that prior, in team-weeks
NO_PROJ_RATIO = 0.95      # season average -> projection scale when no projection exists
# How many positions each football lineup slot accepts (fill the most specific first).
SLOT_SPECIFICITY = {0: 1, 2: 1, 4: 1, 6: 1, 16: 1, 17: 1, 3: 2, 5: 2, 23: 3, 7: 4}


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# ---- players ----------------------------------------------------------------------
def projection_history(view: LeagueView, past_raws: list[dict]) -> dict[int, dict[int, float]]:
    """{player id: {week: ESPN projection}} from past weeks' box scores."""
    hist: dict[int, dict[int, float]] = {}
    for raw in past_raws:
        for m in raw.get("schedule") or []:
            for side in (m.get("home") or {}, m.get("away") or {}):
                for e in (side.get("rosterForCurrentScoringPeriod") or {}).get("entries") or []:
                    p = player_of(e)
                    for s in p.get("stats") or []:
                        if (s.get("statSourceId") == 1 and s.get("statSplitTypeId") == 1
                                and s.get("seasonId") == view.season and s.get("scoringPeriodId")):
                            v = num(s.get("appliedTotal"))
                            if v > 0:
                                hist.setdefault(p.get("id"), {})[int(s["scoringPeriodId"])] = v
    return hist


def _status(view: LeagueView, wrap: dict, p: dict) -> str | None:
    return view._injury(wrap, p)


def make_player(view: LeagueView, wrap: dict, owner, hist: dict) -> dict:
    p = player_of(wrap)
    week = view.scoring_period
    pid = p.get("id")
    projs = [v for w, v in (hist.get(pid) or {}).items() if week - HIST_WEEKS <= w < week]
    cur = find_split(p, 1, 1, view.season, period=week)
    cur_proj = num((cur or {}).get("appliedTotal")) if cur else 0.0
    if cur_proj > 0:
        projs.append(cur_proj)
    season = find_split(p, 0, 0, view.season) or {}
    avg = num(season.get("appliedAverage"))
    base = st.mean(projs) if projs else avg * NO_PROJ_RATIO
    starting = {int(s) for s in (p.get("eligibleSlots") or []) if view.sport.is_starting_slot(s)}
    return {
        "id": pid,
        "name": p.get("fullName"),
        "pos": view.sport.position(p),
        "pro_id": p.get("proTeamId"),
        "nfl": view.sport.pro_team(p.get("proTeamId")),
        "slots": starting & set(view.slot_counts),
        "status": _status(view, wrap, p),
        "in_ir": wrap.get("lineupSlotId") == view.sport.ir_slot,
        "owner": owner,
        "base": base,
        "cur_proj": cur_proj,
        "avg": avg,
        "n_proj": len(projs),
    }


def availability(view: LeagueView, pl: dict, week: int) -> float:
    """Chance the player plays in `week` (bye, ESPN injury status)."""
    if view.pro.game(pl["pro_id"], week) is None:
        return 0.0
    cur = view.scoring_period
    status = pl["status"]
    if status == "INJURY_RESERVE":
        return 0.0 if week < cur + 4 else 0.4
    if status in ("OUT", "SUSPENSION"):
        return 0.0 if week == cur else 0.85
    if status == "DOUBTFUL":
        return 0.2 if week == cur else 0.9
    if status == "QUESTIONABLE":
        return 0.8 if week == cur else 0.95
    return 1.0 if week == cur else 0.97


def week_value(view: LeagueView, pl: dict, week: int) -> float:
    if view.pro.game(pl["pro_id"], week) is None:
        return 0.0
    v = pl["cur_proj"] if (week == view.scoring_period and pl["cur_proj"] > 0) else pl["base"]
    return v * availability(view, pl, week)


def best_lineup(view: LeagueView, players: list[dict], week: int, value=None) -> tuple[float, list[tuple[str, str, float]]]:
    """Expected points of the best legal lineup in `week` (greedy, most specific slot first)."""
    slots = sorted(
        ((sid, n) for sid, n in view.slot_counts.items() if view.sport.is_starting_slot(sid)),
        key=lambda x: (SLOT_SPECIFICITY.get(x[0], 3), x[0]),
    )
    value = value or (lambda p: week_value(view, p, week))
    vals = sorted(((value(p), p) for p in players), key=lambda x: -x[0])
    used: set = set()
    total, lineup = 0.0, []
    for sid, n in slots:
        for _ in range(n):
            pick = next(((v, p) for v, p in vals if p["id"] not in used and sid in p["slots"]), None)
            if pick is None:
                continue
            used.add(pick[1]["id"])
            total += pick[0]
            lineup.append((view.sport.slot(sid), pick[1]["name"], round(pick[0], 1)))
    return total, lineup


def regular_starters(view: LeagueView, players: list[dict]) -> set[str]:
    """Who starts when everyone is healthy and playing (by rest-of-season value)."""
    return {n for _, n, _ in best_lineup(view, players, view.scoring_period,
                                         value=lambda p: 0.0 if p["status"] == "INJURY_RESERVE" else p["base"])[1]}


# ---- calibration ------------------------------------------------------------------
def score_noise(view: LeagueView, past_raws: list[dict]) -> tuple[float, float, int]:
    """(SD, bias, n) of actual team scores around their starters' projections, shrunk to a prior."""
    resid = []
    for raw in past_raws:
        for m in raw.get("schedule") or []:
            for side in (m.get("home") or {}, m.get("away") or {}):
                entries = (side.get("rosterForCurrentScoringPeriod") or {}).get("entries") or []
                if not entries or side.get("totalPoints") is None:
                    continue
                week = None
                proj = 0.0
                for e in entries:
                    if not view.sport.is_starting_slot(e.get("lineupSlotId")):
                        continue
                    p = player_of(e)
                    for s in p.get("stats") or []:
                        if s.get("statSourceId") == 1 and s.get("statSplitTypeId") == 1 and s.get("seasonId") == view.season:
                            proj += num(s.get("appliedTotal"))
                            week = s.get("scoringPeriodId")
                            break
                if week and proj > 0:
                    resid.append(num(side.get("totalPoints")) - proj)
    n = len(resid)
    if n < 2:
        return DEFAULT_SD, 0.0, n
    sd = math.sqrt((n * st.pvariance(resid) + PRIOR_N * DEFAULT_SD ** 2) / (n + PRIOR_N))
    bias = st.mean(resid) * n / (n + PRIOR_N)
    return sd, bias, n


# ---- season simulation -------------------------------------------------------------
def _schedule(view: LeagueView) -> dict[int, list[tuple[int, int]]]:
    out: dict[int, list[tuple[int, int]]] = {}
    for m in view.core.get("schedule") or []:
        mp = m.get("matchupPeriodId") or 0
        h, a = (m.get("home") or {}).get("teamId"), (m.get("away") or {}).get("teamId")
        if h and a and mp <= (view.reg_season_periods or 0) and mp >= view.current_matchup_period:
            out.setdefault(mp, []).append((h, a))
    return out


def _live(view: LeagueView, live_raw: dict | None) -> dict[int, tuple[float, float]]:
    """Current matchup: {team id: (points so far, ESPN projected final)}."""
    out = {}
    for m in (live_raw or {}).get("schedule") or []:
        if m.get("matchupPeriodId") != view.current_matchup_period:
            continue
        for side in (m.get("home") or {}, m.get("away") or {}):
            tid = side.get("teamId")
            score = side.get("totalPointsLive", side.get("totalPoints"))
            proj = side.get("totalProjectedPointsLive", side.get("totalProjectedPoints"))
            if tid and proj is not None and proj >= 0:
                out[tid] = (num(score), num(proj))
    return out


def _weeks_of(view: LeagueView, mp: int) -> list[int]:
    return list(view.matchup_periods.get(mp) or [mp])


def season_outlook(view: LeagueView, live_raw: dict | None, past_raws: list[dict], pool: list[dict],
                   team_id, sims: int = 5000, seed: int = 7) -> dict:
    if not view.is_football or view.is_categories:
        raise ValueError("season_outlook is for football points leagues")
    hist = projection_history(view, past_raws)
    players = {tid: [make_player(view, e, tid, hist) for e in view.roster_entries(tid)] for tid in view.teams}
    sd, bias, n_cal = score_noise(view, past_raws)
    reg_end = view.reg_season_periods or 0
    sched = _schedule(view)
    live = _live(view, live_raw)
    cur_mp = view.current_matchup_period
    playoff_mps = sorted(mp for mp in view.matchup_periods if mp > reg_end)
    n_playoff = int(view.settings.get("scheduleSettings", {}).get("playoffTeamCount") or 0)
    weeks_needed = sorted({w for mp in list(sched) + playoff_mps for w in _weeks_of(view, mp)})
    next_week = next((w for w in weeks_needed if w > view.scoring_period), None)
    if cur_mp > reg_end:
        mine = players[team_id]
        return {
            "team": view.team_label(team_id),
            "note": "The regular season is over; this shows each remaining week's best lineup instead of odds.",
            "your_best_lineup_by_week": {
                w: [{"slot": s, "player": n, "proj": v} for s, n, v in best_lineup(view, mine, w)[1]]
                for w in weeks_needed if w >= view.scoring_period
            },
        }

    def strength(plist):
        return {w: best_lineup(view, plist, w)[0] + bias for w in weeks_needed}

    STR = {tid: strength(pl) for tid, pl in players.items()}

    def mp_mean(tid, mp, S=STR):
        return sum(S[tid].get(w, 0.0) for w in _weeks_of(view, mp))

    def mp_sd(mp):
        return sd * math.sqrt(len(_weeks_of(view, mp)))

    rec = {tid: view.record(tid) for tid in view.teams}
    base_w = {tid: num(r.get("wins")) + 0.5 * num(r.get("ties")) for tid, r in rec.items()}
    base_pf = {tid: num(r.get("pointsFor")) for tid, r in rec.items()}
    teams = list(view.teams)
    rng = random.Random(seed)
    tally = {t: {"playoffs": 0, "bye": 0, "final": 0, "title": 0, "wins": 0.0, "seed": 0} for t in teams}
    by_wins: dict[float, list[int]] = {}
    byes = 2 if n_playoff == 6 else 0
    for _ in range(sims):
        W, PF = dict(base_w), dict(base_pf)
        for mp, games in sched.items():
            for h, a in games:
                if mp == cur_mp and h in live and a in live:
                    def draw(t):
                        so_far, proj = live[t]
                        frac = max(proj - so_far, 0.0) / max(proj, 1.0)
                        return proj + bias * frac + rng.gauss(0, sd * math.sqrt(frac))
                    xh, xa = draw(h), draw(a)
                else:
                    xh, xa = rng.gauss(mp_mean(h, mp), mp_sd(mp)), rng.gauss(mp_mean(a, mp), mp_sd(mp))
                PF[h] += xh
                PF[a] += xa
                W[h if xh > xa else a] += 1
        order = sorted(teams, key=lambda t: (W[t], PF[t]), reverse=True)
        for i, t in enumerate(order):
            tally[t]["wins"] += W[t] - base_w[t]
            tally[t]["seed"] += i + 1
        made = order[:n_playoff] if n_playoff else []
        for t in made:
            tally[t]["playoffs"] += 1
        for t in made[:byes]:
            tally[t]["bye"] += 1
        by_wins.setdefault(W[team_id], [0, 0])
        by_wins[W[team_id]][1] += 1
        by_wins[W[team_id]][0] += team_id in made
        if made and playoff_mps and n_playoff in (4, 6, 8):
            def game(x, y, mp):
                return x if rng.gauss(mp_mean(x, mp), mp_sd(mp)) > rng.gauss(mp_mean(y, mp), mp_sd(mp)) else y
            rounds = playoff_mps
            seeds = list(made)
            if n_playoff == 6 and len(rounds) >= 3:
                w1, w2 = game(seeds[2], seeds[5], rounds[0]), game(seeds[3], seeds[4], rounds[0])
                low, high = sorted([w1, w2], key=seeds.index)
                f1, f2 = game(seeds[0], high, rounds[1]), game(seeds[1], low, rounds[1])
                final_mp = rounds[2]
            elif n_playoff == 4 and len(rounds) >= 2:
                f1, f2 = game(seeds[0], seeds[3], rounds[0]), game(seeds[1], seeds[2], rounds[0])
                final_mp = rounds[1]
            elif n_playoff == 8 and len(rounds) >= 3:
                q = [game(seeds[0], seeds[7], rounds[0]), game(seeds[3], seeds[4], rounds[0]),
                     game(seeds[1], seeds[6], rounds[0]), game(seeds[2], seeds[5], rounds[0])]
                f1, f2 = game(q[0], q[1], rounds[1]), game(q[2], q[3], rounds[1])
                final_mp = rounds[2]
            else:
                continue
            tally[f1]["final"] += 1
            tally[f2]["final"] += 1
            tally[game(f1, f2, final_mp)]["title"] += 1

    def pct(x):
        return round(100.0 * x / sims, 1)

    reg_weeks = [w for mp in sched if mp != cur_mp for w in _weeks_of(view, mp)]
    po_weeks = [w for mp in playoff_mps for w in _weeks_of(view, mp)]

    def avg_over(S, weeks):
        return rnd(st.mean(S[w] for w in weeks), 1) if weeks else None

    league = []
    for t in sorted(teams, key=lambda t: (-tally[t]["playoffs"], -tally[t]["title"])):
        league.append(compact({
            "team": view.team_label(t),
            "record": view.record_str(t),
            "points_for": rnd(base_pf[t], 1),
            "strength_rest_of_season": avg_over(STR[t], reg_weeks),
            "strength_playoff_weeks": avg_over(STR[t], po_weeks),
            "playoffs_pct": pct(tally[t]["playoffs"]),
            "bye_pct": pct(tally[t]["bye"]) if byes else None,
            "title_pct": pct(tally[t]["title"]),
            "expected_wins_left": rnd(tally[t]["wins"] / sims, 1),
        }))

    # the user's path, week by week
    my_weeks = []
    mine = players[team_id]
    core = regular_starters(view, mine)
    for mp in sorted(sched):
        game_ = next(((h, a) for h, a in sched[mp] if team_id in (h, a)), None)
        if not game_:
            continue
        opp = game_[1] if game_[0] == team_id else game_[0]
        weeks = _weeks_of(view, mp)
        if mp == cur_mp and team_id in live and opp in live:
            (ms, mpj), (os_, opj) = live[team_id], live[opp]
            fm = max(mpj - ms, 0) / max(mpj, 1)
            fo = max(opj - os_, 0) / max(opj, 1)
            mu = (mpj + bias * fm) - (opj + bias * fo)
            var = sd ** 2 * (fm + fo)
            p = _phi(mu / math.sqrt(var)) if var > 0 else float(mu > 0)
            me_v, op_v = mpj, opj
            note = {"score_now": f"{rnd(ms, 1)}-{rnd(os_, 1)}"}
        else:
            me_v, op_v = mp_mean(team_id, mp), mp_mean(opp, mp)
            p = _phi((me_v - op_v) / (mp_sd(mp) * math.sqrt(2)))
            note = {}
        bye_names = sorted({pl["name"] for pl in mine for w in weeks
                            if pl["name"] in core and view.pro.game(pl["pro_id"], w) is None})
        my_weeks.append(compact({
            "week": weeks[0] if len(weeks) == 1 else f"{weeks[0]}-{weeks[-1]}",
            "opponent": view.team_label(opp),
            "opponent_record": view.record_str(opp),
            "you": rnd(me_v, 1),
            "them": rnd(op_v, 1),
            "win_pct": round(100 * p),
            "your_players_on_bye": bye_names or None,
        } | note))

    total_games = (view.reg_season_periods or 0)
    wins_needed = []
    for w in sorted(by_wins):
        made, n = by_wins[w]
        if n / sims >= 0.01:
            losses = total_games - w
            wins_needed.append({"final_record": f"{w:g}-{losses:g}", "playoff_pct": pct(made * sims / n) if n else 0,
                                "share_of_seasons_pct": pct(n)})

    # best free-agent adds by lineup gain (no drop assumed; pick the drop from the bench)
    def lineup_avg(plist, weeks):
        return st.mean(best_lineup(view, plist, w)[0] for w in weeks) if weeks else 0.0

    base_reg, base_po = lineup_avg(mine, reg_weeks), lineup_avg(mine, po_weeks)
    my_pct = tally[team_id]["playoffs"] / sims
    w_reg = 0.4 if my_pct > 0.95 else 0.7   # a near-lock plays for the playoffs; a bubble team for now
    adds = []
    for wrap in pool:
        pl = make_player(view, wrap, "FA", hist)
        if pl["base"] < 3:
            continue
        g_reg = lineup_avg(mine + [pl], reg_weeks) - base_reg
        g_po = lineup_avg(mine + [pl], po_weeks) - base_po
        score = w_reg * g_reg + (1 - w_reg) * g_po
        if score > 0.3:
            starts = [w for w in reg_weeks + po_weeks
                      if any(n == pl["name"] for _, n, _ in best_lineup(view, mine + [pl], w)[1])]
            adds.append((score, compact({
                "name": pl["name"], "pos": pl["pos"], "team": pl["nfl"], "status": pl["status"],
                "value_per_game": rnd(pl["base"], 1),
                "lineup_gain_rest_of_season": rnd(g_reg, 1), "lineup_gain_playoffs": rnd(g_po, 1),
                "would_start_weeks": starts,
            })))
    adds.sort(key=lambda x: -x[0])

    kickers = [p["avg"] for pl in players.values() for p in pl if p["pos"] == "K" and p["avg"] > 0]
    notes = [
        "Values are ESPN's recent weekly projections (no extra matchup adjustment); injuries use ESPN statuses "
        "(IR = out about 4 weeks, OUT = misses this week). Tell the model what the news says to refine it.",
        f"Weekly scoring noise in this league: SD {sd:.0f} points per team around projections"
        f" (from {n_cal} team-weeks), projections {'high' if bias < 0 else 'low'} by {abs(bias):.1f}.",
    ]
    if kickers and st.mean(kickers) < 4:
        notes.append(f"Kickers average {st.mean(kickers):.1f} points here: don't spend pickups or FAAB on them.")
    if len(view.divisions) > 1:
        notes.append("This league has divisions; division-winner playoff spots aren't modeled.")
    if (view.settings.get("scheduleSettings") or {}).get("playoffSeedingRule") not in (None, "TOTAL_POINTS_SCORED"):
        notes.append("Seeding ties are broken by points for here (the league uses a different tiebreak).")

    return {
        "team": view.team_label(team_id),
        "your_outlook": {
            "playoffs_pct": pct(tally[team_id]["playoffs"]),
            "bye_pct": pct(tally[team_id]["bye"]) if byes else None,
            "reach_final_pct": pct(tally[team_id]["final"]),
            "title_pct": pct(tally[team_id]["title"]),
            "expected_wins_left": rnd(tally[team_id]["wins"] / sims, 1),
            "avg_final_seed": rnd(tally[team_id]["seed"] / sims, 1),
        },
        "your_weeks": my_weeks,
        "wins_needed": wins_needed,
        "your_best_lineup_next_week": [
            {"slot": s, "player": n, "proj": v} for s, n, v in best_lineup(view, mine, next_week)[1]
        ] if next_week else None,
        "best_adds": [a for _, a in adds[:8]],
        "league": league,
        "sims": sims,
        "notes": notes,
    }
