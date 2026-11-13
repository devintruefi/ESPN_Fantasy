"""Find every ESPN fantasy league on your account, using your login cookies.

ESPN's "fan" profile API (the same one the ESPN app uses) lists the fantasy
teams you own. This is best-effort: if ESPN changes it, list leagues
explicitly in the config instead (see README).
"""

from __future__ import annotations

from . import http
from .config import LeagueConfig, Settings
from .espn import EspnUnavailable, LeagueAccessError
from .sports import BASKETBALL, FOOTBALL

FAN_API = "https://fan.api.espn.com/apis/v2/fans/{swid}"
GAME_CODES = {"FFL": FOOTBALL, "FBA": BASKETBALL}


def parse_fan_profile(data: dict) -> list[LeagueConfig]:
    found: dict[tuple[str, int], LeagueConfig] = {}
    for pref in data.get("preferences") or []:
        entry = ((pref or {}).get("metaData") or {}).get("entry") or {}
        sport = GAME_CODES.get(str(entry.get("abbrev") or "").upper())
        if not sport:
            continue
        season = entry.get("seasonId")
        team_id = entry.get("entryId")
        for group in entry.get("groups") or []:
            try:
                league_id = int(group.get("groupId"))
            except (TypeError, ValueError):
                continue
            key = (sport, league_id)
            prev = found.get(key)
            if prev is None or (season or 0) > (prev.season or 0):
                found[key] = LeagueConfig(
                    sport=sport,
                    league_id=league_id,
                    season=int(season) if season else None,
                    name=group.get("groupName") or None,
                    team_id=int(team_id) if team_id else None,
                    discovered=True,
                )
    # Only keep each sport's most recent season (drop leagues from old years).
    latest: dict[str, int] = {}
    for lg in found.values():
        latest[lg.sport] = max(latest.get(lg.sport, 0), lg.season or 0)
    return sorted(
        (lg for lg in found.values() if (lg.season or 0) >= latest.get(lg.sport, 0)),
        key=lambda lg: (lg.sport, lg.league_id),
    )


def discover(settings: Settings) -> list[LeagueConfig]:
    if not settings.has_credentials:
        return []
    try:
        resp = http.session().get(
            FAN_API.format(swid=settings.swid),
            params={
                "displayHiddenPrefs": "true",
                "context": "fantasy",
                "useCookieAuth": "true",
                "source": "fantasyapp-ios",
                "featureFlags": "challengeEntries",
            },
            cookies=settings.cookies(),
        )
    except Exception as e:  # network trouble: discovery is optional
        raise EspnUnavailable(f"Couldn't reach ESPN to find your leagues ({type(e).__name__}).") from None
    if resp.status_code in (401, 403):
        raise LeagueAccessError("ESPN rejected your cookies while looking up your leagues; they may have expired.")
    if resp.status_code != 200:
        raise EspnUnavailable(f"ESPN returned HTTP {resp.status_code} while looking up your leagues.")
    try:
        data = resp.json()
    except ValueError:
        raise EspnUnavailable("ESPN sent an unreadable response while looking up your leagues.") from None
    if data.get("anon"):
        raise LeagueAccessError(
            "ESPN didn't recognize your login cookies (it treated you as a guest). "
            "Copy fresh espn_s2 and SWID values from espn.com."
        )
    return parse_fan_profile(data)
