"""Turn ESPN's raw league JSON into compact, consistent dicts for Claude.

Everything here is pure (no network) so it can be tested against saved
ESPN responses.

ESPN stat "splits" on a player:
  statSourceId     0 = actual, 1 = projected
  statSplitTypeId  football:   0 = season, 1 = single week
                   basketball: 0 = season, 1 = last 7 days, 2 = last 15,
                               3 = last 30, 5 = single game
"""

from __future__ import annotations

import datetime as dt
import difflib
import re
import unicodedata
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from espn_api.football.constant import SETTINGS_SCORING_FORMAT_MAP

from .sports import (
    BASKETBALL,
    BASKETBALL_CORE_STATS,
    FOOTBALL,
    FOOTBALL_SKIP_STATS,
    OUT_STATUSES,
    RISK_STATUSES,
    Sport,
)

SCORING_TYPES = {
    "H2H_POINTS": "Head-to-head points",
    "H2H_CATEGORY": "Head-to-head each category",
    "H2H_MOST_CATEGORIES": "Head-to-head most categories",
    "ROTO": "Rotisserie",
    "TOTAL_POINTS": "Season total points",
}
CATEGORY_TYPES = {"H2H_CATEGORY", "H2H_MOST_CATEGORIES", "ROTO"}
HEALTHY = {None, "", "ACTIVE", "NORMAL"}
PCT_STATS = {"FG%", "FT%", "3PT%", "AFG%"}

BBALL_WINDOWS = {"season": 0, "l7": 1, "l15": 2, "l30": 3}

# ESPN's scoring "days" follow the US Eastern calendar (a 10:30pm ET tip-off is
# that day's game even though it's the next day in UTC), so all date math uses
# Eastern time; only displayed times use the user's time zone.
try:
    ESPN_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - missing tz database (some Windows setups)
    ESPN_TZ = dt.timezone(dt.timedelta(hours=-5))


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def rnd(x, n: int = 1):
    if x is None:
        return None
    try:
        v = round(float(x), n)
    except (TypeError, ValueError):
        return None
    return int(v) if v == int(v) and n <= 1 and abs(v) < 1e15 else v


