"""Configuration: ESPN cookies and which leagues to load.

Sources, in order (later ones override earlier ones):
  1. TOML file at $ESPN_FANTASY_CONFIG, or ~/.config/espn-fantasy-hub/config.toml
  2. Environment variables ESPN_S2, ESPN_SWID, ESPN_LEAGUES

ESPN_LEAGUES format:  "football:123456,football:7891011,basketball:555666"
Optional extras per league:  "football:123456:2026:3"  (season, your team id)
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .sports import sport_from_name

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "espn-fantasy-hub" / "config.toml"


class ConfigError(ValueError):
    pass


@dataclass
class LeagueConfig:
    sport: str  # "football" | "basketball"
    league_id: int
    season: int | None = None
    name: str | None = None  # your own nickname for the league
    team_id: int | None = None  # your team, if auto-detection can't find it
    discovered: bool = False


@dataclass
class Settings:
    espn_s2: str | None = None
    swid: str | None = None
    leagues: list[LeagueConfig] = field(default_factory=list)
    auto_discover: bool = True
    allow_writes: bool = False
    timezone: str | None = None
    config_path: Path | None = None

    @property
    def has_credentials(self) -> bool:
        return bool(self.espn_s2 and self.swid)

    def cookies(self) -> dict | None:
        if not self.has_credentials:
            return None
        return {"espn_s2": self.espn_s2, "SWID": self.swid}


def normalize_swid(swid: str | None) -> str | None:
    if not swid:
        return None
    swid = swid.strip().strip('"').strip("'")
    if not swid:
        return None
    core = swid.strip("{}").upper()
    if not re.fullmatch(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}", core):
        raise ConfigError(
            "ESPN SWID looks wrong. It should look like "
            "{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX} (copy the SWID cookie from espn.com)."
        )
    return "{" + core + "}"


def normalize_s2(s2: str | None) -> str | None:
    if not s2:
        return None
    s2 = s2.strip().strip('"').strip("'")
    return s2 or None


def _parse_league_entry(raw: dict, where: str) -> LeagueConfig:
    try:
        sport = sport_from_name(str(raw.get("sport", ""))).key
    except ValueError as e:
        raise ConfigError(f"{where}: {e}") from None
    league_id = raw.get("id", raw.get("league_id"))
    try:
        league_id = int(league_id)
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: league id must be a number, got {league_id!r}") from None
    season = raw.get("season")
    team_id = raw.get("team_id")
    return LeagueConfig(
        sport=sport,
        league_id=league_id,
        season=int(season) if season not in (None, "") else None,
        name=(str(raw["name"]).strip() or None) if raw.get("name") else None,
        team_id=int(team_id) if team_id not in (None, "") else None,
    )


def parse_leagues_env(value: str) -> list[LeagueConfig]:
    leagues = []
    for i, chunk in enumerate(p.strip() for p in value.split(",")):
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split(":")]
        if len(parts) < 2:
            raise ConfigError(
                f"ESPN_LEAGUES entry {chunk!r} should look like 'football:123456'"
            )
        raw = {"sport": parts[0], "id": parts[1]}
        if len(parts) > 2 and parts[2]:
            raw["season"] = parts[2]
        if len(parts) > 3 and parts[3]:
            raw["team_id"] = parts[3]
        leagues.append(_parse_league_entry(raw, f"ESPN_LEAGUES entry {i + 1}"))
    return leagues


def load_settings(environ: dict | None = None, config_path: str | Path | None = None) -> Settings:
    env = os.environ if environ is None else environ
    settings = Settings()

    path = config_path or env.get("ESPN_FANTASY_CONFIG")
    path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"Could not parse {path}: {e}") from None
        settings.config_path = path
        settings.espn_s2 = data.get("espn_s2") or None
        settings.swid = data.get("swid") or None
        settings.auto_discover = bool(data.get("auto_discover", True))
        settings.allow_writes = bool(data.get("allow_writes", False))
        settings.timezone = data.get("timezone") or None
        for i, raw in enumerate(data.get("league", []) or []):
            settings.leagues.append(_parse_league_entry(raw, f"{path} [[league]] #{i + 1}"))
    elif config_path or env.get("ESPN_FANTASY_CONFIG"):
        raise ConfigError(f"Config file not found: {path}")

    if env.get("ESPN_S2"):
        settings.espn_s2 = env["ESPN_S2"]
    if env.get("ESPN_SWID"):
        settings.swid = env["ESPN_SWID"]
    if env.get("ESPN_LEAGUES"):
        env_leagues = parse_leagues_env(env["ESPN_LEAGUES"])
        known = {(lg.sport, lg.league_id) for lg in settings.leagues}
        settings.leagues.extend(lg for lg in env_leagues if (lg.sport, lg.league_id) not in known)
    if env.get("ESPN_AUTO_DISCOVER"):
        settings.auto_discover = env["ESPN_AUTO_DISCOVER"].strip().lower() not in ("0", "false", "no")
    if env.get("ESPN_ALLOW_WRITES"):
        settings.allow_writes = env["ESPN_ALLOW_WRITES"].strip().lower() in ("1", "true", "yes", "on")
    if env.get("ESPN_TIMEZONE"):
        settings.timezone = env["ESPN_TIMEZONE"]

    settings.espn_s2 = normalize_s2(settings.espn_s2)
    settings.swid = normalize_swid(settings.swid)
    if bool(settings.espn_s2) != bool(settings.swid):
        raise ConfigError(
            "Set both ESPN_S2 and ESPN_SWID (or neither, for public leagues only)."
        )
    return settings
