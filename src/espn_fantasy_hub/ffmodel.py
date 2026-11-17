"""Football points-league brain: rest-of-season player values, weekly lineup strength, and a
Monte Carlo of the rest of the season and the playoff bracket, run under several stress tests.

What the model rests on (measured on two 12-team ESPN leagues, weeks 1-9 of 2026):
- ESPN's weekly projection beat the player's season average at every position, and blending
  the average in never helped (RMSE 7.5 vs 8.3 points per player-week).
- For weeks ESPN hasn't projected yet, the mean of a player's recent weekly projections
  predicts almost as well as the real projection (7.5 vs 7.3) and much better than his
  season average (8.1).
- Players who beat or missed their projection early didn't keep doing it (correlation
  -0.03 to -0.28 at every position), so where projections and averages disagree we side
  with projections, and test the other view as a stress test.
- Adjusting projections for the opponent's defense-vs-position record made them worse out
  of sample, so no matchup adjustment is applied on top of ESPN's.
- A team's weekly score lands within about 19-22 points (1 SD) of the sum of its starters'
  projections; projections ran 2-4 points high per team-week. Weekly win probabilities from
  this scored a Brier of 0.22 on 108 real matchups (coin flip 0.25).

Everything here is pure (no network) so it can be tested on saved ESPN responses.
"""

from __future__ import annotations

import math
import random
import statistics as st
from typing import Any

from .views import LeagueView, compact, find_split, num, player_of, rnd

HIST_WEEKS = 3            # past weekly projections averaged into a player's value (plus this week's)
DEFAULT_SD = 20.0         # prior for the weekly team-score noise around projections
PRIOR_N = 24              # weight of that prior, in team-weeks
NO_PROJ_RATIO = 0.95      # season average -> projection scale when no projection exists
SKILL = ("QB", "RB", "WR", "TE")
# How many positions each football lineup slot accepts (fill the most specific first).
SLOT_SPECIFICITY = {0: 1, 2: 1, 4: 1, 6: 1, 16: 1, 17: 1, 3: 2, 5: 2, 23: 3, 7: 4}

# Stress tests every decision is run through. "tau" is persistent team-strength uncertainty
# (points per week, drawn once per simulated season); "sd_scale" widens weekly noise.
SCENARIOS = [
    {"key": "espn", "label": "ESPN projections", "value": "proj", "injury": "espn", "tau": 4.0, "sd_scale": 1.0},
    {"key": "noise", "label": "More randomness", "value": "proj", "injury": "espn", "tau": 8.0, "sd_scale": 1.2},
    {"key": "avg", "label": "Season averages", "value": "avg", "injury": "espn", "tau": 4.0, "sd_scale": 1.0},
    {"key": "blend", "label": "50/50 blend", "value": "blend", "injury": "espn", "tau": 4.0, "sd_scale": 1.0},
    {"key": "slow", "label": "Slower injury returns", "value": "proj", "injury": "slow", "tau": 4.0, "sd_scale": 1.0},
]


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# ---- players ----------------------------------------------------------------------
def projection_history(view: LeagueView, past_raws: list[dict]) -> dict[int, dict[int, float]]:
    """{player id: {week: ESPN projection}} from past weeks' box scores (rostered players only)."""
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
    """One player for the model. ``hist`` = {player id: {week: projected points}} (a week
    present with 0 means ESPN projected nothing, e.g. ruled out)."""
    p = player_of(wrap)
    week = view.scoring_period
    pid = p.get("id")
    by_week = dict(hist.get(pid) or {})
    cur = find_split(p, 1, 1, view.season, period=week)
    if cur is not None and week not in by_week:
        by_week[week] = num(cur.get("appliedTotal"))
    projs = [v for w, v in by_week.items() if week - HIST_WEEKS <= w <= week + 1 and v > 0]
    season = find_split(p, 0, 0, view.season) or {}
    avg = num(season.get("appliedAverage"))
    total = num(season.get("appliedTotal"))
    games = round(total / avg) if avg else 0
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
        "by_week": by_week,
        "cur_proj": by_week.get(week, 0.0),
        "avg": avg,
        "games": games,
        "n_proj": len(projs),
    }


