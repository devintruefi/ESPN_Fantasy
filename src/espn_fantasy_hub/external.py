"""Outside data sources (read-only, public, no keys).

- Sleeper: what players fantasy managers are adding/dropping right now (trending).
- nflverse: weekly NFL usage (snaps, targets, target share, air yards, carries).
- dynastyprocess: player id map linking ESPN, Sleeper, nflverse (gsis) and PFR ids.
- ESPN's public scoreboard: game lines (spread, total) and venue (indoor or not).
- Open-Meteo: hourly forecast (wind, rain/snow chance, temperature) at stadiums.

Every fetch has a timeout, is cached, and returns None on failure, so a source being down
never breaks a tool: the tool says which source was unavailable.
"""

from __future__ import annotations

import csv
import io
import logging

import requests

from . import http
from .espn import _TTLCache

log = logging.getLogger(__name__)
_CACHE = _TTLCache()

SLEEPER_TREND = "https://api.sleeper.app/v1/players/{sport}/trending/{kind}"
SLEEPER_PLAYERS = "https://api.sleeper.app/v1/players/{sport}"
ID_MAP_URL = "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv"
NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download/{tag}/{name}_{season}.csv"
ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


def _fetch(key, ttl: float, url: str, params=None, as_text: bool = False, timeout: float = 20):
    def load():
        try:
            r = http.session().get(url, params=params, timeout=timeout)
            if r.status_code != 200:
                log.warning("outside data %s: HTTP %s", url, r.status_code)
                return None
            return r.text if as_text else r.json()
        except (requests.exceptions.RequestException, ValueError) as e:
            log.warning("outside data %s failed: %s", url, e)
            return None

    value = _CACHE.get_or_load(key, ttl, load)
    if value is None:   # don't keep failures: try again next call
        _CACHE._data.pop(key, None)
    return value


def sleeper_trending(sport: str, kind: str, hours: int = 24, limit: int = 40) -> list[dict] | None:
    """[{player_id, count}] for sport 'nfl' or 'nba', kind 'add' or 'drop'."""
    return _fetch(("sleeper", sport, kind, hours, limit), 1800, SLEEPER_TREND.format(sport=sport, kind=kind),
                  {"lookback_hours": hours, "limit": limit})


def sleeper_players(sport: str) -> dict | None:
    """Sleeper's player directory (large: fetched at most every 12 hours)."""
    return _fetch(("sleeper_players", sport), 12 * 3600, SLEEPER_PLAYERS.format(sport=sport), timeout=40)


def _csv(text: str | None) -> list[dict] | None:
    if not text:
        return None
    return list(csv.DictReader(io.StringIO(text)))


def nfl_id_map() -> list[dict] | None:
    return _csv(_fetch("idmap", 24 * 3600, ID_MAP_URL, as_text=True, timeout=40))


def nflverse_weekly(season: int) -> list[dict] | None:
    url = NFLVERSE.format(tag="stats_player", name="stats_player_week", season=season)
    return _csv(_fetch(("nflverse_week", season), 6 * 3600, url, as_text=True, timeout=40))


def nflverse_snaps(season: int) -> list[dict] | None:
    url = NFLVERSE.format(tag="snap_counts", name="snap_counts", season=season)
    return _csv(_fetch(("nflverse_snaps", season), 6 * 3600, url, as_text=True, timeout=40))


def nfl_scoreboard(season: int, week: int) -> dict | None:
    return _fetch(("nfl_sb", season, week), 1800, ESPN_SITE.format(path="football/nfl"),
                  {"seasontype": 2, "week": week, "dates": season})


def nba_scoreboard(day) -> dict | None:
    return _fetch(("nba_sb", str(day)), 1800, ESPN_SITE.format(path="basketball/nba"), {"dates": f"{day:%Y%m%d}"})


def weather(points: list[tuple[float, float]]) -> list[dict] | None:
    """Hourly wind (mph), precipitation chance (%) and temperature (F) for the next 10 days, UTC."""
    if not points:
        return []
    lat = ",".join(f"{p[0]:.3f}" for p in points)
    lon = ",".join(f"{p[1]:.3f}" for p in points)
    data = _fetch(("meteo", lat, lon), 3600, OPEN_METEO, {
        "latitude": lat, "longitude": lon, "hourly": "wind_speed_10m,precipitation_probability,temperature_2m",
        "wind_speed_unit": "mph", "temperature_unit": "fahrenheit", "timezone": "UTC", "forecast_days": 10})
    if data is None:
        return None
    return data if isinstance(data, list) else [data]
