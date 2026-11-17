"""Thin, cached, error-normalized access to ESPN's fantasy API (read-only).

Uses espn-api's request layer (endpoints, cookie handling, endpoint fallback)
and returns ESPN's raw JSON. All parsing lives in ``views.py``.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import requests
from espn_api.requests.espn_requests import (
    ESPNAccessDenied,
    ESPNInvalidLeague,
    ESPNUnknownError,
    EspnFantasyRequests,
)

from . import http
from .projections import parse_public_players
from .sports import Sport


class EspnError(Exception):
    """Error with a message that is safe and useful to show the user."""


class LeagueAccessError(EspnError):
    pass


class LeagueNotFound(EspnError):
    pass


class EspnUnavailable(EspnError):
    pass


# TTLs in seconds
# Hosted deployments can run several instances, each with its own cache, so
# keep league data fresh enough that a move made elsewhere shows up quickly.
TTL_LIVE = 20
TTL_LEAGUE = 45
TTL_SLOW = 3600
TTL_SEASON = 6 * 3600


class _TTLCache:
    def __init__(self) -> None:
        self._data: dict[Any, tuple[float, Any]] = {}
        self._locks: dict[Any, threading.Lock] = {}
        self._guard = threading.Lock()

    def get_or_load(self, key, ttl: float, loader: Callable[[], Any]):
        now = time.monotonic()
        hit = self._data.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:  # one fetch per key at a time
            hit = self._data.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
            value = loader()
            self._data[key] = (time.monotonic(), value)
            return value

    def clear(self) -> None:
        self._data.clear()


# Season-wide data (pro schedules, player index) is shared by every league of
# the same sport and season, so cache it at module level.
_SEASON_CACHE = _TTLCache()


# Weekly projected stat lines from ESPN's public player feed, per (game, season, player).
# Shared by every league of the sport: the NFL's projections don't depend on the league.
PUBLIC_PLAYERS_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/{game}/seasons/{season}/players"
TTL_PROJ = 3600
_PROJ_CACHE: dict[tuple, tuple[float, frozenset, dict]] = {}


def _call(fn: Callable[[], Any], what: str, league_id: int | None = None):
    try:
        return fn()
    except ESPNAccessDenied:
        raise LeagueAccessError(
            f"ESPN refused access to league {league_id}. Private leagues need your "
            "espn_s2 and SWID cookies. If they're already set they have probably "
            "expired: log in to espn.com again and copy fresh values."
        ) from None
    except ESPNInvalidLeague:
        raise LeagueNotFound(
            f"ESPN says league {league_id} doesn't exist for this season."
        ) from None
    except ESPNUnknownError as e:
        raise EspnUnavailable(f"ESPN returned an error while loading {what} ({e}).") from None
    except requests.exceptions.Timeout:
        raise EspnUnavailable(f"ESPN timed out while loading {what}. Try again shortly.") from None
    except requests.exceptions.RequestException as e:
        raise EspnUnavailable(f"Couldn't reach ESPN while loading {what} ({type(e).__name__}).") from None
    except (ValueError, json.JSONDecodeError):
        raise EspnUnavailable(f"ESPN sent an unreadable response for {what}.") from None


def _filter_header(obj: dict) -> dict:
    return {"x-fantasy-filter": json.dumps(obj)}


class LeagueClient:
    """Raw ESPN data for one league + season."""

    def __init__(self, sport: Sport, league_id: int, season: int, cookies: dict | None) -> None:
        http.install()
        self.sport = sport
        self.league_id = league_id
        self.season = season
        self.req = EspnFantasyRequests(
            sport=sport.espn_sport, year=season, league_id=league_id, cookies=cookies
        )
        self._cache = _TTLCache()

    # ---- league-scoped ----------------------------------------------------
    def _league_get(self, key, ttl, what, params=None, headers=None, extend=""):
        return self._cache.get_or_load(
            key,
            ttl,
            lambda: _call(
                lambda: self.req.league_get(params=params, headers=headers, extend=extend),
                what,
                self.league_id,
            ),
        )

    def core(self) -> dict:
        """Settings, teams, records, rosters (current period), schedule, members."""
        return self._league_get(
            "core",
            TTL_LEAGUE,
            "league data",
            params={"view": ["mTeam", "mRoster", "mMatchup", "mSettings", "mStandings"]},
        )

    def roster_for_period(self, scoring_period: int) -> dict:
        return self._league_get(
            ("roster", scoring_period),
            TTL_LIVE,
            "rosters",
            params={"view": "mRoster", "scoringPeriodId": scoring_period},
        )

    def scoreboard(self, matchup_period: int, scoring_period: int, final: bool = False) -> dict:
        """Live scores + per-player box scores for one matchup period (``final``: a finished
        period, which never changes, so it is cached much longer)."""
        return self._league_get(
            ("scoreboard", matchup_period, scoring_period),
            TTL_SLOW if final else TTL_LIVE,
            "matchups",
            params={"view": ["mMatchupScore", "mScoreboard"], "scoringPeriodId": scoring_period},
            headers=_filter_header(
                {"schedule": {"filterMatchupPeriodIds": {"value": [matchup_period]}}}
            ),
        )

    def player_pool(
        self,
        scoring_period: int,
        statuses: tuple[str, ...] = ("FREEAGENT", "WAIVERS"),
        slot_ids: tuple[int, ...] = (),
        limit: int = 100,
    ) -> dict:
        flt = {
            "players": {
                "filterStatus": {"value": list(statuses)},
                "filterSlotIds": {"value": list(slot_ids)},
                "limit": limit,
                "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
                "sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": "STANDARD"},
            }
        }
        return self._league_get(
            ("pool", scoring_period, statuses, slot_ids, limit),
            TTL_LEAGUE,
            "available players",
            params={"view": "kona_player_info", "scoringPeriodId": scoring_period},
            headers=_filter_header(flt),
        )

    def player_cards(self, player_ids: list[int], final_scoring_period: int, extra_splits: tuple[str, ...] = ()) -> dict:
        ids = sorted(set(int(i) for i in player_ids))
        return self._cache.get_or_load(
            ("cards", tuple(ids), extra_splits),
            TTL_LEAGUE,
            lambda: _call(
                lambda: self.req.get_player_card(ids, final_scoring_period, list(extra_splits) or None),
                "player details",
                self.league_id,
            ),
        )

    def transactions(self, scoring_period: int, final: bool = False) -> dict:
        return self._league_get(
            ("tx", scoring_period),
            TTL_SLOW if final else TTL_LEAGUE,
            "transactions",
            params={"view": "mTransactions2", "scoringPeriodId": scoring_period},
        )

    def pending_transactions(self) -> dict:
        """ESPN's queue of unprocessed waiver claims (and pending trades)."""
        return self._league_get(
            "pending_tx", TTL_LIVE, "pending waiver claims", params={"view": "mPendingTransactions"}
        )

    def positional_ratings(self, scoring_period: int) -> dict:
        return self._league_get(
            ("posratings", scoring_period),
            TTL_SLOW,
            "defense-vs-position ratings",
            params={"view": "mPositionalRatings", "scoringPeriodId": scoring_period},
        )

    def draft(self) -> dict:
        return self._league_get("draft", TTL_SLOW, "draft", params={"view": "mDraftDetail"})

    # ---- season-scoped (shared) ------------------------------------------
    def pro_schedule(self) -> dict:
        return _SEASON_CACHE.get_or_load(
            ("proschedule", self.sport.game, self.season),
            TTL_SEASON,
            lambda: _call(self.req.get_pro_schedule, "pro schedules", self.league_id),
        )

    def player_index(self) -> list[dict]:
        """Every active pro player: id, name, team, position."""
        return _SEASON_CACHE.get_or_load(
            ("players", self.sport.game, self.season),
            TTL_SEASON,
            lambda: _call(self.req.get_pro_players, "player list", self.league_id),
        )

    def news(self, player_id: int) -> dict:
        return _SEASON_CACHE.get_or_load(
            ("news", self.sport.game, player_id),
            900,
            lambda: _call(lambda: self.req.get_player_news(player_id), "player news", self.league_id),
        )

    def public_projections(self, player_ids, weeks) -> dict[int, dict[int, dict]]:
        """{player id: {week: projected raw stats}} from ESPN's public player feed.

        Fetches only players not already cached for these weeks, 50 per request, in
        parallel. A failed batch is skipped (callers fall back to box-score history)."""
        weeks = tuple(sorted({int(w) for w in weeks if int(w) >= 1}))
        if not weeks:
            return {}
        want = {int(i) for i in player_ids if i}
        now = time.monotonic()
        out: dict[int, dict[int, dict]] = {}
        need = []
        for pid in want:
            hit = _PROJ_CACHE.get((self.sport.game, self.season, pid))
            if hit and now - hit[0] < TTL_PROJ and set(weeks) <= hit[1]:
                out[pid] = {w: hit[2][w] for w in weeks if w in hit[2]}
            else:
                need.append(pid)
        if not need:
            return out
        url = PUBLIC_PLAYERS_URL.format(game=self.sport.game, season=self.season)
        extra = [f"11{self.season}{w}" for w in weeks]

        def fetch(batch):
            flt = {"filterIds": {"value": batch},
                   "filterStatsForTopScoringPeriodIds": {"value": 1, "additionalValue": extra}}
            try:
                r = http.session().get(url, params={"view": "kona_playercard"}, timeout=20, headers={
                    "X-Fantasy-Filter": json.dumps(flt), "X-Fantasy-Platform": "kona-PROD",
                    "X-Fantasy-Source": "kona"})
                if r.status_code != 200:
                    return {}
                return parse_public_players(r.json(), self.season)
            except (requests.exceptions.RequestException, ValueError):
                return {}

        batches = [need[i:i + 50] for i in range(0, len(need), 50)]
        with ThreadPoolExecutor(max_workers=min(6, len(batches))) as pool:
            results = list(pool.map(fetch, batches))
        stamp = time.monotonic()
        for res in results:
            for pid, by_week in res.items():
                _PROJ_CACHE[(self.sport.game, self.season, pid)] = (stamp, frozenset(weeks), by_week)
                out[pid] = {w: by_week[w] for w in weeks if w in by_week}
        return out

    def clear(self) -> None:
        self._cache.clear()