def availability(view: LeagueView, pl: dict, week: int, injury: str = "espn") -> float:
    """Chance the player plays in `week` (bye, ESPN injury status; 'slow' = later returns)."""
    if view.pro.game(pl["pro_id"], week) is None:
        return 0.0
    cur = view.scoring_period
    status = pl["status"]
    slow = injury == "slow"
    if status == "INJURY_RESERVE":
        return 0.0 if week < cur + (8 if slow else 4) else 0.4
    if status in ("OUT", "SUSPENSION"):
        return 0.0 if week <= cur + (2 if slow else 0) else 0.85
    if status == "DOUBTFUL":
        return 0.2 if week == cur else (0.0 if slow and week == cur + 1 else 0.9)
    if status == "QUESTIONABLE":
        return 0.8 if week == cur else 0.95
    return 1.0 if week == cur else 0.97


def point_value(pl: dict, week: int, mode: str = "proj") -> float:
    """Expected points if he plays: ESPN's projection for that week when published, else his
    recent-projection mean ('proj'), his season average ('avg'), or the midpoint ('blend')."""
    proj = pl["by_week"].get(week)
    proj = proj if proj is not None else pl["base"]
    if mode == "proj" or pl["pos"] not in SKILL or pl["games"] < 3:
        return proj
    if mode == "avg":
        return pl["avg"]
    return 0.5 * (proj + pl["avg"])


def week_value(view: LeagueView, pl: dict, week: int, mode: str = "proj", injury: str = "espn") -> float:
    if view.pro.game(pl["pro_id"], week) is None:
        return 0.0
    if mode == "proj" and week in pl["by_week"] and pl["by_week"][week] == 0 and week <= view.scoring_period + 1:
        return 0.0  # ESPN projects nothing this/next week (ruled out)
    return point_value(pl, week, mode) * availability(view, pl, week, injury)


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


# ---- the world: every team, every player, the schedule --------------------------------
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


