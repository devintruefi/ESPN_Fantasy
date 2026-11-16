"""High-level operations behind each MCP tool. Independent of MCP so it can be
tested and used from the CLI."""

from __future__ import annotations

import datetime as dt
import html
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from pathlib import Path

from . import analysis, catmodel, catseason, external, ffmodel, insights, intel, projections, strategy, trades, writes
from .config import LeagueConfig, Settings
from .discovery import discover
from .espn import EspnError, LeagueClient, LeagueNotFound
from .sports import BASKETBALL, FOOTBALL, SPORT_ALIASES, SPORTS, Sport
from .views import (
    BBALL_WINDOWS,
    LeagueView,
    ProSchedule,
    compact,
    find_split,
    fmt_date,
    fmt_time,
    norm_name as views_norm,
    pick_player,
    ms_to_dt,
    num,
    player_of,
    ppe_of,
    rnd,
)


class UserError(Exception):
    """A problem with the request (unknown league/team/player)."""


@dataclass
class LeagueHandle:
    cfg: LeagueConfig
    settings: Settings
    sport: Sport = field(init=False)
    season: int = field(init=False)
    client: LeagueClient = field(init=False)
    _fallback_done: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        self.sport = SPORTS[self.cfg.sport]
        self.season = self.cfg.season or self.sport.guess_season()
        self.client = LeagueClient(self.sport, self.cfg.league_id, self.season, self.settings.cookies())

    def core(self) -> dict:
        try:
            return self.client.core()
        except LeagueNotFound:
            # The league may not have been renewed for the new season yet.
            with self._lock:
                if self.cfg.season is None and not self._fallback_done:
                    self._fallback_done = True
                    self.season -= 1
                    self.client = LeagueClient(self.sport, self.cfg.league_id, self.season, self.settings.cookies())
                    return self.client.core()
            raise

    def my_team_id(self, core: dict) -> int | None:
        team_ids = {t.get("id") for t in core.get("teams") or []}
        if self.cfg.team_id and self.cfg.team_id in team_ids:
            return self.cfg.team_id
        swid = (self.settings.swid or "").upper()
        if swid:
            for t in core.get("teams") or []:
                owners = [str(o).upper() for o in (t.get("owners") or [])]
                if swid in owners or str(t.get("primaryOwner") or "").upper() == swid:
                    return t.get("id")
        return None

    def label(self, core: dict | None = None) -> str:
        name = self.cfg.name or ((core or {}).get("settings") or {}).get("name") or f"League {self.cfg.league_id}"
        return name


