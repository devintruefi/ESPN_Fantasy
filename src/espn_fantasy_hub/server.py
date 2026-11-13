"""MCP server: exposes your ESPN fantasy leagues to Claude as read-only tools."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .config import ConfigError, load_settings
from .espn import EspnError
from .service import Hub, UserError

log = logging.getLogger("espn_fantasy_hub")

INSTRUCTIONS = """\
Read-only access to the user's ESPN fantasy football and fantasy basketball leagues.

Start with `my_leagues` to see every league, the user's team, the live matchup and
lineup alerts. Most tools take `league` (league name, id, or "football"/"basketball"
when that's unambiguous) and `team` (team name, abbreviation, owner name, or omit
for the user's own team).

Key facts:
- Football: periods are NFL weeks. `opp_rank_vs_pos` is ESPN's defense-vs-position
  rank for the player's opponent: 1 = toughest matchup, 32 = easiest.
- Basketball: lineups are set DAILY; "scoring periods" are days and a matchup is
  usually Mon-Sun. In category leagues ESPN's "player rater" (rater) is the best
  single value number; it already reflects the league's categories.
- `lineup_check` finds starters who are OUT/injured/on bye/without a game and the
  best bench replacement. Run it before lock and whenever the user asks about
  lineups. For basketball use days_ahead=1 to plan tomorrow.
- Injury status values: ACTIVE (omitted), QUESTIONABLE, DOUBTFUL, OUT,
  INJURY_RESERVE, DAY_TO_DAY, SUSPENSION.
- This server cannot make moves (lineups, waivers, trades). Tell the user exactly
  what to do in the ESPN app instead.
- Data is cached ~30-90 seconds; call `refresh` if the user just made a move.
"""

LeagueArg = Annotated[
    str | None,
    Field(description="League name, ESPN league id, or 'football'/'basketball' if you have only one of that sport. Omit if the user has one league."),
]
TeamArg = Annotated[
    str | None,
    Field(description="Team name, abbreviation, owner name, or team id. Omit for the user's own team."),
]

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)


def _dump(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)


def build_server(hub: Hub | None = None) -> FastMCP:
    state: dict[str, Hub | None] = {"hub": hub}

    def get_hub() -> Hub:
        if state["hub"] is None:
            try:
                settings = load_settings()
            except ConfigError as e:
                raise ToolError(f"Configuration problem: {e}") from None
            tz = None
            if settings.timezone:
                try:
                    from zoneinfo import ZoneInfo

                    tz = ZoneInfo(settings.timezone)
                except Exception:
                    raise ToolError(f"Unknown timezone {settings.timezone!r} in config.") from None
            state["hub"] = Hub(settings, tz=tz)
        return state["hub"]  # type: ignore[return-value]

    def run(fn: Callable[[Hub], Any]) -> str:
        try:
            return _dump(fn(get_hub()))
        except (UserError, EspnError) as e:
            raise ToolError(str(e)) from None
        except ToolError:
            raise
        except Exception as e:  # never crash the server on bad data
            log.exception("tool failed")
            raise ToolError(f"Unexpected error ({type(e).__name__}: {e}). ESPN's data may have changed shape.") from None

    mcp = FastMCP("ESPN Fantasy", instructions=INSTRUCTIONS)

    @mcp.tool(annotations=READ_ONLY)
    def my_leagues() -> str:
        """Dashboard of every ESPN fantasy league the user is in: their team, record,
        seed, live matchup score, and lineup alerts (injured/benched/bye starters).
        Call this first."""
        return run(lambda h: h.my_leagues())

    @mcp.tool(annotations=READ_ONLY)
    def league_settings(league: LeagueArg = None) -> str:
        """League rules: scoring format (points rules or categories), roster slots,
        playoff format, waiver/FAAB rules, trade deadline, draft, current week."""
        return run(lambda h: h.league_settings(league))

    @mcp.tool(annotations=READ_ONLY)
    def standings(league: LeagueArg = None) -> str:
        """Full standings: every team's record, seed, points for/against, streak,
        playoff odds, waiver priority, FAAB left, moves, owners; season category
        totals in category leagues."""
        return run(lambda h: h.standings(league))

    @mcp.tool(annotations=READ_ONLY)
    def team_roster(
        league: LeagueArg = None,
        team: TeamArg = None,
        detailed: Annotated[bool, Field(description="Include full stat lines (season and recent splits).")] = False,
    ) -> str:
        """One team's roster with lineup slot, injury status, season and recent
        production, projections, ownership, and this week's games/opponents.
        Works for any team in the league (scout opponents and trade partners)."""
        return run(lambda h: h.roster(league, team, detailed))

    @mcp.tool(annotations=READ_ONLY)
    def all_rosters(league: LeagueArg = None) -> str:
        """Every team's roster in one compact view (slot, player, injury, key value
        number). Use for trade targets, positional needs, and league-wide context."""
        return run(lambda h: h.all_rosters(league))

    @mcp.tool(annotations=READ_ONLY)
    def scoreboard(
        league: LeagueArg = None,
        period: Annotated[int | None, Field(description="Matchup period / week number. Omit for the current one.")] = None,
    ) -> str:
        """All matchups for a week/matchup period: live scores, projections, win
        probability, and category-by-category results in category leagues."""
        return run(lambda h: h.scoreboard(league, period))

    @mcp.tool(annotations=READ_ONLY)
    def matchup_detail(
        league: LeagueArg = None,
        team: TeamArg = None,
        period: Annotated[int | None, Field(description="Matchup period / week number. Omit for the current one.")] = None,
    ) -> str:
        """Box score for one matchup (default: the user's current one): both lineups
        with each player's points/stats so far, projections, game status, games left
        (basketball), and the category-by-category battle."""
        return run(lambda h: h.matchup(league, team, period))

    @mcp.tool(annotations=READ_ONLY)
    def team_schedule(league: LeagueArg = None, team: TeamArg = None) -> str:
        """A team's full season schedule: results, scores, upcoming opponents and
        their records, and strength of remaining schedule."""
        return run(lambda h: h.schedule(league, team))

    @mcp.tool(annotations=READ_ONLY)
    def lineup_check(
        league: LeagueArg = None,
        team: TeamArg = None,
        days_ahead: Annotated[int, Field(description="Basketball only: 0 = today, 1 = tomorrow, etc.", ge=0, le=7)] = 0,
    ) -> str:
        """Audit a lineup before lock: starters who are OUT/IR/DOUBTFUL/QUESTIONABLE,
        on bye, or without a game; empty slots; healthy bench players projected to
        outscore a starter; players stuck in IR. Gives exact swaps to make."""
        return run(lambda h: h.lineup_check(league, team, days_ahead))

    @mcp.tool(annotations=READ_ONLY)
    def free_agents(
        league: LeagueArg = None,
        position: Annotated[str | None, Field(description="Football: QB, RB, WR, TE, FLEX, D/ST, K. Basketball: PG, SG, SF, PF, C, G, F, UT.")] = None,
        sort_by: Annotated[str | None, Field(description="Football: projected (default), season_avg, season_total, owned, trending. Basketball: l15 (default), l7, l30, season, projected (points leagues), owned, trending.")] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> str:
        """Best available players (free agents and players on waivers) in this
        league, with stats, projections, injury status, schedule and trend."""
        return run(lambda h: h.free_agents(league, position, sort_by, limit))

    @mcp.tool(annotations=READ_ONLY)
    def player_info(
        name: Annotated[str, Field(description="Player name (partial names work).")],
        league: LeagueArg = None,
    ) -> str:
        """Everything on one player in the context of a league: who rosters him,
        injury status, ESPN outlook, season and recent stats, game log, upcoming
        schedule/matchups, and latest news."""
        return run(lambda h: h.player(league, name))

    @mcp.tool(annotations=READ_ONLY)
    def recent_transactions(
        league: LeagueArg = None,
        periods_back: Annotated[int | None, Field(description="Football: weeks back (default 1). Basketball: days back (default 6).", ge=0, le=14)] = None,
    ) -> str:
        """Recent league activity: free-agent pickups, waiver claims (with FAAB
        bids), trades and trade proposals visible to the user."""
        return run(lambda h: h.transactions(league, periods_back))

    @mcp.tool(annotations=READ_ONLY)
    def draft_results(league: LeagueArg = None, team: TeamArg = None) -> str:
        """The league's draft: every pick (or one team's picks), with auction
        prices and keepers."""
        return run(lambda h: h.draft(league, team))

    @mcp.tool(annotations=READ_ONLY)
    def refresh() -> str:
        """Clear cached ESPN data (use right after the user makes a move in the ESPN app)."""
        return run(lambda h: h.refresh())

    return mcp


def serve() -> None:
    logging.basicConfig(level=logging.WARNING)
    build_server().run()