class FootballWorld:
    """Every team's roster and every relevant free agent, valued for each remaining week."""

    def __init__(self, view: LeagueView, live_raw: dict | None, past_raws: list[dict], pool: list[dict],
                 proj_points: dict[int, dict[int, float]] | None = None):
        if not view.is_football or view.is_categories:
            raise ValueError("FootballWorld needs a football points league")
        self.view = view
        self._wv: dict[tuple, float] = {}
        box = projection_history(view, past_raws)
        hist: dict[int, dict[int, float]] = {}
        for pid, weeks in box.items():
            hist[pid] = dict(weeks)
        for pid, weeks in (proj_points or {}).items():   # complete histories win over box scores
            hist.setdefault(pid, {}).update(weeks)
        self.full_history = bool(proj_points)
        self.players: dict[int, dict] = {}
        self.rosters: dict[int, list[int]] = {}
        for tid in view.teams:
            ids = []
            for e in view.roster_entries(tid):
                pl = make_player(view, e, tid, hist)
                self.players[pl["id"]] = pl
                ids.append(pl["id"])
            self.rosters[tid] = ids
        self.free_agents: list[int] = []
        for wrap in pool:
            pl = make_player(view, wrap, "FA", hist)
            if pl["id"] not in self.players:
                self.players[pl["id"]] = pl
                self.free_agents.append(pl["id"])
        # A defense's own season average barely predicts its points (r ~ 0.1 in testing), so a
        # D/ST with no projections gets the league's typical projected D/ST value instead.
        dst = [p["base"] for p in self.players.values() if p["pos"] == "D/ST" and p["n_proj"]]
        if dst:
            typical = st.median(dst)
            for p in self.players.values():
                if p["pos"] == "D/ST" and not p["n_proj"]:
                    p["base"] = typical
        self.sd, self.bias, self.n_cal = score_noise(view, past_raws)
        self.reg_end = view.reg_season_periods or 0
        self.cur_mp = view.current_matchup_period
        self.sched = self._schedule()
        self.live = _live(view, live_raw)
        self.playoff_mps = sorted(mp for mp in view.matchup_periods if mp > self.reg_end)
        self.n_playoff = int((view.settings.get("scheduleSettings") or {}).get("playoffTeamCount") or 0)
        self.weeks_all = sorted({w for mp in list(self.sched) + self.playoff_mps for w in self.weeks_of(mp)})
        self.reg_weeks = [w for mp in self.sched if mp != self.cur_mp for w in self.weeks_of(mp)]
        self.po_weeks = [w for mp in self.playoff_mps for w in self.weeks_of(mp)]

    # -- helpers
    def weeks_of(self, mp: int) -> list[int]:
        return list(self.view.matchup_periods.get(mp) or [mp])

    def _schedule(self) -> dict[int, list[tuple[int, int]]]:
        out: dict[int, list[tuple[int, int]]] = {}
        for m in self.view.core.get("schedule") or []:
            mp = m.get("matchupPeriodId") or 0
            h, a = (m.get("home") or {}).get("teamId"), (m.get("away") or {}).get("teamId")
            if h and a and self.cur_mp <= mp <= self.reg_end:
                out.setdefault(mp, []).append((h, a))
        return out

    def team(self, tid) -> list[dict]:
        return [self.players[i] for i in self.rosters.get(tid, [])]

    def by_name(self, name: str) -> dict | None:
        from .views import norm_name
        q = norm_name(name)
        hits = [p for p in self.players.values() if norm_name(p["name"] or "") == q]
        return hits[0] if len(hits) == 1 else None

    def wv(self, p: dict, week: int, mode: str = "proj", injury: str = "espn") -> float:
        """week_value, memoized (players don't change once the world is built)."""
        key = (p["id"], week, mode, injury)
        v = self._wv.get(key)
        if v is None:
            v = self._wv[key] = week_value(self.view, p, week, mode, injury)
        return v

    def lineup_total(self, plist: list[dict], week: int, mode: str = "proj", injury: str = "espn") -> float:
        return best_lineup(self.view, plist, week, value=lambda p: self.wv(p, week, mode, injury))[0]

    def lineup(self, plist: list[dict], week: int, mode: str = "proj") -> list[tuple[str, str, float]]:
        return best_lineup(self.view, plist, week, value=lambda p: self.wv(p, week, mode))[1]

    def strength(self, plist: list[dict], mode: str = "proj", injury: str = "espn") -> dict[int, float]:
        return {w: self.lineup_total(plist, w, mode, injury) + self.bias for w in self.weeks_all}

    def strengths(self, rosters: dict[int, list[dict]], mode: str = "proj", injury: str = "espn") -> dict[int, dict[int, float]]:
        return {tid: self.strength(pl, mode, injury) for tid, pl in rosters.items()}

    def lineup_score(self, plist: list[dict], mode: str = "proj") -> float:
        """Points a week of starting lineup: 60% regular season left, 40% playoff weeks."""
        reg = sum(self.lineup_total(plist, w, mode) for w in self.reg_weeks) / len(self.reg_weeks) if self.reg_weeks else 0.0
        po = sum(self.lineup_total(plist, w, mode) for w in self.po_weeks) / len(self.po_weeks) if self.po_weeks else 0.0
        if not self.reg_weeks:
            return po
        return 0.6 * reg + 0.4 * po if self.po_weeks else reg

    # -- simulation
    def simulate(self, STR: dict[int, dict[int, float]], sims: int = 4000, seed: int = 7,
                 tau: float = 0.0, sd_scale: float = 1.0, focus=None) -> dict:
        view = self.view
        sd = self.sd * sd_scale
        teams = list(view.teams)
        rec = {tid: view.record(tid) for tid in teams}
        base_w = {t: num(r.get("wins")) + 0.5 * num(r.get("ties")) for t, r in rec.items()}
        base_pf = {t: num(r.get("pointsFor")) for t, r in rec.items()}
        mp_mean = {(t, mp): sum(STR[t].get(w, 0.0) for w in self.weeks_of(mp))
                   for t in teams for mp in list(self.sched) + self.playoff_mps}
        mp_sd = {mp: sd * math.sqrt(len(self.weeks_of(mp))) for mp in list(self.sched) + self.playoff_mps}
        rng = random.Random(seed)
        tally = {t: {"playoffs": 0, "bye": 0, "final": 0, "title": 0, "wins": 0.0, "seed": 0} for t in teams}
        by_wins: dict[float, list[int]] = {}
        byes = 2 if self.n_playoff == 6 else 0
        gauss = rng.gauss
        for _ in range(sims):
            shock = {t: gauss(0, tau) for t in teams} if tau else {t: 0.0 for t in teams}
            W, PF = dict(base_w), dict(base_pf)
            for mp, games in self.sched.items():
                for h, a in games:
                    if mp == self.cur_mp and h in self.live and a in self.live:
                        xs = []
                        for t in (h, a):
                            so_far, proj = self.live[t]
                            frac = max(proj - so_far, 0.0) / max(proj, 1.0)
                            xs.append(proj + self.bias * frac + gauss(0, sd * math.sqrt(frac)))
                        xh, xa = xs
                    else:
                        xh = gauss(mp_mean[(h, mp)] + shock[h], mp_sd[mp])
                        xa = gauss(mp_mean[(a, mp)] + shock[a], mp_sd[mp])
                    PF[h] += xh
                    PF[a] += xa
                    W[h if xh > xa else a] += 1
            order = sorted(teams, key=lambda t: (W[t], PF[t]), reverse=True)
            for i, t in enumerate(order):
                tally[t]["wins"] += W[t] - base_w[t]
                tally[t]["seed"] += i + 1
            made = order[:self.n_playoff] if self.n_playoff else []
            for t in made:
                tally[t]["playoffs"] += 1
            for t in made[:byes]:
                tally[t]["bye"] += 1
            if focus is not None:
                slot = by_wins.setdefault(W[focus], [0, 0])
                slot[1] += 1
                slot[0] += focus in made
            if not (made and self.playoff_mps and self.n_playoff in (4, 6, 8)):
                continue

            def game(x, y, mp):
                return x if gauss(mp_mean[(x, mp)] + shock[x], mp_sd[mp]) > gauss(mp_mean[(y, mp)] + shock[y], mp_sd[mp]) else y

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
        out = {t: {k: (v / sims if k not in ("wins", "seed") else v / sims) for k, v in r.items()} for t, r in tally.items()}
        return {"teams": out, "by_wins": by_wins, "sims": sims, "has_bye": bool(byes)}

    def stress_test(self, rosters: dict[int, list[dict]], sims: int = 3000, scenarios=None, focus=None) -> dict[str, dict]:
        """Season odds for every team under every stress test (common random numbers)."""
        out = {}
        for sc in scenarios or SCENARIOS:
            STR = self.strengths(rosters, sc["value"], sc["injury"])
            out[sc["key"]] = self.simulate(STR, sims=sims, seed=7, tau=sc["tau"], sd_scale=sc["sd_scale"], focus=focus)
        return out

    def all_rosters(self) -> dict[int, list[dict]]:
        return {t: self.team(t) for t in self.view.teams}