def num(value) -> float | None:
    """ESPN occasionally sends stats as text (e.g. minutes as "34:12");
    return a float, or None when the value isn't numeric."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compact(d: dict) -> dict:
    """Drop keys whose value is None / empty so outputs stay small."""
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def ms_to_dt(ms, tz=None) -> dt.datetime | None:
    if not ms:
        return None
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).astimezone(tz)


def fmt_time(when: dt.datetime | None) -> str | None:
    if when is None:
        return None
    text = when.strftime("%a %b %d %I:%M%p").replace(" 0", " ")
    tz = when.tzname()
    return f"{text} {tz}" if tz and tz != "UTC" else text


def fmt_date(when: dt.datetime | None) -> str | None:
    return when.strftime("%Y-%m-%d") if when else None


def norm_name(name: str) -> str:
    name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    name = re.sub(r"[^a-z0-9/ ]", "", name.lower())
    name = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", name)
    return re.sub(r"\s+", " ", name).strip()


def ppe_of(wrap: dict) -> dict:
    """Roster entries nest the player pool entry; pool results are the entry."""
    return wrap.get("playerPoolEntry") or wrap


def player_of(wrap: dict) -> dict:
    return ppe_of(wrap).get("player") or {}


def find_split(player: dict, source: int, split: int, season=None, period=None) -> dict | None:
    for s in player.get("stats") or []:
        if s.get("statSourceId") != source or s.get("statSplitTypeId") != split:
            continue
        if season is not None and s.get("seasonId") != season:
            continue
        if period is not None and s.get("scoringPeriodId") != period:
            continue
        return s
    return None


def matchup_total_split(player: dict) -> dict | None:
    """The matchup-period total ESPN attaches to box-score rosters.

    ESPN tags it with an id ending in "null" ('01null' in football, '05null' in
    daily sports); fall back to an actual split with no scoring period.
    """
    stats = player.get("stats") or []
    for s in stats:
        if s.get("statSourceId") == 0 and str(s.get("id", "")).endswith("null"):
            return s
    for s in stats:
        if s.get("statSourceId") == 0 and s.get("statSplitTypeId") in (1, 5) and not s.get("scoringPeriodId"):
            return s
    return None


# --------------------------------------------------------------------------
# pro (NFL / NBA) schedule
# --------------------------------------------------------------------------
class ProSchedule:
    """Real-world games by pro team and ESPN scoring period."""

    def __init__(self, raw: dict | None, sport: Sport, tz=None, now: dt.datetime | None = None):
        self.sport = sport
        self.tz = tz
        self.now = now or dt.datetime.now(dt.timezone.utc).astimezone(tz)
        self.today = self.now.astimezone(ESPN_TZ).date()  # ESPN's calendar day
        self.by_team: dict[int, dict[int, list[dict]]] = {}
        self.bye_week: dict[int, int] = {}
        self.period_date: dict[int, dt.date] = {}
        for team in ((raw or {}).get("settings") or {}).get("proTeams") or []:
            tid = team.get("id")
            if tid is None:
                continue
            if team.get("byeWeek"):
                self.bye_week[tid] = team["byeWeek"]
            periods: dict[int, list[dict]] = {}
            for key, games in (team.get("proGamesByScoringPeriod") or {}).items():
                try:
                    period = int(key)
                except ValueError:
                    continue
                periods[period] = games or []
                for g in games or []:
                    start = ms_to_dt(g.get("date"), ESPN_TZ)
                    if start and (period not in self.period_date or start.date() < self.period_date[period]):
                        self.period_date[period] = start.date()
            self.by_team[tid] = periods

    def game(self, pro_team_id, period) -> dict | None:
        games = self.by_team.get(pro_team_id, {}).get(period) or []
        if not games:
            return None
        g = games[0]
        home = g.get("homeProTeamId") == pro_team_id
        opp = g.get("awayProTeamId") if home else g.get("homeProTeamId")
        return {"opp_id": opp, "home": home, "start": ms_to_dt(g.get("date"), self.tz)}

    def matchup_label(self, game: dict | None) -> str | None:
        if not game:
            return None
        return ("vs " if game["home"] else "@") + self.sport.pro_team(game["opp_id"])

    # basketball: periods are days
    def periods_between(self, start: dt.date, end: dt.date) -> list[int]:
        return sorted(p for p, d in self.period_date.items() if start <= d <= end)

    def upcoming_games(self, pro_team_id, start: dt.date, end: dt.date) -> list[dict]:
        out = []
        for p in self.periods_between(start, end):
            g = self.game(pro_team_id, p)
            if g and (g["start"] is None or g["start"] > self.now):
                g["period"] = p
                out.append(g)
        return out


def end_of_week(today: dt.date) -> dt.date:
    """ESPN basketball matchup weeks end on Sunday."""
    return today + dt.timedelta(days=(6 - today.weekday()))


# --------------------------------------------------------------------------
# league
# --------------------------------------------------------------------------
class LeagueView:
    def __init__(
        self,
        sport: Sport,
        core: dict,
        *,
        my_team_id: int | None = None,
        pro: ProSchedule | None = None,
        pos_ratings: dict | None = None,
        tz=None,
    ) -> None:
        self.sport = sport
        self.core = core
        self.tz = tz
        self.pro = pro or ProSchedule(None, sport, tz)
        self.pos_ratings = pos_ratings or {}
        s = core.get("settings") or {}
        st = core.get("status") or {}
        self.settings = s
        self.status = st
        self.name = s.get("name") or f"League {core.get('id')}"
        self.league_id = core.get("id")
        self.season = core.get("seasonId")
        scoring = s.get("scoringSettings") or {}
        self.scoring_type = scoring.get("scoringType") or "H2H_POINTS"
        self.is_categories = self.scoring_type in CATEGORY_TYPES
        self.current_matchup_period = st.get("currentMatchupPeriod") or 1
        self.scoring_period = core.get("scoringPeriodId") or st.get("latestScoringPeriod") or 1
        self.final_scoring_period = st.get("finalScoringPeriod")
        sched = s.get("scheduleSettings") or {}
        self.reg_season_periods = sched.get("matchupPeriodCount")
        self.matchup_periods = {
            int(k): v for k, v in (sched.get("matchupPeriods") or {}).items()
        }
        self.divisions = {d.get("id"): d.get("name") for d in sched.get("divisions") or []}
        self.teams: dict[int, dict] = {t["id"]: t for t in core.get("teams") or []}
        self.members: dict[str, dict] = {m.get("id"): m for m in core.get("members") or []}
        self.my_team_id = my_team_id
        roster_settings = s.get("rosterSettings") or {}
        self.slot_counts: dict[int, int] = {
            int(k): v for k, v in (roster_settings.get("lineupSlotCounts") or {}).items() if v
        }
        self.categories: list[dict] = []
        if self.is_categories:
            for item in scoring.get("scoringItems") or []:
                self.categories.append(
                    {
                        "id": item.get("statId"),
                        "name": sport.stat(item.get("statId")),
                        "lower_is_better": bool(item.get("isReverseItem")),
                    }
                )
        acq = s.get("acquisitionSettings") or {}
        self.uses_faab = bool(acq.get("isUsingAcquisitionBudget"))
        self.faab_budget = acq.get("acquisitionBudget") or 0
        self.season_over = self._season_over()

    # ---- basics -----------------------------------------------------------
    @property
    def is_football(self) -> bool:
        return self.sport.key == FOOTBALL

    def _season_over(self) -> bool:
        sched = self.core.get("schedule") or []
        if not sched:
            return False
        return all((m.get("winner") or "UNDECIDED") != "UNDECIDED" for m in sched)

    def team_name(self, team_id) -> str:
        t = self.teams.get(team_id)
        if not t:
            return "BYE" if not team_id else f"Team {team_id}"
        name = t.get("name") or f"{t.get('location', '')} {t.get('nickname', '')}".strip()
        return name or t.get("abbrev") or f"Team {team_id}"

    def team_label(self, team_id) -> str:
        label = self.team_name(team_id)
        if team_id == self.my_team_id:
            label += " (you)"
        return label

    def owners(self, team_id) -> list[str]:
        t = self.teams.get(team_id) or {}
        out = []
        for oid in t.get("owners") or []:
            m = self.members.get(oid) or {}
            full = f"{m.get('firstName', '')} {m.get('lastName', '')}".strip()
            name = m.get("displayName") or full
            if full and name and full.lower() != name.lower():
                name = f"{name} ({full})"
            if name:
                out.append(name)
        return out

    def record(self, team_id, kind: str = "overall") -> dict:
        return ((self.teams.get(team_id) or {}).get("record") or {}).get(kind) or {}

    def record_str(self, team_id, kind: str = "overall") -> str:
        r = self.record(team_id, kind)
        w, l, t = r.get("wins", 0), r.get("losses", 0), r.get("ties", 0)
        return f"{w}-{l}-{t}" if t else f"{w}-{l}"

    def find_team(self, query) -> int | None:
        """Match a team by id, abbreviation, name or owner name."""
        if query is None or (isinstance(query, str) and query.strip().lower() in ("", "me", "mine", "my team", "my")):
            return self.my_team_id
        q = str(query).strip()
        if q.isdigit() and int(q) in self.teams:
            return int(q)
        ql = q.lower()
        for tid, t in self.teams.items():
            if (t.get("abbrev") or "").lower() == ql or self.team_name(tid).lower() == ql:
                return tid
        matches = [
            tid
            for tid in self.teams
            if ql in self.team_name(tid).lower()
            or any(ql in o.lower() for o in self.owners(tid))
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def team_choices(self) -> list[str]:
        return [f"{self.team_name(t)} [{self.teams[t].get('abbrev')}]" for t in sorted(self.teams)]

    def period_label(self, matchup_period=None) -> str:
        mp = matchup_period or self.current_matchup_period
        word = "Week" if self.is_football else "Matchup"
        playoff = self.reg_season_periods and mp > self.reg_season_periods
        return f"{word} {mp}" + (" (playoffs)" if playoff else "")

    # ---- settings ---------------------------------------------------------
    def settings_view(self) -> dict:
        s = self.settings
        sched = s.get("scheduleSettings") or {}
        acq = s.get("acquisitionSettings") or {}
        trade = s.get("tradeSettings") or {}
        draft = s.get("draftSettings") or {}
        scoring = s.get("scoringSettings") or {}

        slots = {}
        for sid, count in sorted(self.slot_counts.items()):
            slots[self.sport.slot(sid) or str(sid)] = count
        starters = sum(c for sid, c in self.slot_counts.items() if self.sport.is_starting_slot(sid))

        out: dict[str, Any] = {
            "league": self.name,
            "league_id": self.league_id,
            "sport": self.sport.key,
            "season": self.season,
            "format": SCORING_TYPES.get(self.scoring_type, self.scoring_type),
            "teams": len(self.teams),
            "your_team": self.team_name(self.my_team_id) if self.my_team_id else None,
            "now": {
                "matchup_period": self.current_matchup_period,
                "label": self.period_label(),
                "scoring_period": self.scoring_period,
                "season_over": self.season_over or None,
            },
            "roster": {"slots": slots, "starters": starters},
        }
        if self.is_categories:
            out["categories"] = [
                c["name"] + (" (lower is better)" if c["lower_is_better"] else "")
                for c in self.categories
            ]
        else:
            if self.is_football:
                out["scoring_summary"] = self._football_summary(scoring)
            out["scoring"] = self._points_rules(scoring)
        if scoring.get("scoringEnhancementType") == "WIN_BONUS_TOP_HALF":
            out["median_scoring"] = "Each week you also play the league median (extra W/L)"
        out["schedule"] = compact(
            {
                "regular_season_matchups": sched.get("matchupPeriodCount"),
                "playoff_teams": sched.get("playoffTeamCount"),
                "playoff_matchup_length": sched.get("playoffMatchupPeriodLength"),
                "playoff_seeding_tiebreak": sched.get("playoffSeedingRule"),
                "tie_rule": scoring.get("matchupTieRule"),
                "divisions": [d for d in self.divisions.values() if d] if len(self.divisions) > 1 else None,
            }
        )
        out["waivers_and_free_agents"] = compact(
            {
                "type": acq.get("acquisitionType"),
                "faab_budget": self.faab_budget if self.uses_faab else None,
                "min_bid": acq.get("minimumBid") if self.uses_faab else None,
                "waiver_hours": acq.get("waiverHours"),
                "process_days": acq.get("waiverProcessDays"),
                "process_hour": acq.get("waiverProcessHour"),
                "season_acquisition_limit": None if (acq.get("acquisitionLimit") in (None, -1)) else acq.get("acquisitionLimit"),
                "add_limit": self._add_limit_text(),
            }
        )
        deadline = ms_to_dt(trade.get("deadlineDate"), self.tz)
        out["trades"] = compact(
            {
                "deadline": fmt_time(deadline),
                "review_hours": trade.get("revisionHours"),
                "veto_votes_required": trade.get("vetoVotesRequired"),
                "allow_out_of_position": None,
            }
        )
        out["draft"] = compact(
            {
                "type": draft.get("type"),
                "date": fmt_time(ms_to_dt(draft.get("date"), self.tz)),
                "keepers": draft.get("keeperCount") or None,
                "auction_budget": draft.get("auctionBudget") if draft.get("type") == "AUCTION" else None,
            }
        )
        lock = (s.get("rosterSettings") or {}).get("lineupLocktimeType")
        if lock:
            out["roster"]["lineup_lock"] = lock
        return compact(out)

    def add_limit(self) -> tuple[int | None, str]:
        """(limit, 'day'|'matchup') for pickups, or (None, '') when unlimited."""
        acq = self.settings.get("acquisitionSettings") or {}
        limit = acq.get("matchupAcquisitionLimit")
        if limit is None or limit <= 0:
            return None, ""
        return int(limit), ("day" if acq.get("matchupLimitPerScoringPeriod") else "matchup")

    def _add_limit_text(self) -> str | None:
        limit, per = self.add_limit()
        return f"{limit} pickup(s) per {per}" if limit else None

    def adds_status(self, team_id) -> dict | None:
        """Pickups used this matchup vs the league limit."""
        limit, per = self.add_limit()
        totals = ((self.teams.get(team_id) or {}).get("transactionCounter") or {}).get("matchupAcquisitionTotals") or {}
        used = totals.get(str(self.current_matchup_period), 0)
        if not limit:
            return {"pickups_this_matchup": used} if used else None
        out = {"limit": f"{limit} per {per}", "pickups_this_matchup": used}
        if per == "matchup":
            out["pickups_left_this_matchup"] = max(0, limit - used)
        return out

    def _football_summary(self, scoring: dict) -> str:
        pts = {i.get("statId"): (i.get("points") or 0) for i in scoring.get("scoringItems") or []}
        rec = pts.get(53, 0)
        ppr = {1: "Full PPR", 0.5: "Half PPR", 0: "Standard (no PPR)"}.get(rec, f"{rec} pts per reception")
        ptd = pts.get(4)
        parts = [ppr]
        if ptd:
            parts.append(f"{rnd(ptd)}-pt passing TDs")
        if pts.get(72):  # fumbles lost
            parts.append(f"fumble lost {rnd(pts[72])}")
        return ", ".join(parts)

    def _points_rules(self, scoring: dict) -> list[str]:
        rules = []
        for item in scoring.get("scoringItems") or []:
            sid = item.get("statId")
            pts = item.get("points") or 0
            if self.is_football:
                label = (SETTINGS_SCORING_FORMAT_MAP.get(sid) or {}).get("label") or self.sport.stat(sid)
            else:
                label = self.sport.stat(sid)
            overrides = item.get("pointsOverrides") or {}
            if pts == 0 and overrides:
                parts = [f"{self.sport.slot(int(k))}: {rnd(v, 2)}" for k, v in overrides.items()]
                rules.append(f"{label} = " + ", ".join(parts))
            elif pts:
                extra = ""
                if overrides:
                    extra = " (" + ", ".join(f"{self.sport.slot(int(k))}: {rnd(v, 2)}" for k, v in overrides.items()) + ")"
                rules.append(f"{label} = {rnd(pts, 2)}{extra}")
        return rules

    # ---- standings ---------------------------------------------------------
    def standings(self) -> list[dict]:
        rows = []
        for tid, t in self.teams.items():
            overall = self.record(tid)
            streak = None
            if overall.get("streakType") in ("WIN", "LOSS", "TIE") and overall.get("streakLength"):
                streak = overall["streakType"][0] + str(overall["streakLength"])
            sim = t.get("currentSimulationResults") or {}
            counter = t.get("transactionCounter") or {}
            row = {
                "seed": t.get("playoffSeed"),
                "final_rank": t.get("rankCalculatedFinal") or t.get("rankFinal") or None,
                "team": self.team_label(tid),
                "abbrev": t.get("abbrev"),
                "team_id": tid,
                "owners": self.owners(tid),
                "record": self.record_str(tid),
                "win_pct": rnd(overall.get("percentage"), 3),
                "games_back": rnd(overall.get("gamesBack")),
                "streak": streak,
                "points_for": None if self.is_categories else rnd(overall.get("pointsFor")),
                "points_against": None if self.is_categories else rnd(overall.get("pointsAgainst")),
                "division": self.divisions.get(t.get("divisionId")) if len(self.divisions) > 1 else None,
                "division_record": self.record_str(tid, "division") if len(self.divisions) > 1 else None,
                "playoff_odds_pct": rnd(sim["playoffPct"] * 100) if sim.get("playoffPct") is not None else None,
                "clinched": t.get("playoffClinchType") if t.get("playoffClinchType") not in (None, "UNKNOWN", "NONE") else None,
                "espn_projected_rank": t.get("currentProjectedRank") or None,
                "waiver_priority": t.get("waiverRank") or None,
                "faab_left": (self.faab_budget - (counter.get("acquisitionBudgetSpent") or 0)) if self.uses_faab else None,
                "moves": counter.get("acquisitions"),
                "trades": counter.get("trades"),
            }
            if self.is_categories and t.get("valuesByStat"):
                cat_ids = {str(c["id"]) for c in self.categories}
                row["season_category_totals"] = {
                    self.sport.stat(k): rnd(v, 3 if self.sport.stat(k) in PCT_STATS else 1)
                    for k, v in t["valuesByStat"].items()
                    if k in cat_ids
                }
                if self.scoring_type == "ROTO":
                    row["roto_points"] = rnd(t.get("points"))
            rows.append(compact(row))
        rows.sort(key=lambda r: (r.get("final_rank") or r.get("seed") or 99))
        return rows

    # ---- players -----------------------------------------------------------
    def _injury(self, wrap: dict, player: dict) -> str | None:
        status = player.get("injuryStatus") or wrap.get("injuryStatus")
        return None if status in HEALTHY else status

    def _bball_stat_line(self, split: dict | None, names: list[str], average: bool = True) -> dict:
        if not split:
            return {}
        src = split.get("averageStats") if average and split.get("averageStats") else split.get("stats")
        if not src:
            return {}
        out = {}
        for k, v in src.items():
            name = self.sport.stat(k)
            if name in names:
                out[name] = rnd(v, 3 if name in PCT_STATS else 1)
        return {n: out[n] for n in names if n in out}

    def bball_stat_names(self) -> list[str]:
        if self.is_categories:
            names = [c["name"] for c in self.categories]
            # Shooting volume decides how much a player moves FG%/FT%.
            volume = []
            if "FG%" in names:
                volume += ["FGM", "FGA"]
            if "FT%" in names:
                volume += ["FTM", "FTA"]
            return names + [n for n in volume + ["MIN", "GP"] if n not in names]
        return BASKETBALL_CORE_STATS

    def _football_stat_line(self, split: dict | None, limit: int = 12) -> dict:
        if not split or not split.get("stats"):
            return {}
        items = []
        for k, v in split["stats"].items():
            name = self.sport.stat(k)
            if not v or name.isdigit() or str(k).isdigit() and int(k) in FOOTBALL_SKIP_STATS:
                continue
            items.append((name, rnd(v, 1)))
        return dict(items[:limit])

    def player_row(
        self,
        wrap: dict,
        *,
        period: int | None = None,
        detail: str = "standard",
        show_fantasy_team: bool = False,
    ) -> dict:
        """One player, sport-aware. ``wrap`` is a roster entry or a pool entry."""
        ppe = ppe_of(wrap)
        p = player_of(wrap)
        sport = self.sport
        period = period or self.scoring_period
        own = p.get("ownership") or {}
        row: dict[str, Any] = {
            "name": p.get("fullName"),
            "id": p.get("id") or ppe.get("id"),
            "pos": sport.position(p),
            "team": sport.pro_team(p.get("proTeamId")),
            "injury": self._injury(wrap, p),
        }
        if "lineupSlotId" in wrap:
            row["slot"] = sport.slot(wrap.get("lineupSlotId"))
        elig = sport.eligible_positions(p.get("eligibleSlots"), allowed=self.slot_counts)
        if len(elig) > 1 or (elig and elig[0] != row["pos"]):
            row["eligible"] = elig
        on_team = ppe.get("onTeamId")
        if show_fantasy_team:
            if on_team:
                row["fantasy_team"] = self.team_label(on_team)
            elif ppe.get("status") == "WAIVERS":
                row["fantasy_team"] = "On waivers"
            elif ppe.get("status") == "FREEAGENT":
                row["fantasy_team"] = "Free agent"
        row["owned_pct"] = rnd(own.get("percentOwned"))
        row["started_pct"] = rnd(own.get("percentStarted"))
        if own.get("percentChange"):
            row["owned_change"] = rnd(own.get("percentChange"), 2)
        ratings = ppe.get("ratings") or {}

        if self.is_football:
            self._football_fields(row, wrap, ppe, p, period, ratings, detail)
        else:
            self._basketball_fields(row, ppe, p, ratings, detail)
        if ppe.get("lineupLocked"):
            row["locked"] = True
        return compact(row)

    def _football_fields(self, row, wrap, ppe, p, week, ratings, detail):
        season = find_split(p, 0, 0, self.season)
        proj = find_split(p, 1, 1, self.season, period=week)
        actual = find_split(p, 0, 1, self.season, period=week)
        last = find_split(p, 0, 1, self.season, period=week - 1) if week and week > 1 else None
        r0 = ratings.get("0") or {}
        total = (season or {}).get("appliedTotal")
        avg = (season or {}).get("appliedAverage")
        row["season_fpts"] = compact(
            {
                "total": rnd(total),
                "avg": rnd(avg),
                "games": round(total / avg) if total and avg else None,
                "pos_rank": r0.get("positionalRanking") or None,
                "ovr_rank": r0.get("totalRanking") or None,
            }
        )
        wk = {
            "proj": rnd((proj or {}).get("appliedTotal")),
            "pts": rnd((actual or {}).get("appliedTotal")) if actual else None,
        }
        game = self.pro.game(p.get("proTeamId"), week)
        if game is None:
            wk["game"] = "BYE" if self.pro.bye_week.get(p.get("proTeamId")) == week else "no game"
        else:
            wk["game"] = self.pro.matchup_label(game)
            wk["kickoff"] = fmt_time(game["start"])
            rating = self._oprk(p, game["opp_id"])
            if rating:
                wk["opp_rank_vs_pos"] = rating
        row["week"] = compact(wk)
        if last:
            row["last_week_pts"] = rnd(last.get("appliedTotal"))
        if detail == "full":
            row["season_stats"] = self._football_stat_line(season, limit=16)
            row["week_proj_stats"] = self._football_stat_line(proj)

    def _oprk(self, player: dict, opp_id) -> int | None:
        ratings = (self.pos_ratings.get(str(player.get("defaultPositionId"))) or {})
        by_opp = ratings.get("ratingsByOpponent") if "ratingsByOpponent" in ratings else ratings
        hit = (by_opp or {}).get(str(opp_id))
        return hit.get("rank") if isinstance(hit, dict) else None

    def _basketball_fields(self, row, ppe, p, ratings, detail):
        names = self.bball_stat_names()
        season = find_split(p, 0, 0, self.season)
        if not self.is_categories:
            fp = {}
            for label, split_id in BBALL_WINDOWS.items():
                s = find_split(p, 0, split_id, self.season)
                if s and s.get("appliedAverage") is not None:
                    fp[f"{label}_avg"] = rnd(s.get("appliedAverage"))
            proj = find_split(p, 1, 0, self.season)
            if proj:
                fp["proj_avg"] = rnd(proj.get("appliedAverage"))
            if season:
                fp["season_total"] = rnd(season.get("appliedTotal"))
            row["fpts"] = compact(fp)
        rater = {}
        for label, key in (("season", "0"), ("l7", "1"), ("l15", "2"), ("l30", "3")):
            r = ratings.get(key) or {}
            if r.get("totalRating") is not None:
                rater[label] = rnd(r.get("totalRating"), 2)
        if rater:
            row["rater"] = rater
            if (ratings.get("0") or {}).get("totalRanking"):
                row["rank"] = ratings["0"]["totalRanking"]
        row["season_avg"] = self._bball_stat_line(season, names)
        if detail == "full":
            for label, split_id in (("l7", 1), ("l15", 2), ("l30", 3)):
                line = self._bball_stat_line(find_split(p, 0, split_id, self.season), names)
                if line:
                    row[f"{label}_avg"] = line
        row["games"] = self.bball_games(p.get("proTeamId"))

    def bball_games(self, pro_team_id) -> dict:
        today = self.pro.today
        week_end = end_of_week(today)
        today_game = self.pro.game(pro_team_id, self.scoring_period)
        if today_game and self.pro.period_date.get(self.scoring_period) != today:
            today_game = None  # scoring period isn't today (offseason / stale)
        out = {
            "today": (
                f"{self.pro.matchup_label(today_game)} {today_game['start'].strftime('%I:%M%p').lstrip('0')}"
                if today_game and today_game["start"]
                else None
            ),
            "left_this_week": len(self.pro.upcoming_games(pro_team_id, today, week_end)),
            "next_7_days": len(self.pro.upcoming_games(pro_team_id, today, today + dt.timedelta(days=6))),
        }
        return {k: v for k, v in out.items() if v is not None}

    # ---- rosters -----------------------------------------------------------
    def roster_entries(self, team_id, roster_core: dict | None = None) -> list[dict]:
        src = roster_core or self.core
        for t in src.get("teams") or []:
            if t.get("id") == team_id:
                return (t.get("roster") or {}).get("entries") or []
        return []

    def slot_order(self, entry: dict) -> tuple:
        sid = entry.get("lineupSlotId")
        rank = 0 if self.sport.is_starting_slot(sid) else (1 if sid == self.sport.bench_slot else 2)
        return (rank, sid if sid is not None else 99)

    def roster(self, team_id, *, detail: str = "standard", roster_core: dict | None = None, period=None) -> list[dict]:
        entries = sorted(self.roster_entries(team_id, roster_core), key=self.slot_order)
        return [self.player_row(e, detail=detail, period=period) for e in entries]

    def team_overview(self, team_id) -> dict:
        t = self.teams.get(team_id) or {}
        counter = t.get("transactionCounter") or {}
        return compact(
            {
                "team": self.team_label(team_id),
                "abbrev": t.get("abbrev"),
                "team_id": team_id,
                "owners": self.owners(team_id),
                "record": self.record_str(team_id),
                "seed": t.get("playoffSeed"),
                "points_for": None if self.is_categories else rnd(self.record(team_id).get("pointsFor")),
                "faab_left": (self.faab_budget - (counter.get("acquisitionBudgetSpent") or 0)) if self.uses_faab else None,
                "waiver_priority": t.get("waiverRank") or None,
                "pickups": self.adds_status(team_id),
                "on_trade_block": self.trade_block(team_id) or None,
            }
        )

    def trade_block(self, team_id) -> list[str]:
        """Names of players this team has put on the trade block."""
        block = ((self.teams.get(team_id) or {}).get("tradeBlock") or {}).get("players") or {}
        if not block:
            return []
        names = {}
        for e in self.roster_entries(team_id):
            p = player_of(e)
            names[str(p.get("id") or e.get("playerId"))] = p.get("fullName")
        return [names.get(str(pid), f"player {pid}") for pid in block]

    # ---- schedule ------------------------------------------------------------
    def _side(self, m: dict, team_id) -> tuple[dict, dict, str]:
        if (m.get("home") or {}).get("teamId") == team_id:
            return m.get("home") or {}, m.get("away") or {}, "HOME"
        return m.get("away") or {}, m.get("home") or {}, "AWAY"

    def _cat_score(self, side: dict) -> str:
        c = side.get("cumulativeScore") or {}
        w, l, t = c.get("wins", 0), c.get("losses", 0), c.get("ties", 0)
        return f"{w}-{l}-{t}"

    def team_schedule(self, team_id) -> dict:
        games = []
        remaining = []
        for m in sorted(self.core.get("schedule") or [], key=lambda x: x.get("matchupPeriodId", 0)):
            home_id = (m.get("home") or {}).get("teamId")
            away_id = (m.get("away") or {}).get("teamId")
            if team_id not in (home_id, away_id):
                continue
            me, opp, side = self._side(m, team_id)
            opp_id = opp.get("teamId")
            winner = m.get("winner") or "UNDECIDED"
            if winner == "UNDECIDED":
                result = None
            elif winner == "TIE":
                result = "T"
            else:
                result = "W" if winner == side else "L"
            mp = m.get("matchupPeriodId")
            row = {
                "period": mp,
                "opponent": self.team_label(opp_id) if opp_id else "BYE",
                "opp_record": self.record_str(opp_id) if opp_id else None,
                "result": result,
                "playoff": m.get("playoffTierType") if m.get("playoffTierType") not in (None, "NONE") else None,
            }
            if result or mp == self.current_matchup_period:
                if self.is_categories:
                    row["score"] = f"{self._cat_score(me)} vs {self._cat_score(opp)}"
                else:
                    row["score"] = f"{rnd(me.get('totalPoints'))}-{rnd(opp.get('totalPoints'))}"
            if mp == self.current_matchup_period and not result:
                row["current"] = True
            games.append(compact(row))
            if not result and mp >= self.current_matchup_period and opp_id:
                remaining.append(opp_id)
        out = {
            "team": self.team_label(team_id),
            "record": self.record_str(team_id),
            "matchups": games,
        }
        if remaining:
            out["remaining_opponents_win_pct"] = rnd(
                sum(self.record(o).get("percentage", 0) for o in remaining) / len(remaining), 3
            )
        return out

    # ---- scoreboard / box scores ------------------------------------------------
    def _side_summary(self, side: dict) -> dict:
        tid = side.get("teamId")
        out: dict[str, Any] = {"team": self.team_label(tid) if tid else "BYE", "record": self.record_str(tid) if tid else None}
        if self.is_categories:
            c = side.get("cumulativeScore") or {}
            out["cats"] = f"{c.get('wins', 0)}-{c.get('losses', 0)}-{c.get('ties', 0)}"
        else:
            live = side.get("totalPointsLive")
            out["score"] = rnd(live if live is not None else side.get("totalPoints"), 2)
            proj = side.get("totalProjectedPointsLive", side.get("totalProjectedPoints"))
            if proj is not None and proj >= 0:
                out["projected"] = rnd(proj, 1)
        return compact(out)

    def _category_table(self, home: dict, away: dict) -> list[dict]:
        h = (home.get("cumulativeScore") or {}).get("scoreByStat") or {}
        a = (away.get("cumulativeScore") or {}).get("scoreByStat") or {}
        rows = []
        for cat in self.categories:
            k = str(cat["id"])
            if k not in h and k not in a:
                continue
            hv = (h.get(k) or {}).get("score")
            av = (a.get(k) or {}).get("score")
            res = (h.get(k) or {}).get("result")
            digits = 3 if cat["name"] in PCT_STATS else 1
            rows.append(
                compact(
                    {
                        "cat": cat["name"],
                        "home": rnd(hv, digits),
                        "away": rnd(av, digits),
                        "leader": {"WIN": "home", "LOSS": "away", "TIE": "tied"}.get(res, None),
                    }
                )
            )
        return rows

    def scoreboard(self, raw: dict, matchup_period: int) -> list[dict]:
        out = []
        for m in raw.get("schedule") or []:
            if m.get("matchupPeriodId") != matchup_period:
                continue
            home, away = m.get("home") or {}, m.get("away") or {}
            row: dict[str, Any] = {
                "home": self._side_summary(home),
                "away": self._side_summary(away),
                "winner": {"HOME": "home", "AWAY": "away", "TIE": "tie"}.get(m.get("winner")),
            }
            if m.get("playoffTierType") not in (None, "NONE"):
                row["playoff"] = m.get("playoffTierType")
            wp_h, wp_a = home.get("winProbability"), away.get("winProbability")
            if wp_h is not None and not row.get("winner"):
                row["home"]["win_prob_pct"] = rnd(wp_h * 100 if wp_h <= 1 else wp_h)
                if wp_a is not None:
                    row["away"]["win_prob_pct"] = rnd(wp_a * 100 if wp_a <= 1 else wp_a)
            if self.is_categories:
                row["categories"] = self._category_table(home, away)
            out.append(compact(row))
        return out

    def find_matchup(self, raw: dict, matchup_period: int, team_id) -> dict | None:
        for m in raw.get("schedule") or []:
            if m.get("matchupPeriodId") != matchup_period:
                continue
            if team_id in ((m.get("home") or {}).get("teamId"), (m.get("away") or {}).get("teamId")):
                return m
        return None

    def box_side(self, side: dict, matchup_period: int, current: bool) -> dict:
        """One team's lineup in a matchup, with per-player period results."""
        tid = side.get("teamId")
        period_roster = (side.get("rosterForMatchupPeriod") or {}).get("entries") or []
        current_roster = (side.get("rosterForCurrentScoringPeriod") or {}).get("entries") or []
        by_id_current = {e.get("playerId"): e for e in current_roster}
        players = []
        seen = set()
        lineup = current_roster if (current and current_roster) else (period_roster or current_roster)
        period_by_id = {e.get("playerId"): e for e in period_roster}
        week = self._matchup_week(matchup_period)
        for e in sorted(lineup, key=self.slot_order):
            pid = e.get("playerId")
            seen.add(pid)
            ppe = ppe_of(e)
            p = player_of(e)
            pe = period_by_id.get(pid)
            row: dict[str, Any] = {
                "slot": self.sport.slot(e.get("lineupSlotId")),
                "name": p.get("fullName"),
                "pos": self.sport.position(p),
                "team": self.sport.pro_team(p.get("proTeamId")),
                "injury": self._injury(e, p),
            }
            if self.is_categories:
                if pe:
                    stats = matchup_total_split(player_of(pe))
                    line = self._bball_stat_line(stats, [c["name"] for c in self.categories], average=False)
                    if line:
                        row["period_stats"] = line
            else:
                pts = ppe_of(pe).get("appliedStatTotal") if pe else None
                if pts is None and not current:
                    pts = ppe.get("appliedStatTotal")
                row["pts"] = rnd(pts, 2)
                if self.is_football:
                    proj = find_split(p, 1, 1, self.season, period=week) or (find_split(player_of(pe), 1, 1, self.season, period=week) if pe else None)
                    if proj:
                        row["proj"] = rnd(proj.get("appliedTotal"))
            if self.is_football:
                game = self.pro.game(p.get("proTeamId"), week)
                if game is None:
                    row["game"] = "BYE" if self.pro.bye_week.get(p.get("proTeamId")) == week else "no game"
                else:
                    started = game["start"] and game["start"] <= self.pro.now
                    row["game"] = f"{self.pro.matchup_label(game)} " + ("(started/final)" if started else fmt_time(game["start"]) or "")
            elif current:
                row["games_left"] = self.bball_games(p.get("proTeamId")).get("left_this_week")
            players.append(compact(row))
        summary = self._side_summary(side)
        summary["lineup"] = players
        if not self.is_football and current:
            summary["games_left_active_players"] = sum(
                (r.get("games_left") or 0)
                for r in players
                if r.get("slot") != self.sport.slot(self.sport.ir_slot) and r.get("injury") not in OUT_STATUSES
            )
        if not players:
            summary["note"] = "ESPN didn't return lineups for this matchup (common for past seasons)."
        return summary

    def _matchup_week(self, matchup_period: int) -> int:
        """Football: the scoring period (NFL week) for a matchup period."""
        if not self.is_football:
            return self.scoring_period
        periods = self.matchup_periods.get(matchup_period) or [matchup_period]
        if matchup_period == self.current_matchup_period:
            return self.scoring_period
        return periods[-1]

    # ---- transactions --------------------------------------------------------
    TX_LABELS = {
        "FREEAGENT": "free agent pickup",
        "WAIVER": "waiver claim",
        "WAIVER_ERROR": "failed waiver claim",
        "TRADE_ACCEPT": "trade",
        "TRADE_UPHOLD": "trade (upheld)",
        "TRADE_PROPOSAL": "trade proposal",
        "TRADE_VETO": "trade vetoed",
        "TRADE_DECLINE": "trade declined",
        "TRADE_ERROR": "trade failed",
    }

    def transactions(self, raws: Iterable[dict], names: dict[int, str]) -> list[dict]:
        rows = []
        seen = set()
        for raw in raws:
            for tx in raw.get("transactions") or []:
                if tx.get("id") in seen:
                    continue
                seen.add(tx.get("id"))
                kind = tx.get("type")
                if kind not in self.TX_LABELS:
                    continue
                items = tx.get("items") or []
                when = ms_to_dt(tx.get("processDate") or tx.get("proposedDate"), self.tz)
                row: dict[str, Any] = {
                    "when": fmt_time(when),
                    "_sort": when.timestamp() if when else 0,
                    "type": self.TX_LABELS[kind],
                    "status": tx.get("status") if tx.get("status") not in (None, "EXECUTED") else None,
                    "team": self.team_label(tx.get("teamId")) if tx.get("teamId") else None,
                }
                if kind.startswith("TRADE"):
                    gets: dict[str, list[str]] = {}
                    for it in items:
                        if it.get("type") != "TRADE":
                            continue
                        to = self.team_label(it.get("toTeamId"))
                        gets.setdefault(to, []).append(names.get(it.get("playerId"), f"player {it.get('playerId')}"))
                    row["gets"] = gets
                    drops = [names.get(it.get("playerId"), str(it.get("playerId"))) for it in items if it.get("type") == "DROP"]
                    if drops:
                        row["dropped"] = drops
                else:
                    row["added"] = [names.get(it.get("playerId"), str(it.get("playerId"))) for it in items if it.get("type") == "ADD"]
                    row["dropped"] = [names.get(it.get("playerId"), str(it.get("playerId"))) for it in items if it.get("type") == "DROP"]
                    if self.uses_faab and kind.startswith("WAIVER"):
                        row["bid"] = tx.get("bidAmount")
                rows.append(compact(row))
        rows.sort(key=lambda r: r.get("_sort", 0), reverse=True)
        for r in rows:
            r.pop("_sort", None)
        return rows

    # ---- draft ---------------------------------------------------------------
    def draft(self, raw: dict, names: dict[int, str], team_id=None) -> dict:
        detail = raw.get("draftDetail") or {}
        if not detail.get("drafted"):
            return {"drafted": False, "note": "This league hasn't drafted yet."}
        picks = []
        for pk in sorted(detail.get("picks") or [], key=lambda x: x.get("overallPickNumber", 0)):
            if team_id and pk.get("teamId") != team_id:
                continue
            picks.append(
                compact(
                    {
                        "pick": pk.get("overallPickNumber"),
                        "round": pk.get("roundId"),
                        "team": self.teams.get(pk.get("teamId"), {}).get("abbrev") or pk.get("teamId"),
                        "player": names.get(pk.get("playerId"), str(pk.get("playerId"))),
                        "price": pk.get("bidAmount") or None,
                        "keeper": True if pk.get("keeper") else None,
                    }
                )
            )
        return {
            "teams": {t.get("abbrev"): self.team_name(tid) for tid, t in self.teams.items()},
            "picks": picks,
        }


# --------------------------------------------------------------------------
# player search
# --------------------------------------------------------------------------
def match_players(query: str, index: list[dict], limit: int = 5) -> list[dict]:
    q = norm_name(query)
    if not q:
        return []
    by_norm: dict[str, list[dict]] = {}
    for p in index:
        by_norm.setdefault(norm_name(p.get("fullName", "")), []).append(p)
    if q in by_norm:
        return by_norm[q][:limit]
    contains = [p for n, ps in by_norm.items() if q in n for p in ps]
    if contains:
        return contains[:limit]
    close = difflib.get_close_matches(q, list(by_norm), n=limit, cutoff=0.75)
    return [p for n in close for p in by_norm[n]][:limit]
