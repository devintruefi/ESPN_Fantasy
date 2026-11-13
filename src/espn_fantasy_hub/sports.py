"""Per-sport lookup tables (positions, lineup slots, pro teams, stat names)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from espn_api.basketball import constant as bc
from espn_api.football import constant as fc

FOOTBALL = "football"
BASKETBALL = "basketball"

SPORT_ALIASES = {
    "football": FOOTBALL,
    "ffl": FOOTBALL,
    "nfl": FOOTBALL,
    "fantasy football": FOOTBALL,
    "basketball": BASKETBALL,
    "fba": BASKETBALL,
    "nba": BASKETBALL,
    "fantasy basketball": BASKETBALL,
}

# ESPN injury statuses that mean "will not play".
OUT_STATUSES = {
    "OUT",
    "INJURY_RESERVE",
    "SUSPENSION",
    "SIXTY_DAY_DL",
    "FIFTEEN_DAY_DL",
    "TEN_DAY_DL",
    "SEVEN_DAY_DL",
    "BEREAVEMENT",
    "PATERNITY",
    "RESERVE_RETIRED",
}
# Statuses that mean "might not play — check before lock".
RISK_STATUSES = {"DOUBTFUL", "QUESTIONABLE", "DAY_TO_DAY", "PROBABLE"}


def _int_keys(mapping: dict) -> dict[int, str]:
    out: dict[int, str] = {}
    for k, v in mapping.items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            continue
    return out


@dataclass(frozen=True)
class Sport:
    key: str
    espn_sport: str  # espn-api sport key
    game: str  # ESPN game code
    slot_names: dict[int, str]
    pro_teams: dict[int, str]
    stat_names: dict[int, str]
    bench_slot: int
    ir_slot: int
    unused_slots: frozenset[int]
    default_positions: dict[int, str] = field(default_factory=dict)

    # ---- positions / slots -------------------------------------------------
    def slot(self, slot_id) -> str:
        if slot_id is None:
            return ""
        return self.slot_names.get(int(slot_id), str(slot_id))

    def is_starting_slot(self, slot_id) -> bool:
        return slot_id is not None and int(slot_id) not in (
            {self.bench_slot, self.ir_slot} | set(self.unused_slots)
        )

    def eligible_positions(self, eligible_slot_ids, allowed=None) -> list[str]:
        """Eligible starting positions (limited to slots the league uses)."""
        skip = {self.bench_slot, self.ir_slot} | set(self.unused_slots)
        out = []
        for sid in eligible_slot_ids or []:
            if sid in skip or (allowed is not None and sid not in allowed):
                continue
            name = self.slot(sid)
            if name and name not in out:
                out.append(name)
        return out

    def position(self, player: dict) -> str:
        pid = player.get("defaultPositionId")
        if pid in self.default_positions:
            return self.default_positions[pid]
        # Fall back to the first single-position eligible slot.
        for sid in player.get("eligibleSlots", []) or []:
            name = self.slot(sid)
            if name and "/" not in name and sid not in (self.bench_slot, self.ir_slot):
                return name
        return ""

    def pro_team(self, pro_team_id) -> str:
        if pro_team_id in (None, 0):
            return "FA"
        return self.pro_teams.get(int(pro_team_id), str(pro_team_id))

    def stat(self, stat_id) -> str:
        try:
            return self.stat_names.get(int(stat_id), str(stat_id))
        except (TypeError, ValueError):
            return str(stat_id)

    # ---- seasons -----------------------------------------------------------
    def guess_season(self, today: dt.date | None = None) -> int:
        """ESPN season id for 'now'.

        Football seasons are named for the year they start (2026 = fall 2026).
        Basketball seasons are named for the year they end (2027 = 2026-27).
        """
        today = today or dt.date.today()
        if self.key == FOOTBALL:
            return today.year if today.month >= 3 else today.year - 1
        return today.year + 1 if today.month >= 8 else today.year


# espn-api's football stat map gives some per-game stats the same name as the
# season total (e.g. 61 is receiving yards PER GAME, 42 is the total), so a
# naive mapping overwrites totals with averages. Fix the names and skip exact
# duplicates.
FOOTBALL_STAT_FIXES = {
    22: "passingYardsPerGame",
    40: "rushingYardsPerGame",
    61: "receivingYardsPerGame",
}
# Duplicates of other ids, or team-level noise, hidden from stat lines.
FOOTBALL_SKIP_STATS = {41, 187, 206, 155, 156, 157, 158}

FOOTBALL_SPORT = Sport(
    key=FOOTBALL,
    espn_sport="nfl",
    game="ffl",
    slot_names={k: v for k, v in fc.POSITION_MAP.items() if isinstance(k, int)}
    | {23: "FLEX"},
    pro_teams={k: v for k, v in fc.PRO_TEAM_MAP.items() if isinstance(k, int)},
    stat_names=_int_keys(fc.PLAYER_STATS_MAP) | FOOTBALL_STAT_FIXES,
    bench_slot=20,
    ir_slot=21,
    unused_slots=frozenset({22, 24, 25}),
    default_positions={
        1: "QB",
        2: "RB",
        3: "WR",
        4: "TE",
        5: "K",
        7: "P",
        9: "DT",
        10: "DE",
        11: "LB",
        12: "CB",
        13: "S",
        14: "HC",
        16: "D/ST",
    },
)

BASKETBALL_SPORT = Sport(
    key=BASKETBALL,
    espn_sport="nba",
    game="fba",
    slot_names={k: v for k, v in bc.POSITION_MAP.items() if isinstance(k, int)},
    pro_teams={k: v for k, v in bc.PRO_TEAM_MAP.items() if isinstance(k, int)},
    stat_names=_int_keys(bc.STATS_MAP),
    bench_slot=12,
    ir_slot=13,
    unused_slots=frozenset({14, 15}),
    default_positions={1: "PG", 2: "SG", 3: "SF", 4: "PF", 5: "C"},
)

SPORTS = {FOOTBALL: FOOTBALL_SPORT, BASKETBALL: BASKETBALL_SPORT}

# Basketball box-score stats worth showing in a points league (and as context
# in a category league), in display order.
BASKETBALL_CORE_STATS = [
    "PTS", "REB", "AST", "STL", "BLK", "3PM", "TO", "FG%", "FT%", "FGM", "FGA",
    "FTM", "FTA", "MIN", "GP",
]


def sport_from_name(name: str) -> Sport:
    key = SPORT_ALIASES.get((name or "").strip().lower())
    if key is None:
        raise ValueError(
            f"Unknown sport {name!r}. Use 'football' or 'basketball'."
        )
    return SPORTS[key]