def objective(odds: dict) -> str:
    """What a team should be optimizing: playoffs while they're in doubt, then the title."""
    return "title" if odds.get("playoffs", 0) >= 0.9 else "playoffs"


# ---- season outlook (the user-facing summary) ---------------------------------------
def season_outlook(view: LeagueView, live_raw: dict | None, past_raws: list[dict], pool: list[dict],
                   team_id, sims: int = 5000, seed: int = 7, proj_points: dict | None = None,
                   world: FootballWorld | None = None) -> dict:
    if not view.is_football or view.is_categories:
        raise ValueError("season_outlook is for football points leagues")
    world = world or FootballWorld(view, live_raw, past_raws, pool, proj_points)
    mine = world.team(team_id)
    next_week = next((w for w in world.weeks_all if w > view.scoring_period), None)
    if world.cur_mp > world.reg_end:
        return {
            "team": view.team_label(team_id),
            "note": "The regular season is over; this shows each remaining week's best lineup instead of odds.",
            "your_best_lineup_by_week": {
                w: [{"slot": s, "player": n, "proj": v} for s, n, v in best_lineup(view, mine, w)[1]]
                for w in world.weeks_all if w >= view.scoring_period
            },
        }
    rosters = world.all_rosters()
    STR = world.strengths(rosters)
    res = world.simulate(STR, sims=sims, seed=seed, tau=4.0, focus=team_id)
    tally, by_wins = res["teams"], res["by_wins"]
    byes = res["has_bye"]

    def pct(x):
        return round(100.0 * x, 1)

    def avg_over(S, weeks):
        return rnd(st.mean(S[w] for w in weeks), 1) if weeks else None

    league = []
    for t in sorted(view.teams, key=lambda t: (-tally[t]["playoffs"], -tally[t]["title"])):
        rec = view.record(t)
        league.append(compact({
            "team": view.team_label(t),
            "record": view.record_str(t),
            "points_for": rnd(num(rec.get("pointsFor")), 1),
            "strength_rest_of_season": avg_over(STR[t], world.reg_weeks),
            "strength_playoff_weeks": avg_over(STR[t], world.po_weeks),
            "playoffs_pct": pct(tally[t]["playoffs"]),
            "bye_pct": pct(tally[t]["bye"]) if byes else None,
            "title_pct": pct(tally[t]["title"]),
            "expected_wins_left": rnd(tally[t]["wins"], 1),
        }))

    # stress tests for the user's own odds
    stress = world.stress_test(rosters, sims=max(1500, sims // 3))
    # the range always contains the headline number (separate runs differ by sampling noise)
    lo = {k: min([stress[s["key"]]["teams"][team_id][k] for s in SCENARIOS] + [tally[team_id][k]])
          for k in ("playoffs", "bye", "title")}
    hi = {k: max([stress[s["key"]]["teams"][team_id][k] for s in SCENARIOS] + [tally[team_id][k]])
          for k in ("playoffs", "bye", "title")}
    by_test = [compact({"test": s["label"], "playoffs_pct": pct(stress[s["key"]]["teams"][team_id]["playoffs"]),
                        "bye_pct": pct(stress[s["key"]]["teams"][team_id]["bye"]) if byes else None,
                        "title_pct": pct(stress[s["key"]]["teams"][team_id]["title"])}) for s in SCENARIOS]

    # the user's path, week by week
    my_weeks = []
    core = regular_starters(view, mine)
    for mp in sorted(world.sched):
        game_ = next(((h, a) for h, a in world.sched[mp] if team_id in (h, a)), None)
        if not game_:
            continue
        opp = game_[1] if game_[0] == team_id else game_[0]
        weeks = world.weeks_of(mp)
        if mp == world.cur_mp and team_id in world.live and opp in world.live:
            (ms, mpj), (os_, opj) = world.live[team_id], world.live[opp]
            fm = max(mpj - ms, 0) / max(mpj, 1)
            fo = max(opj - os_, 0) / max(opj, 1)
            mu = (mpj + world.bias * fm) - (opj + world.bias * fo)
            var = world.sd ** 2 * (fm + fo)
            p = _phi(mu / math.sqrt(var)) if var > 0 else float(mu > 0)
            me_v, op_v = mpj, opj
            note = {"score_now": f"{rnd(ms, 1)}-{rnd(os_, 1)}"}
        else:
            me_v = sum(STR[team_id][w] for w in weeks)
            op_v = sum(STR[opp][w] for w in weeks)
            p = _phi((me_v - op_v) / (world.sd * math.sqrt(2 * len(weeks))))
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

    total_games = view.reg_season_periods or 0
    wins_needed = []
    for w in sorted(by_wins):
        made, n = by_wins[w]
        if n / sims >= 0.01:
            wins_needed.append({"final_record": f"{w:g}-{total_games - w:g}", "playoff_pct": pct(made / n) if n else 0,
                                "share_of_seasons_pct": pct(n / sims)})

    # best free-agent adds by lineup gain (no drop assumed; pick the drop from the bench)
    def lineup_avg(plist, weeks):
        return st.mean(world.lineup_total(plist, w) for w in weeks) if weeks else 0.0

    base_reg, base_po = lineup_avg(mine, world.reg_weeks), lineup_avg(mine, world.po_weeks)
    w_reg = 0.4 if tally[team_id]["playoffs"] > 0.95 else 0.7   # a near-lock plays for the playoffs
    adds = []
    for pid in world.free_agents:
        pl = world.players[pid]
        if pl["base"] < 3:
            continue
        g_reg = lineup_avg(mine + [pl], world.reg_weeks) - base_reg
        g_po = lineup_avg(mine + [pl], world.po_weeks) - base_po
        score = w_reg * g_reg + (1 - w_reg) * g_po
        if score > 0.3:
            starts = [w for w in world.reg_weeks + world.po_weeks
                      if any(n == pl["name"] for _, n, _ in best_lineup(view, mine + [pl], w)[1])]
            adds.append((score, compact({
                "name": pl["name"], "pos": pl["pos"], "team": pl["nfl"], "status": pl["status"],
                "value_per_game": rnd(pl["base"], 1),
                "lineup_gain_rest_of_season": rnd(g_reg, 1), "lineup_gain_playoffs": rnd(g_po, 1),
                "would_start_weeks": starts,
            })))
    adds.sort(key=lambda x: -x[0])

    kickers = [p["avg"] for t in view.teams for p in world.team(t) if p["pos"] == "K" and p["avg"] > 0]
    notes = [
        "Values are ESPN's weekly projections (this week's and next week's when published, otherwise the mean "
        "of recent ones; no extra matchup adjustment)" + ("" if world.full_history else
        " (box-score history only: ESPN's player feed was unavailable)") + ". Injuries use ESPN statuses "
        "(IR = out about 4 weeks, OUT = misses this week). Tell the model what the news says to refine it.",
        f"Weekly scoring noise in this league: SD {world.sd:.0f} points per team around projections"
        f" (from {world.n_cal} team-weeks), projections {'high' if world.bias < 0 else 'low'} by {abs(world.bias):.1f}.",
        "Odds include uncertainty about each team's true strength; the stress test range re-runs them with season "
        "averages, a 50/50 blend, more randomness and slower injury returns.",
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
            "expected_wins_left": rnd(tally[team_id]["wins"], 1),
            "avg_final_seed": rnd(tally[team_id]["seed"], 1),
            "stress_test_range": compact({"playoffs_pct": f"{pct(lo['playoffs'])}-{pct(hi['playoffs'])}",
                                          "bye_pct": f"{pct(lo['bye'])}-{pct(hi['bye'])}" if byes else None,
                                          "title_pct": f"{pct(lo['title'])}-{pct(hi['title'])}"}),
        },
        "stress_tests": by_test,
        "your_weeks": my_weeks,
        "wins_needed": wins_needed,
        "your_best_lineup_next_week": [
            {"slot": s, "player": n, "proj": v} for s, n, v in best_lineup(view, mine, next_week)[1]
        ] if next_week else None,
        "best_adds": [a for _, a in adds[:8]],
        "all_teams": league,
        "sims": sims,
        "notes": notes,
    }