class Hub:
    DISCOVERY_TTL = 6 * 3600

    def __init__(self, settings: Settings, tz=None, now_fn=None) -> None:
        self.settings = settings
        self.tz = tz
        self.now_fn = now_fn or (lambda: dt.datetime.now(dt.timezone.utc))
        self._handles: list[LeagueHandle] | None = None
        self._discovered_at = 0.0
        self.discovery_error: str | None = None
        self._lock = threading.Lock()
        self._worlds: dict = {}

    # ---- league registry ------------------------------------------------------
    def handles(self) -> list[LeagueHandle]:
        with self._lock:
            stale = self.settings.auto_discover and time.monotonic() - self._discovered_at > self.DISCOVERY_TTL
            if self._handles is None or stale:
                configs = list(self.settings.leagues)
                if self.settings.auto_discover and self.settings.has_credentials:
                    try:
                        known = {(c.sport, c.league_id) for c in configs}
                        configs += [c for c in discover(self.settings) if (c.sport, c.league_id) not in known]
                        self.discovery_error = None
                    except EspnError as e:
                        self.discovery_error = str(e)
                    self._discovered_at = time.monotonic()
                old = {(h.cfg.sport, h.cfg.league_id): h for h in (self._handles or [])}
                self._handles = [old.get((c.sport, c.league_id)) or LeagueHandle(c, self.settings) for c in configs]
            return self._handles

    def resolve(self, league: str | int | None) -> LeagueHandle:
        handles = self.handles()
        if not handles:
            raise UserError(self.no_leagues_message())
        if league in (None, ""):
            if len(handles) == 1:
                return handles[0]
            raise UserError("You have several leagues — say which one. " + self._league_choices())
        q = str(league).strip().lower()
        for h in handles:
            if q == str(h.cfg.league_id) or (h.cfg.name and q == h.cfg.name.lower()):
                return h
        if q in SPORT_ALIASES:
            by_sport = [h for h in handles if h.sport.key == SPORT_ALIASES[q]]
            if len(by_sport) == 1:
                return by_sport[0]
            if by_sport:
                raise UserError(f"You have {len(by_sport)} {SPORT_ALIASES[q]} leagues — say which one. " + self._league_choices())
            raise UserError(f"You have no {SPORT_ALIASES[q]} league configured. " + self._league_choices())
        matches = []
        for h in handles:
            try:
                name = h.label(h.core()).lower()
            except EspnError:
                name = (h.cfg.name or "").lower()
            if q in name:
                matches.append(h)
        if len(matches) == 1:
            return matches[0]
        raise UserError(f"Couldn't tell which league {league!r} means. " + self._league_choices())

    def _league_choices(self) -> str:
        parts = []
        for h in self.handles():
            try:
                name = h.label(h.core())
            except EspnError:
                name = h.cfg.name or "?"
            parts.append(f"{name} ({h.sport.key}, id {h.cfg.league_id})")
        return "Your leagues: " + "; ".join(parts)

    def no_leagues_message(self) -> str:
        msg = (
            "No ESPN leagues are configured. Add league ids with ESPN_LEAGUES "
            "(e.g. 'football:123456,basketball:654321') or in the config file, "
            "and for private leagues set ESPN_S2 and ESPN_SWID. Run "
            "`espn-fantasy-hub setup` for a guided setup."
        )
        if self.discovery_error:
            msg += f" (Automatic league lookup failed: {self.discovery_error})"
        return msg

    # ---- view construction -----------------------------------------------------
    def view(self, h: LeagueHandle, *, ratings: bool = False) -> LeagueView:
        core = h.core()
        pro_raw = h.client.pro_schedule()
        now = self.now_fn().astimezone(self.tz)
        pro = ProSchedule(pro_raw, h.sport, self.tz, now=now)
        pos_ratings = None
        if ratings and h.sport.key == FOOTBALL:
            try:
                raw = h.client.positional_ratings(core.get("scoringPeriodId") or 1)
                pos_ratings = (raw.get("positionAgainstOpponent") or {}).get("positionalRatings") or {}
            except EspnError:
                pos_ratings = None
        return LeagueView(h.sport, core, my_team_id=h.my_team_id(core), pro=pro, pos_ratings=pos_ratings, tz=self.tz)

    def _team(self, view: LeagueView, team) -> int:
        tid = view.find_team(team)
        if tid is None:
            if team in (None, "", "me", "mine", "my team"):
                raise UserError(
                    "I can't tell which team is yours in this league (no ESPN login cookies, "
                    "or your SWID doesn't own a team here). Name the team, or set team_id in the config. "
                    "Teams: " + "; ".join(view.team_choices())
                )
            raise UserError(f"No team matches {team!r}. Teams: " + "; ".join(view.team_choices()))
        return tid

    def _header(self, h: LeagueHandle, view: LeagueView) -> dict:
        return {"league": view.name, "sport": h.sport.key, "season": view.season}

    def _names(self, h: LeagueHandle, ids: set[int], final_period: int | None) -> dict[int, str]:
        names = {p["id"]: p.get("fullName") for p in h.client.player_index() if "id" in p}
        missing = [i for i in ids if i and i not in names]
        if missing:
            try:
                cards = h.client.player_cards(missing[:50], final_period or 1)
                for wrap in cards.get("players") or []:
                    pl = wrap.get("player") or {}
                    if pl.get("id"):
                        names[pl["id"]] = pl.get("fullName")
            except EspnError:
                pass
        return names

    # ---- tools -----------------------------------------------------------------
    def my_leagues(self) -> dict:
        handles = self.handles()
        if not handles:
            return {"leagues": [], "message": self.no_leagues_message()}
        out = []
        for h in handles:
            try:
                out.append(self._league_card(h))
            except EspnError as e:
                out.append({"league_id": h.cfg.league_id, "sport": h.sport.key, "error": str(e)})
        result: dict[str, Any] = {
            "leagues": out,
            "times_shown_in": self.now_fn().astimezone(self.tz).tzname(),
        }
        if self.discovery_error:
            result["note"] = f"Automatic league lookup failed: {self.discovery_error}"
        return result

    def _league_card(self, h: LeagueHandle) -> dict:
        view = self.view(h)
        card: dict[str, Any] = {
            "league": view.name,
            "league_id": h.cfg.league_id,
            "sport": h.sport.key,
            "season": view.season,
            "format": view.settings_view().get("format"),
            "teams": len(view.teams),
            "now": view.period_label() + (" — season over" if view.season_over else ""),
        }
        tid = view.my_team_id
        if tid is None:
            card["your_team"] = "unknown (set ESPN cookies or team_id)"
            return card
        card["your_team"] = view.team_overview(tid)
        if not view.season_over:
            raw = h.client.scoreboard(view.current_matchup_period, view.scoring_period)
            m = view.find_matchup(raw, view.current_matchup_period, tid)
            if m:
                me, opp, _ = view._side(m, tid)
                card["current_matchup"] = {"you": view._side_summary(me), "opponent": view._side_summary(opp)}
            check = analysis.lineup_check(view, tid, sorted(view.roster_entries(tid), key=view.slot_order), view.scoring_period)
            for i in check["issues"]:
                i.pop("_needs_pickup_slot", None)
            alerts = [i for i in check["issues"] if i["level"] in ("critical", "warning", "watch")]
            card["lineup_alerts"] = [f"[{i['level']}] {i['problem']}" for i in alerts] or ["none"]
        return card

    def league_settings(self, league=None) -> dict:
        h = self.resolve(league)
        return self.view(h).settings_view()

    def standings(self, league=None) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        return self._header(h, view) | {"now": view.period_label(), "standings": view.standings()}

    def roster(self, league=None, team=None, detail: bool = False) -> dict:
        h = self.resolve(league)
        view = self.view(h, ratings=True)
        tid = self._team(view, team)
        return self._header(h, view) | {
            "team": view.team_overview(tid),
            "period": view.period_label(),
            "players": view.roster(tid, detail="full" if detail else "standard"),
        }

    def all_rosters(self, league=None) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        teams = []
        for tid in sorted(view.teams, key=lambda t: view.teams[t].get("playoffSeed") or 99):
            players = []
            for e in sorted(view.roster_entries(tid), key=view.slot_order):
                row = view.player_row(e)
                players.append(compact({
                    "slot": row.get("slot"),
                    "name": row.get("name"),
                    "pos": row.get("pos"),
                    "team": row.get("team"),
                    "injury": row.get("injury"),
                    **self._headline_value(view, row),
                }))
            teams.append(compact({
                "team": view.team_label(tid),
                "record": view.record_str(tid),
                "seed": view.teams[tid].get("playoffSeed"),
                "on_trade_block": view.trade_block(tid),
                "players": players,
            }))
        return self._header(h, view) | {"value_shown": self._headline_label(view), "teams": teams}

    @staticmethod
    def _headline_label(view: LeagueView) -> str:
        if view.is_football:
            return "season fantasy points avg (avg) and this week's projection (proj)"
        if view.is_categories:
            return "ESPN player rater, season (rater) and last 15 days (rater_l15)"
        return "fantasy points per game, season (avg) and last 15 days (l15)"

    @staticmethod
    def _headline_value(view: LeagueView, row: dict) -> dict:
        if view.is_football:
            return {"avg": (row.get("season_fpts") or {}).get("avg"), "proj": (row.get("week") or {}).get("proj")}
        if view.is_categories:
            r = row.get("rater") or {}
            return {"rater": r.get("season"), "rater_l15": r.get("l15")}
        f = row.get("fpts") or {}
        return {"avg": f.get("season_avg"), "l15": f.get("l15_avg")}

    def scoreboard(self, league=None, period: int | None = None) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        mp = period or view.current_matchup_period
        sp = view.scoring_period if mp == view.current_matchup_period else self._period_scoring(view, mp)
        raw = h.client.scoreboard(mp, sp)
        return self._header(h, view) | {"period": view.period_label(mp), "matchups": view.scoreboard(raw, mp)}

    def _period_scoring(self, view: LeagueView, mp: int) -> int:
        if view.is_football:
            return view._matchup_week(mp)
        return view.scoring_period

    def matchup(self, league=None, team=None, period: int | None = None) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        tid = self._team(view, team)
        mp = period or view.current_matchup_period
        current = mp == view.current_matchup_period and not view.season_over
        sp = view.scoring_period if mp == view.current_matchup_period else self._period_scoring(view, mp)
        raw = h.client.scoreboard(mp, sp)
        m = view.find_matchup(raw, mp, tid)
        if not m:
            return self._header(h, view) | {"period": view.period_label(mp), "note": f"{view.team_label(tid)} has no matchup (bye) this period."}
        me, opp, _ = view._side(m, tid)
        out = self._header(h, view) | {
            "period": view.period_label(mp),
            "status": "final" if (m.get("winner") or "UNDECIDED") != "UNDECIDED" else ("in progress" if current else "upcoming"),
            "you" if tid == view.my_team_id else "team": view.box_side(me, mp, current),
            "opponent": view.box_side(opp, mp, current) if opp.get("teamId") else "BYE",
        }
        if view.is_categories:
            home_first = (m.get("home") or {}).get("teamId") == tid
            table = view._category_table(m.get("home") or {}, m.get("away") or {})
            out["categories"] = [
                compact({
                    "cat": r["cat"],
                    "you": r.get("home") if home_first else r.get("away"),
                    "opp": r.get("away") if home_first else r.get("home"),
                    "leader": {"home": "you", "away": "opp", "tied": "tied"}.get(r.get("leader")) if home_first
                    else {"home": "opp", "away": "you", "tied": "tied"}.get(r.get("leader")),
                })
                for r in table
            ]
            low = [c["name"] for c in view.categories if c["lower_is_better"]]
            if low:
                out["lower_is_better"] = low
        if not view.is_football and current:
            out["note"] = "games_left counts each player's remaining NBA games through Sunday (ESPN weeks end Sunday; playoff/All-Star matchups can run longer)."
        return out

    def schedule(self, league=None, team=None) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        tid = self._team(view, team)
        return self._header(h, view) | view.team_schedule(tid)

    POSITION_ALIASES = {"DST": "D/ST", "DEF": "D/ST", "D": "D/ST", "FLEX": "FLEX", "UTIL": "UT", "UTL": "UT"}

    def free_agents(self, league=None, position: str | None = None, sort_by: str | None = None, limit: int = 20) -> dict:
        h = self.resolve(league)
        view = self.view(h, ratings=True)
        limit = max(1, min(int(limit or 20), 50))
        slot_ids: tuple[int, ...] = ()
        if position:
            pos = self.POSITION_ALIASES.get(position.strip().upper(), position.strip().upper())
            reverse = {v.upper(): k for k, v in h.sport.slot_names.items()}
            if pos not in reverse:
                valid = sorted({h.sport.slot(k) for k in view.slot_counts if h.sport.is_starting_slot(k)})
                raise UserError(f"Unknown position {position!r}. Try one of: {', '.join(valid)}")
            slot_ids = (reverse[pos],)
        raw = h.client.player_pool(view.scoring_period, slot_ids=slot_ids, limit=150)
        rows = [view.player_row(p, show_fantasy_team=True) for p in raw.get("players") or []]
        key, label = self._fa_sort(view, sort_by)
        rows.sort(key=lambda r: (key(r) is not None, key(r) or 0), reverse=True)
        out = self._header(h, view) | {
            "period": view.period_label(),
            "position": position or "all",
            "sorted_by": label,
        }
        if view.is_football and not position:
            # Points differ a lot by position, so show the best few at each
            # position the league actually starts.
            real_positions = set(h.sport.default_positions.values())
            positions = [h.sport.slot(s) for s in sorted(view.slot_counts)
                         if h.sport.is_starting_slot(s) and h.sport.slot(s) in real_positions]
            per = max(3, limit // max(1, len(positions)))
            out["by_position"] = {pos: [r for r in rows if r.get("pos") == pos][:per] for pos in positions}
            out["tip"] = "Pass position=... for a deeper list at one position."
        else:
            out["players"] = rows[:limit]
        return out

    def _fa_sort(self, view: LeagueView, sort_by: str | None):
        s = (sort_by or "").strip().lower().replace(" ", "_")
        if s in ("owned", "ownership", "rostered"):
            return (lambda r: r.get("owned_pct")), "% rostered across ESPN"
        if s in ("trending", "hot", "adds"):
            return (lambda r: r.get("owned_change")), "change in % rostered (trending adds)"
        if view.is_football:
            if s in ("season", "season_avg", "avg"):
                def season_avg(r):
                    sf = r.get("season_fpts") or {}
                    return sf.get("avg") if (sf.get("games") or 0) >= 3 else None
                return season_avg, "season fantasy points per game (3+ games played)"
            if s in ("total", "season_total"):
                return (lambda r: (r.get("season_fpts") or {}).get("total")), "season fantasy points"
            return (lambda r: None if r.get("locked") else (r.get("week") or {}).get("proj")), "this week's projected points (players whose game already started are listed last)"
        window = {"l7": "l7", "last_7": "l7", "l15": "l15", "last_15": "l15", "recent": "l15", "": "l15",
                  "l30": "l30", "last_30": "l30", "season": "season", "season_avg": "season"}.get(s, "l15")
        if s in ("proj", "projected", "projection") and not view.is_categories:
            return (lambda r: (r.get("fpts") or {}).get("proj_avg")), "ESPN projected fantasy points per game"
        if view.is_categories:
            return (lambda r: (r.get("rater") or {}).get(window)), f"ESPN player rater ({window}) for your league's categories"
        return (lambda r: (r.get("fpts") or {}).get(f"{window}_avg")), f"fantasy points per game ({window})"

    def player(self, league=None, name: str = "") -> dict:
        h = self.resolve(league)
        view = self.view(h, ratings=True)
        if not name or not name.strip():
            raise UserError("Give a player name.")
        pick, matches = pick_player(name, h.client.player_index(), h.sport.pro_team)
        if not matches:
            raise UserError(f"No player found matching {name!r}.")
        if pick is None:
            return {"note": f"Several players match {name!r}; ask again with one of these exactly (team in brackets).",
                    "matches": [f"{m.get('fullName')} ({h.sport.pro_team(m.get('proTeamId'))})" for m in matches[:5]]}
        final = view.final_scoring_period or view.scoring_period
        if view.is_football:
            # this week's projection plus the last 4 weeks', so the game log shows projected vs actual
            extra = tuple(f"11{view.season}{w}" for w in range(max(1, view.scoring_period - 4), view.scoring_period + 1))
        else:
            extra = tuple(f"0{i}{view.season}" for i in (1, 2, 3))
        cards = h.client.player_cards([pick["id"]], final, extra)
        wraps = cards.get("players") or []
        if not wraps:
            raise UserError(f"ESPN returned no details for {pick.get('fullName')}.")
        wrap = wraps[0]
        p = player_of(wrap)
        out = self._header(h, view) | view.player_row(wrap, detail="full", show_fantasy_team=True)
        if "fantasy_team" not in out:
            out["fantasy_team"] = "Free agent / waivers"
        if view.is_football:
            proj_pts: dict[int, float] = {}
            if not view.is_categories:   # every week's projection from the public feed, in this league's scoring
                weeks = range(max(1, view.scoring_period - 8), view.scoring_period + 2)
                raw = h.client.public_projections([p.get("id")], weeks)
                proj_pts = projections.weekly_points(view, raw, {p.get("id"): view.sport.position(p)}).get(p.get("id"), {})
            out.update(self._football_card(view, p, proj_pts))
        else:
            out.update(self._basketball_card(view, p))
        news = self._news(h, pick["id"])
        if news:
            out["news"] = news
        return out

    def _football_card(self, view: LeagueView, p: dict, proj_pts: dict | None = None) -> dict:
        proj_pts = proj_pts or {}
        extra: dict[str, Any] = {}
        week = view.scoring_period
        outlooks = ((p.get("outlooks") or {}).get("outlooksByWeek") or {})
        if outlooks.get(str(week)):
            extra["espn_outlook_this_week"] = outlooks[str(week)][:700]
        if p.get("seasonOutlook"):
            extra["espn_season_outlook"] = p["seasonOutlook"][:700]
        log = []
        for w in range(1, week + 1):
            actual = find_split(p, 0, 1, view.season, period=w)
            proj = find_split(p, 1, 1, view.season, period=w)
            game = view.pro.game(p.get("proTeamId"), w)
            if not actual and not proj and w not in proj_pts:
                if game is None and w < week:
                    log.append({"week": w, "game": "BYE"})
                continue
            proj_v = (proj or {}).get("appliedTotal")
            if proj_v is None and w in proj_pts:
                proj_v = proj_pts[w]
            log.append(compact({
                "week": w,
                "game": view.pro.matchup_label(game),
                "pts": rnd((actual or {}).get("appliedTotal")),
                "proj": rnd(proj_v),
                "stats": view._football_stat_line(actual, limit=8) or None,
            }))
        extra["weekly_log"] = log[-10:]
        upcoming = []
        for w in range(week + 1, min(week + 4, (view.final_scoring_period or week) + 1)):
            game = view.pro.game(p.get("proTeamId"), w)
            upcoming.append(compact({
                "week": w,
                "game": view.pro.matchup_label(game) or "BYE",
                "proj": rnd(proj_pts.get(w)) if game and w in proj_pts else None,
                "opp_rank_vs_pos_now": view._oprk(p, game["opp_id"]) if game else None,
            }))
        if upcoming:
            extra["next_weeks"] = upcoming
        return extra

    def _basketball_card(self, view: LeagueView, p: dict) -> dict:
        names = view.bball_stat_names()
        games = []
        for s in p.get("stats") or []:
            if s.get("statSourceId") == 0 and s.get("statSplitTypeId") == 5 and s.get("seasonId") == view.season:
                games.append(s)
        games.sort(key=lambda s: s.get("scoringPeriodId") or 0, reverse=True)
        log = []
        for s in games[:10]:
            sp = s.get("scoringPeriodId")
            game = view.pro.game(p.get("proTeamId"), sp)
            line = view._bball_stat_line(s, [n for n in names if n != "GP"], average=False)
            row = {
                "date": str(view.pro.period_date.get(sp, "")) or None,
                "game": view.pro.matchup_label(game),
                "fpts": None if view.is_categories or not line else rnd(s.get("appliedTotal")),
                "line": line or "DNP",
            }
            log.append(compact(row))
        today = view.pro.today
        upcoming = [
            f"{fmt_date(g['start'])} {view.pro.matchup_label(g)}"
            for g in view.pro.upcoming_games(p.get("proTeamId"), today, today + dt.timedelta(days=6))
        ]
        return compact({"last_10_games": log, "next_7_days": upcoming})

    def _news(self, h: LeagueHandle, player_id: int) -> list[dict]:
        try:
            raw = h.client.news(player_id)
        except EspnError:
            return []
        feed = ((raw or {}).get("feed") or (raw or {}).get("news", {}).get("feed") or [])
        items = []
        for item in feed[:4]:
            story = item.get("story") or item.get("description") or ""
            story = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", story))).strip()
            items.append(compact({
                "published": (item.get("published") or "")[:16].replace("T", " ") or None,
                "headline": item.get("headline"),
                "story": story[:400] or None,
            }))
        return items

    def lineup_check(self, league=None, team=None, days_ahead: int = 0) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        tid = self._team(view, team)
        if view.season_over:
            return self._header(h, view) | {"note": "The season is over — no lineups to set."}
        period = view.scoring_period
        entries = view.roster_entries(tid)
        if days_ahead and not view.is_football:
            period = view.scoring_period + int(days_ahead)
            entries = view.roster_entries(tid, h.client.roster_for_period(period))
        entries = sorted(entries, key=view.slot_order)
        day_word = "today" if not days_ahead else ("tomorrow" if int(days_ahead) == 1 else f"on {view.pro.period_date.get(period, 'that day')}")
        out = analysis.lineup_check(view, tid, entries, period, day_word)
        self._add_pickup_options(h, view, out["issues"], period)
        if any(i.get("free_agent_options") for i in out["issues"]):
            out["about_pickups"] = (
                "Free agent options are streaming ideas only. A pickup needs a drop: only drop from the user's "
                "rotation/streaming spots, never a core player just to gain one game. Ask the user if unsure."
            )
        return self._header(h, view) | out

    def _add_pickup_options(self, h: LeagueHandle, view: LeagueView, issues: list[dict], period: int) -> None:
        """For holes the bench can't fill, suggest free agents who play then."""
        needs = [i for i in issues if "_needs_pickup_slot" in i]
        if not needs:
            return
        try:
            pool = h.client.player_pool(period, limit=150).get("players") or []
        except EspnError:
            pool = []
        for issue in needs:
            slot_id = issue.pop("_needs_pickup_slot")
            options = analysis.pickup_options(view, pool, slot_id, period) if pool else []
            if options:
                issue["free_agent_options"] = options

    def transactions(self, league=None, periods_back: int | None = None) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        back = periods_back if periods_back is not None else (1 if view.is_football else 6)
        back = max(0, min(int(back), 3 if view.is_football else 14))
        current = view.scoring_period
        raws = [h.client.transactions(sp) for sp in range(current, max(0, current - back - 1), -1) if sp >= 1]
        ids = {it.get("playerId") for r in raws for tx in r.get("transactions") or [] for it in tx.get("items") or []}
        names = self._names(h, ids, view.final_scoring_period)
        span = f"weeks {max(1, current - back)}-{current}" if view.is_football else f"last {back + 1} days"
        return self._header(h, view) | {"covering": span, "transactions": view.transactions(raws, names)}

    def draft(self, league=None, team=None) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        tid = self._team(view, team) if team else None
        raw = h.client.draft()
        ids = {p.get("playerId") for p in (raw.get("draftDetail") or {}).get("picks") or []}
        names = self._names(h, ids, view.final_scoring_period)
        return self._header(h, view) | view.draft(raw, names, tid)

    def refresh(self) -> dict:
        for h in self.handles():
            h.client.clear()
        return {"ok": True, "message": "Cleared cached ESPN data; the next request fetches fresh data."}

    # ---- strategy ----------------------------------------------------------------
    def power_rankings(self, league=None) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        return self._header(h, view) | strategy.power_rankings(view)

    def category_report(self, league=None, team=None) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        tid = self._team(view, team)
        raw = None
        if view.is_categories and not view.season_over:
            raw = h.client.scoreboard(view.current_matchup_period, view.scoring_period)
        return self._header(h, view) | strategy.category_report(view, tid, raw)

    # ---- category-league brain ---------------------------------------------------
    def _need_categories(self, view: LeagueView, tool: str) -> None:
        if not view.is_categories:
            raise UserError(f"{tool} is for category leagues; this league is scored by points. "
                            "Use power_rankings, waiver_upgrades and trade_analyzer instead.")

    def category_strategy(self, league=None, team=None) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        self._need_categories(view, "category_strategy")
        tid = self._team(view, team)
        pool = h.client.player_pool(view.scoring_period, limit=150).get("players") or []
        return self._header(h, view) | {"team": view.team_label(tid)} | catmodel.category_strategy(view, pool, tid)

    def player_values(self, league=None, punt=None, who: str = "all", limit: int = 30, recent: bool = False) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        self._need_categories(view, "player_values")
        pool = h.client.player_pool(view.scoring_period, limit=150).get("players") or []
        who = who if who in ("all", "free", "rostered") else "all"
        return self._header(h, view) | catmodel.player_values(view, pool, punt, who, max(1, min(int(limit), 60)), recent)

    def schedule_planner(self, league=None, team=None) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        tid = self._team(view, team)
        if view.is_football:
            return self._header(h, view) | strategy.football_schedule(view, tid)
        return self._header(h, view) | {"team": view.team_label(tid)} | catmodel.schedule_planner(view, tid)

    # ---- football world (shared by outlook, trades, waivers) ---------------------------
    WORLD_TTL = 60

    def _need_football_points(self, view: LeagueView, tool: str) -> None:
        if not view.is_football or view.is_categories:
            raise UserError(f"{tool} works on football points leagues here; for basketball category leagues "
                            "use category_strategy, schedule_planner and the basketball season_outlook.")

    def _projection_points(self, h: LeagueHandle, view: LeagueView, extra_wraps=()) -> dict[int, dict[int, float]]:
        """Complete weekly projections (recent weeks + this and next week) for every rostered
        player and the given pool, scored with this league's rules."""
        positions: dict[int, str] = {}
        for tid in view.teams:
            for e in view.roster_entries(tid):
                p = player_of(e)
                positions[p.get("id")] = view.sport.position(p)
        for w in extra_wraps:
            p = player_of(w)
            positions[p.get("id")] = view.sport.position(p)
        weeks = range(max(1, view.scoring_period - ffmodel.HIST_WEEKS), view.scoring_period + 2)
        raw = h.client.public_projections([i for i in positions if i], weeks)
        return projections.weekly_points(view, raw, positions) if raw else {}

    def football_world(self, h: LeagueHandle, view: LeagueView) -> ffmodel.FootballWorld:
        key = (h.cfg.sport, h.cfg.league_id, view.scoring_period)
        hit = self._worlds.get(key)
        if hit and time.monotonic() - hit[0] < self.WORLD_TTL:
            return hit[1]
        cur = view.current_matchup_period
        past = []
        for mp in range(max(1, cur - ffmodel.HIST_WEEKS), cur):
            try:
                past.append(h.client.scoreboard(mp, view._matchup_week(mp), final=True))
            except EspnError:
                continue
        live = None if view.season_over else h.client.scoreboard(cur, view.scoring_period)
        pool = h.client.player_pool(view.scoring_period, limit=150).get("players") or []
        proj = self._projection_points(h, view, pool)
        world = ffmodel.FootballWorld(view, live, past, pool, proj)
        self._worlds = {k: v for k, v in self._worlds.items() if time.monotonic() - v[0] < self.WORLD_TTL}
        self._worlds[key] = (time.monotonic(), world)
        return world

    def category_season(self, h: LeagueHandle, view: LeagueView) -> catseason.CategorySeason:
        key = ("cat", h.cfg.sport, h.cfg.league_id, view.scoring_period)
        hit = self._worlds.get(key)
        if hit and time.monotonic() - hit[0] < self.WORLD_TTL:
            return hit[1]
        pool = h.client.player_pool(view.scoring_period, limit=150).get("players") or []
        live = None
        if not view.season_over:
            try:
                live = h.client.scoreboard(view.current_matchup_period, view.scoring_period)
            except EspnError:
                live = None
        season = catseason.CategorySeason(view, pool, live)
        self._worlds = {k: v for k, v in self._worlds.items() if time.monotonic() - v[0] < self.WORLD_TTL}
        self._worlds[key] = (time.monotonic(), season)
        return season

    def season_outlook(self, league=None, team=None) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        if view.is_categories:
            tid = self._team(view, team)
            return self._header(h, view) | catseason.season_outlook(self.category_season(h, view), tid)
        self._need_football_points(view, "season_outlook")
        tid = self._team(view, team)
        world = self.football_world(h, view)
        return self._header(h, view) | ffmodel.season_outlook(view, None, [], [], tid, world=world)

    # ---- league memory and track record --------------------------------------------
    @staticmethod
    def _parallel(fn, items, workers: int = 6) -> list:
        items = list(items)
        if not items:
            return []
        with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
            return list(pool.map(fn, items))

    def _all_transactions(self, h: LeagueHandle, view: LeagueView) -> tuple[list[int], list[dict]]:
        """Every period's transactions this season (basketball: the last 28 days), in parallel."""
        cur = view.scoring_period
        periods = list(range(1, cur + 1)) if view.is_football else list(range(max(1, cur - 27), cur + 1))

        def fetch(sp):
            try:
                return h.client.transactions(sp, final=sp < cur)
            except EspnError:
                return {}

        return periods, self._parallel(fetch, periods)

    def league_intel(self, league=None) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        me = self._team(view, None)
        cur = view.scoring_period
        periods, raws = self._all_transactions(h, view)
        ids = {it.get("playerId") for r in raws for tx in r.get("transactions") or [] for it in tx.get("items") or []}
        ids |= {int(pid) for t in view.teams.values() for pid in ((t.get("tradeBlock") or {}).get("players") or {})}
        names = self._names(h, ids, view.final_scoring_period)
        odds = needs = None
        live_season = not view.season_over and view.current_matchup_period <= (view.reg_season_periods or 0)
        if view.is_football and not view.is_categories:
            world = self.football_world(h, view)
            needs = intel.football_needs(world)
            if live_season:
                odds = world.simulate(world.strengths(world.all_rosters()), sims=3000, seed=7, tau=4.0)["teams"]
        elif view.is_categories and view.scoring_type != "ROTO":
            season = self.category_season(h, view)
            needs = intel.category_needs(season.m, list(view.teams))
            if live_season:
                odds = season.simulate(season.rosters(), catseason.SCENARIOS[0], sims=3000, seed=7)["teams"]
        span = f"weeks 1-{cur}" if view.is_football else f"the last {len(periods)} days"
        return self._header(h, view) | intel.league_intel(view, me, raws, names, odds, needs, span)

    def track_record(self, league=None) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        me = self._team(view, None)
        if view.is_categories:
            return self._header(h, view) | intel.category_track_record(view, me)
        self._need_football_points(view, "track_record")
        cur = view.current_matchup_period if not view.season_over else max(view.matchup_periods or [1]) + 1

        def fetch(mp):
            try:
                return mp, h.client.scoreboard(mp, view._matchup_week(mp), final=True)
            except EspnError:
                return mp, None

        past = {mp: raw for mp, raw in self._parallel(fetch, range(1, cur)) if raw}
        sd, bias, _ = ffmodel.score_noise(view, list(past.values()))
        return self._header(h, view) | intel.football_track_record(view, me, past, sd, bias)

    # ---- outside data ------------------------------------------------------------------
    def _league_players(self, h: LeagueHandle, view: LeagueView, pool=None) -> dict[int, dict]:
        """Every rostered player plus the top free agents: {id: name, pos, team, owner, waivers}."""
        out: dict[int, dict] = {}
        for tid in view.teams:
            for e in view.roster_entries(tid):
                p = player_of(e)
                out[p.get("id")] = {"name": p.get("fullName"), "pos": view.sport.position(p),
                                    "nfl": view.sport.pro_team(p.get("proTeamId")), "owner": tid}
        if pool is None:
            pool = h.client.player_pool(view.scoring_period, limit=150).get("players") or []
        for w in pool:
            p = player_of(w)
            if p.get("id") not in out:
                out[p.get("id")] = {"name": p.get("fullName"), "pos": view.sport.position(p),
                                    "nfl": view.sport.pro_team(p.get("proTeamId")), "owner": "FA",
                                    "waivers": ppe_of(w).get("status") == "WAIVERS"}
        return out

    def _sleeper_to_espn(self, view: LeagueView, league_players: dict):
        """A resolver from Sleeper player id to ESPN id (NFL: id map; NBA: name + team)."""
        if view.is_football:
            idx = insights.nfl_index(external.nfl_id_map())
            return idx["sleeper"].get, bool(idx["sleeper"])
        directory = external.sleeper_players("nba") or {}
        by_name: dict[str, list[int]] = {}
        for pid, p in league_players.items():
            by_name.setdefault(views_norm(p["name"] or ""), []).append(pid)

        def resolve(sid):
            sp = directory.get(str(sid)) or {}
            hits = by_name.get(views_norm(sp.get("full_name") or ""), [])
            if len(hits) > 1 and sp.get("team"):
                hits = [i for i in hits if (league_players[i].get("nfl") or "").upper() == sp["team"].upper()] or hits
            return hits[0] if len(hits) == 1 else None

        return resolve, bool(directory)

    def market_trends(self, league=None) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        sport = "nfl" if view.is_football else "nba"
        adds, drops = external.sleeper_trending(sport, "add"), external.sleeper_trending(sport, "drop")
        if adds is None and drops is None:
            raise UserError("Sleeper's trending data is unavailable right now; try again later.")
        players = self._league_players(h, view)
        resolve, ok = self._sleeper_to_espn(view, players)
        out = insights.market_trends(view, adds, drops, resolve, players)
        if not ok:
            out["note"] += " (Player matching data was unavailable, so some players couldn't be named.)"
        return self._header(h, view) | {"source": "Sleeper (all leagues, last 24 hours)"} | out

    def player_usage(self, league=None, team=None, players=None) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        self._need_football_points(view, "player_usage")
        tid = self._team(view, team)
        world = self.football_world(h, view)
        weekly = external.nflverse_weekly(view.season)
        if weekly is None:
            raise UserError("nflverse usage data is unavailable right now; try again later.")
        snaps = external.nflverse_snaps(view.season)
        idx = insights.nfl_index(external.nfl_id_map())
        mine = world.team(tid)
        extra = []
        for n in players or []:
            p = world.by_name(n)
            if p is None:
                raise UserError(f"Couldn't match {n!r} to one player in this league's pool.")
            extra.append(p)
        fas = sorted((world.players[i] for i in world.free_agents if world.players[i]["pos"] in ("QB", "RB", "WR", "TE")),
                     key=lambda p: -p["base"])[:40]
        table = {r["player"]: r for r in insights.usage_table(weekly, snaps, idx, mine + extra + fas)}
        rising = [table[p["name"]] for p in fas if p["name"] in table
                  and any("rising" in f for f in table[p["name"]].get("flags") or [])]
        return self._header(h, view) | compact({
            "team": view.team_label(tid),
            "your_players": [table[p["name"]] for p in mine if p["name"] in table] or None,
            "asked_about": [table[p["name"]] for p in extra if p["name"] in table] or None,
            "free_agents_with_growing_roles": rising[:10] or None,
            "how_to_read": "Each 'a / b' is season average / last 3 games. Volume (snaps, targets, target share, "
                           "carries) is stable and predictive; touchdowns mostly aren't, so a TD-dependent player "
                           "is a sell and volume without TDs is a buy.",
            "source": "nflverse weekly stats and snap counts" + ("" if snaps else " (snap counts unavailable)"),
        })

    def _target_week(self, view: LeagueView) -> tuple[int, dict | None, list[str]]:
        """The NFL week to plan for: this week while most of its games are still to play; once
        only the Monday/late games are left, next week (the leftover games are listed)."""
        week = view.scoring_period
        sb = external.nfl_scoreboard(view.season, week)
        games = insights.game_lines(sb)["games"]
        left = [g for g in games if g["status"] == "STATUS_SCHEDULED"]
        if sb is not None and games and len(left) <= 2:
            nxt = external.nfl_scoreboard(view.season, week + 1)
            if nxt is not None:
                return week + 1, nxt, [f"{g['away']} @ {g['home']}" for g in left]
        return week, sb, []

    def game_environment(self, league=None, team=None) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        tid = self._team(view, team)
        if not view.is_football:
            return self._header(h, view) | self._nba_environment(h, view, tid)
        week, sb, leftover = self._target_week(view)
        if sb is None:
            raise UserError("ESPN's game lines are unavailable right now; try again later.")
        lines = insights.game_lines(sb)
        points = insights.weather_points(lines["games"])
        forecasts = external.weather([pt for _, pt in points]) if points else []
        fc = {gid: f for (gid, _), f in zip(points, forecasts or [])}
        mine_by_team: dict[int, list[str]] = {}
        for e in view.roster_entries(tid):
            p = player_of(e)
            if view.sport.is_starting_slot(e.get("lineupSlotId")) or e.get("lineupSlotId") == view.sport.bench_slot:
                mine_by_team.setdefault(p.get("proTeamId"), []).append(p.get("fullName"))
        games = []
        for g in sorted(lines["games"], key=lambda g: -(g.get("total") or 0)):
            games.append(compact({
                "game": f"{g['away']} @ {g['home']}", "kickoff_utc": g["start"],
                "line": f"{g['home']} {g['spread']:+g}" if g.get("spread") is not None else None,
                "total": g.get("total"),
                "implied_points": f"{g['away']} {g['away_implied']} / {g['home']} {g['home_implied']}" if "home_implied" in g else None,
                "indoor": True if g["indoor"] else None,
                "weather": insights.weather_flags(g, fc.get(g["id"])) if g["id"] in fc else None,
                "your_players": (mine_by_team.get(g["home_id"], []) + mine_by_team.get(g["away_id"], [])) or None,
            }))
        out = {"week": week, "still_to_play_last_week": leftover or None, "games": games,
               "teams_on_bye": [view.sport.pro_team(t) for t in lines["byes"]] or None,
               "source": "ESPN game lines (DraftKings) and Open-Meteo forecasts"
                         + (" (forecasts unavailable)" if points and forecasts is None else "")}
        if not view.is_categories:
            world = self.football_world(h, view)
            out["dst_streaming"] = insights.dst_board(view, world, lines, week, tid)
        out["how_to_read"] = ("Implied points = each team's expected score from the spread and total: higher means more "
                              "scoring chances for its players, lower is better for the defense facing it. ESPN "
                              "projections already price most of this; use it to break close start/sit calls.")
        return self._header(h, view) | compact(out)

    def _nba_environment(self, h: LeagueHandle, view: LeagueView, tid) -> dict:
        today = view.pro.today
        mine_by_team: dict[int, list[str]] = {}
        for e in view.roster_entries(tid):
            p = player_of(e)
            mine_by_team.setdefault(p.get("proTeamId"), []).append(p.get("fullName"))
        days = []
        for d in (today, today + dt.timedelta(days=1)):
            lines = insights.game_lines(external.nba_scoreboard(d))
            games = []
            for g in sorted(lines["games"], key=lambda g: -(g.get("total") or 0)):
                games.append(compact({
                    "game": f"{g['away']} @ {g['home']}", "total": g.get("total"),
                    "line": f"{g['home']} {g['spread']:+g}" if g.get("spread") is not None else None,
                    "blowout_risk": True if g.get("spread") is not None and abs(g["spread"]) >= 12 else None,
                    "your_players": (mine_by_team.get(g["home_id"], []) + mine_by_team.get(g["away_id"], [])) or None,
                }))
            days.append({"date": f"{d:%a %b %d}", "games": games or "no games or lines yet"})
        return {"days": days, "how_to_read": "Higher totals mean faster, higher-scoring games (more counting stats); "
                "a 12+ point spread risks a blowout with fewer minutes for starters."}

    def waiver_plan(self, league=None, team=None) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        tid = self._team(view, team)
        pool = h.client.player_pool(view.scoring_period, limit=150).get("players") or []
        players = self._league_players(h, view, pool)
        trending: set = set()
        adds = external.sleeper_trending("nfl" if view.is_football else "nba", "add")
        if adds:
            resolve, _ = self._sleeper_to_espn(view, players)
            trending = {resolve(str(a.get("player_id"))) for a in adds[:30]} - {None}
        if view.is_categories:
            strat = catmodel.category_strategy(view, pool, tid)
            picks = strat.get("best_pickups_for_you") or []
            ids = {p["name"]: i for i, p in players.items()}
            for r in picks:
                if ids.get(r["name"]) in trending:
                    r["trending_on_sleeper"] = True
            sched = catmodel.schedule_planner(view, tid)
            return self._header(h, view) | compact({
                "team": view.team_label(tid),
                "rotation_spots": strat.get("rotation_spots"),
                "best_moves": picks or "No free agent beats your rotation spots right now.",
                "best_streaming_days": sched.get("best_streaming_days"),
                "rules": ["Only your two rotation spots (worth least over a typical free agent) are suggested as "
                          "drops; core players are never.", "Weekly win % gains come from the league's calibrated "
                          "category model; pick up for the days you have open slots."],
            })
        self._need_football_points(view, "waiver_plan")
        world = self.football_world(h, view)
        faab = left = None
        if view.uses_faab:
            _, raws = self._all_transactions(h, view)
            txs, _ = intel.parse_transactions(raws)
            ids = {it.get("playerId") for tx in txs for it in tx.get("items") or []}
            faab = intel.faab_summary(intel.faab_bids(view, txs, self._names(h, ids, view.final_scoring_period)),
                                      view.faab_budget)
            spent = num((view.teams.get(tid, {}).get("transactionCounter") or {}).get("acquisitionBudgetSpent"))
            left = view.faab_budget - spent
        out = insights.waiver_plan_football(world, tid, faab, left, trending)
        return self._header(h, view) | {"team": view.team_label(tid)} | compact(out)

    def strategy_playbook(self, league=None) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        s = view.settings_view()
        acq = s.get("waivers_and_free_agents") or {}
        trades = s.get("trades") or {}
        sched = s.get("schedule") or {}
        if view.is_football:
            name = "football_points.md"
        elif view.is_categories:
            name = "basketball_categories.md"
        else:
            name = "basketball_points.md"
        path = Path(__file__).parent / "playbooks" / name
        if not path.exists():
            raise UserError("No playbook for this league type yet.")
        cats = ", ".join(c["name"] for c in view.categories)
        need = len(view.categories) // 2 + 1
        if view.scoring_type == "H2H_MOST_CATEGORIES":
            fmt = (f"Head-to-head MOST categories: each week is a single win or loss for taking a majority "
                   f"({need} of {len(view.categories)}) of: {cats}. Margins don't matter.")
        elif view.scoring_type == "H2H_CATEGORY":
            fmt = f"Head-to-head EACH category: every category ({cats}) is its own win/loss each week."
        elif view.scoring_type == "ROTO":
            fmt = f"Rotisserie: season totals ranked in each of {cats}; punting is much weaker here."
        else:
            fmt = f"{s.get('format')}."
        playoff = f"{sched.get('playoff_teams', '?')} teams make the playoffs after {sched.get('regular_season_matchups', '?')} regular-season matchups."
        extra: dict[str, str] = {}
        if view.is_football:
            fmt = f"Head-to-head points: {s.get('scoring_summary') or s.get('format')}."
            reg = view.reg_season_periods or 0
            weeks = [w for mp in sorted(view.matchup_periods) if mp > reg for w in view.matchup_periods[mp]]
            if weeks:
                playoff += f" Playoff weeks: NFL weeks {weeks[0]}-{weeks[-1]}."
            if sched.get("playoff_teams") == 6:
                playoff += " Seeds 1-2 skip the first round."
            if sched.get("playoff_seeding_tiebreak"):
                playoff += f" Seeding tiebreak: {str(sched['playoff_seeding_tiebreak']).replace('_', ' ').lower()}."
            ks = []
            for t in view.teams:
                for e in view.roster_entries(t):
                    p = player_of(e)
                    if view.sport.position(p) == "K":
                        a = num((find_split(p, 0, 0, view.season) or {}).get("appliedAverage"))
                        if a:
                            ks.append(a)
            if ks:
                k_avg = sum(ks) / len(ks)
                extra["kicker_line"] = (
                    f"Kickers here average {k_avg:.1f} points a game"
                    + (" -- almost nothing. Never spend a pickup, FAAB or a roster spot decision on a kicker."
                       if k_avg < 4 else ". A better kicker is worth a free pickup but not FAAB.")
                )
            else:
                extra["kicker_line"] = "Check how this league scores kickers before streaming them."
        if not view.is_football:
            cal = {u["unit"]: u for u in catmodel.nba_calendar(view)}
            reg = view.reg_season_periods or 0
            spans = []
            for mp in sorted(view.matchup_periods):
                if mp > reg:
                    units = [cal[u] for u in view.matchup_periods[mp] if u in cal]
                    if units:
                        spans.append(f"{units[0]['start']:%b %d}-{units[-1]['end']:%b %d}")
            if spans:
                playoff += " Playoff weeks: " + ", ".join(spans) + "."
        fill = {
            "format_line": fmt,
            "pickup_line": f"Pickup limit: {acq['add_limit']}." if acq.get("add_limit") else "No weekly pickup limit.",
            "waiver_line": (
                f"Waivers: {acq.get('type', '?')}"
                + (f", FAAB budget ${acq['faab_budget']}" if acq.get("faab_budget") else ", waiver priority order")
                + (f"; claims process {'/'.join(d.title()[:3] for d in acq.get('process_days', []))}" if acq.get("process_days") else "")
                + ". Players you drop sit on waivers before others can add them."
            ),
            "trade_line": (
                f"Trade deadline: {trades.get('deadline', 'none set')}. Trades are reviewed for {trades.get('review_hours', 0)}h"
                + (f" and {trades['veto_votes_required']} veto votes kill a deal, so offers must look fair to the league."
                   if trades.get("veto_votes_required") else ".")
            ),
            "playoff_line": playoff,
        } | extra
        text = path.read_text(encoding="utf-8")
        for k, v in fill.items():
            text = text.replace("{" + k + "}", v)
        return self._header(h, view) | {"playbook": text}

    def matchup_projection(self, league=None, team=None) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        tid = self._team(view, team)
        raw = h.client.scoreboard(view.current_matchup_period, view.scoring_period)
        return self._header(h, view) | {"period": view.period_label()} | strategy.matchup_projection(view, raw, tid)

    def waiver_upgrades(self, league=None, team=None) -> dict:
        h = self.resolve(league)
        view = self.view(h, ratings=True)
        tid = self._team(view, team)
        pool = h.client.player_pool(view.scoring_period, limit=150).get("players") or []
        return self._header(h, view) | strategy.waiver_upgrades(view, tid, pool)

    def _find_owner(self, view: LeagueView, name: str, exclude=None) -> tuple[int, dict]:
        hits = []
        for tid in view.teams:
            if tid == exclude:
                continue
            try:
                hits.append((tid, writes.find_on_roster(view, view.roster_entries(tid), name)))
            except writes.MoveError:
                continue
        if len(hits) != 1:
            raise UserError(
                f"Couldn't find exactly one rostered player matching {name!r}"
                + (" on another team." if exclude else ".")
            )
        return hits[0]

    def _trade_sides(self, view: LeagueView, me: int, give, receive, partner) -> tuple[int, list[dict], list[dict]]:
        """Resolve a trade's names to roster entries: (partner id, give wraps, receive wraps)."""
        give, receive = list(give or []), list(receive or [])
        try:
            give_w = [writes.find_on_roster(view, view.roster_entries(me), n) for n in give]
        except writes.MoveError as e:
            raise UserError(str(e)) from None
        partner_id = self._team(view, partner) if partner else None
        if partner_id == me:
            raise UserError("That's your own team; name the team you'd trade with.")
        recv_w = []
        for n in receive:
            if partner_id:
                try:
                    recv_w.append(writes.find_on_roster(view, view.roster_entries(partner_id), n))
                except writes.MoveError as e:
                    raise UserError(str(e)) from None
            else:
                tid, w = self._find_owner(view, n, exclude=me)
                partner_id = tid
                recv_w.append(w)
        if partner_id is None:
            raise UserError("Say which team you're trading with.")
        return partner_id, give_w, recv_w

    def trade_analyzer(self, league=None, give=None, receive=None, partner=None) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        me = self._team(view, None)
        if not give and not receive:
            raise UserError("Name the players you'd give and/or receive.")
        partner_id, give_w, recv_w = self._trade_sides(view, me, give, receive, partner)
        if view.is_football and not view.is_categories:
            world = self.football_world(h, view)
            g = [world.players[player_of(w).get("id")] for w in give_w]
            r = [world.players[player_of(w).get("id")] for w in recv_w]
            out = trades.evaluate_trade(world, me, partner_id, g, r)
            closed = trades.trade_window(view)
            if closed:
                out["notes"].insert(0, closed)
            return self._header(h, view) | {"partner": view.team_label(partner_id)} | out
        base = strategy.trade_analysis(view, me, partner_id, give_w, recv_w)
        if view.is_categories:
            try:
                base |= catseason.evaluate_trade(self.category_season(h, view), me, partner_id,
                                                 [player_of(w).get("id") for w in give_w],
                                                 [player_of(w).get("id") for w in recv_w])
            except ValueError as e:
                raise UserError(str(e)) from None
            closed = trades.trade_window(view)
            if closed:
                base["notes"] = [closed] + list(base.get("notes") or [])
        return self._header(h, view) | base

    def find_trades(self, league=None, partner=None, must_receive=None, must_give=None, max_players: int = 3,
                    deep: bool = False) -> dict:
        h = self.resolve(league)
        view = self.view(h)
        me = self._team(view, None)
        closed = trades.trade_window(view)
        if closed:
            return self._header(h, view) | {"note": closed}
        max_players = max(1, min(int(max_players or 3), 3))
        partner_id = self._team(view, partner) if partner else None
        if partner_id == me:
            raise UserError("That's your own team; name the team you'd trade with.")
        must_receive, must_give = list(must_receive or []), list(must_give or [])
        recv_ids, owners = [], set()
        for n in must_receive:
            if partner_id:
                try:
                    w = writes.find_on_roster(view, view.roster_entries(partner_id), n)
                except writes.MoveError as e:
                    raise UserError(str(e)) from None
                owners.add(partner_id)
            else:
                tid, w = self._find_owner(view, n, exclude=me)
                owners.add(tid)
            recv_ids.append(player_of(w).get("id"))
        if len(owners) > 1:
            raise UserError("The players you must receive are on different teams; a trade is with one team.")
        partner_id = partner_id or (owners.pop() if owners else None)
        try:
            give_ids = [player_of(writes.find_on_roster(view, view.roster_entries(me), n)).get("id") for n in must_give]
        except writes.MoveError as e:
            raise UserError(str(e)) from None
        if view.is_categories:
            partners = [partner_id] if partner_id else [t for t in view.teams if t != me]
            return self._header(h, view) | catseason.find_trades(self.category_season(h, view), me, partners,
                                                                 recv_ids, give_ids, max_players,
                                                                 budget_s=75.0 if deep else 22.0)
        self._need_football_points(view, "find_trades")
        world = self.football_world(h, view)
        partners = [partner_id] if partner_id else [t for t in view.teams if t != me]
        out = trades.find_trades(world, me, partners,
                                 must_get=[world.players[i] for i in recv_ids],
                                 must_give=[world.players[i] for i in give_ids],
                                 max_size=max_players, budget_s=75.0 if deep else 22.0)
        return self._header(h, view) | out

    # ---- moves (writes) ---------------------------------------------------------
    def _write_context(self, league):
        if not self.settings.allow_writes:
            raise UserError(
                "Moves are turned off. Set ESPN_ALLOW_WRITES=1 (or allow_writes = true in the config) to enable them."
            )
        if not self.settings.has_credentials:
            raise UserError("Moves need your ESPN login cookies (espn_s2 and SWID).")
        h = self.resolve(league)
        view = self.view(h)
        me = view.my_team_id
        if me is None:
            raise UserError("Couldn't identify your team in this league, so I can't make moves for it.")
        return h, view, me

    def _submit(self, h: LeagueHandle, payload: dict) -> dict:
        try:
            body = writes.post_transaction(h.sport, h.season, h.cfg.league_id, self.settings.cookies(), payload)
        except writes.MoveError as e:
            raise UserError(str(e)) from None
        h.client.clear()
        return compact({
            "espn_status": body.get("status"),
            "transaction_id": body.get("id"),
            "is_pending": body.get("isPending"),
        })

    PREVIEW_NOTE = "PREVIEW ONLY — nothing was sent. Show this to the user; if they approve, call again with the same arguments and confirm=true."

    def set_lineup(self, league=None, start=None, bench=None, moves=None, days_ahead: int = 0, confirm: bool = False) -> dict:
        h, view, me = self._write_context(league)
        period = view.scoring_period
        entries = view.roster_entries(me)
        if days_ahead and not view.is_football:
            period += int(days_ahead)
            entries = view.roster_entries(me, h.client.roster_for_period(period))
        try:
            plan, steps, warnings = writes.plan_lineup(view, entries, start, bench, moves, future=period > view.scoring_period)
        except writes.MoveError as e:
            raise UserError(str(e)) from None
        when = f"Week {period}" if view.is_football else str(view.pro.period_date.get(period, f"scoring period {period}"))
        out: dict[str, Any] = self._header(h, view) | {"team": view.team_label(me), "lineup_for": when, "changes": steps}
        if warnings:
            out["warnings"] = warnings
        if not confirm:
            return out | {"status": self.PREVIEW_NOTE}
        result = self._submit(h, writes.lineup_payload(me, self.settings.swid, period, plan))
        # verify by re-reading the lineup
        fresh = h.client.roster_for_period(period) if (days_ahead and not view.is_football) else None
        view2 = self.view(h)
        now_slots = {player_of(e).get("id"): e.get("lineupSlotId") for e in view2.roster_entries(me, fresh)}
        verified = all(now_slots.get(pid) == to for pid, _, to in plan)
        return out | {"status": "SUBMITTED", "verified_in_espn": verified} | result

    def add_player(self, league=None, add: str = "", drop: str | None = None, bid: int | None = None, confirm: bool = False) -> dict:
        h, view, me = self._write_context(league)
        pick, matches = pick_player(add, h.client.player_index(), h.sport.pro_team)
        if not matches:
            raise UserError(f"No player found matching {add!r}.")
        if pick is None:
            raise UserError("Several players match: " + ", ".join(
                f"{m.get('fullName')} ({h.sport.pro_team(m.get('proTeamId'))})" for m in matches[:5]
            ) + ". Ask again with the name and team exactly as shown.")
        card = (h.client.player_cards([pick["id"]], view.final_scoring_period or view.scoring_period).get("players") or [{}])[0]
        status, owner = card.get("status"), card.get("onTeamId")
        if owner:
            raise UserError(f"{pick.get('fullName')} is on {view.team_label(owner)}. Use the trade tools instead.")
        drop_w = None
        my_entries = view.roster_entries(me)
        if drop:
            try:
                drop_w = writes.find_on_roster(view, my_entries, drop)
            except writes.MoveError as e:
                raise UserError(str(e)) from None
            if ppe_of(drop_w).get("lineupLocked"):
                raise UserError(f"{player_of(drop_w).get('fullName')} is locked (game started) and can't be dropped right now.")
        active = [e for e in my_entries if e.get("lineupSlotId") != h.sport.ir_slot]
        warnings = []
        if len(active) >= writes.roster_limit(view) and not drop_w:
            raise UserError(f"Your roster is full ({len(active)} players). Say who to drop.")
        waiver = status == "WAIVERS"
        if waiver and view.uses_faab and bid is None:
            warnings.append("This league uses FAAB and no bid was given, so the claim would bid $0.")
        if bid is not None and not view.uses_faab:
            warnings.append("This league doesn't use FAAB; the bid is ignored and waiver priority decides.")
        comparison = None
        if drop_w:
            add_val = strategy.season_value(view, card)
            drop_val = strategy.season_value(view, drop_w)
            basis = strategy.value_labels(view)[0]
            drop_name = player_of(drop_w).get("fullName")
            comparison = {
                "basis": basis,
                "adding": {"name": pick.get("fullName"), "season_value": rnd(add_val, 2)},
                "dropping": {"name": drop_name, "season_value": rnd(drop_val, 2)},
            }
            if drop_val is not None and (add_val is None or drop_val > add_val):
                warnings.insert(
                    0,
                    f"CAUTION: {drop_name} has MORE season value than {pick.get('fullName')} "
                    f"({basis}: {rnd(drop_val, 2)} vs {rnd(add_val, 2) if add_val is not None else 'n/a'}). "
                    "Dropping him is probably a mistake unless he's one of the user's rotation spots.",
                )
        steps = [f"{'Claim off waivers' if waiver else 'Add free agent'}: {pick.get('fullName')}"]
        if drop_w:
            steps.append(f"Drop: {player_of(drop_w).get('fullName')}")
        if waiver and view.uses_faab:
            steps.append(f"FAAB bid: ${int(bid or 0)} (you have ${view.team_overview(me).get('faab_left', '?')} left)")
        out: dict[str, Any] = self._header(h, view) | {"team": view.team_label(me), "changes": steps}
        if comparison:
            out["value_check"] = comparison
        if waiver:
            out["note"] = "Waiver claims process at the league's waiver time; this only submits the claim."
        if warnings:
            out["warnings"] = warnings
        if not confirm:
            return out | {"status": self.PREVIEW_NOTE}
        payload = writes.add_drop_payload(
            me, self.settings.swid, view.scoring_period,
            add_id=pick["id"], drop_id=player_of(drop_w).get("id") if drop_w else None,
            waiver=waiver, bid=bid if view.uses_faab else None,
        )
        result = self._submit(h, payload)
        if not waiver:
            view2 = self.view(h)
            result["verified_in_espn"] = any(player_of(e).get("id") == pick["id"] for e in view2.roster_entries(me))
        return out | {"status": "SUBMITTED"} | result

    def drop_player(self, league=None, player: str = "", confirm: bool = False) -> dict:
        h, view, me = self._write_context(league)
        try:
            w = writes.find_on_roster(view, view.roster_entries(me), player)
        except writes.MoveError as e:
            raise UserError(str(e)) from None
        if ppe_of(w).get("lineupLocked"):
            raise UserError(f"{player_of(w).get('fullName')} is locked (game started) and can't be dropped right now.")
        out = self._header(h, view) | {"team": view.team_label(me), "changes": [f"Drop: {player_of(w).get('fullName')}"]}
        if not confirm:
            return out | {"status": self.PREVIEW_NOTE}
        result = self._submit(h, writes.add_drop_payload(me, self.settings.swid, view.scoring_period, drop_id=player_of(w)["id"]))
        return out | {"status": "SUBMITTED"} | result

    def propose_trade(self, league=None, partner=None, give=None, receive=None, confirm: bool = False) -> dict:
        h, view, me = self._write_context(league)
        partner_id, give_w, recv_w = self._trade_sides(view, me, give, receive, partner)
        analysis_out = self.trade_analyzer(league, give, receive, str(partner_id))
        give_ids = [player_of(w)["id"] for w in give_w]
        get_ids = [player_of(w)["id"] for w in recv_w]
        keys = ("verdict", "your_odds", "starting_lineup_change_per_week", "summary",
                "your_starting_lineup_value", "their_starting_lineup_value", "notes")
        out = self._header(h, view) | {
            "proposal": {"to": view.team_label(partner_id), "you_give": give, "you_get": receive, "expires_in_days": 2},
            "analysis": {k: analysis_out[k] for k in keys if k in analysis_out},
            "experimental": "Trade proposals use an ESPN endpoint that hasn't been verified on a live league yet. Check the ESPN app after sending.",
        }
        if not confirm:
            return out | {"status": self.PREVIEW_NOTE}
        payload = writes.trade_payload(me, self.settings.swid, view.scoring_period, partner_id, give_ids, get_ids, writes.now_ms(self.now_fn()))
        return out | {"status": "SUBMITTED"} | self._submit(h, payload)
