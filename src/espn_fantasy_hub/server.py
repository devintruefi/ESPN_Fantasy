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
Live access to the user's ESPN fantasy football and fantasy basketball leagues:
every team, roster, matchup, stat, setting and transaction, plus strategy tools and
(with the user's approval) roster moves. Act as a sharp fantasy advisor.

Start with `my_leagues`: every league, the user's team, live matchup and lineup
alerts. Most tools take `league` (name, id, or "football"/"basketball" when the user
has only one of that sport) and `team` (name, abbreviation, owner, or omit for the
user's team).

How to give good advice:
- Ground every recommendation in this league's rules (`league_settings`: PPR vs
  standard, categories, roster slots, FAAB vs waiver priority, pickup limits,
  trade deadline, playoff format).
- Before lineup advice run `lineup_check` (it lists exact swaps and, when the bench
  can't help, free agents who play that day). Mention game times and anyone
  QUESTIONABLE/DAY_TO_DAY to re-check before lock.
- Football: `week.proj` is ESPN's projection; `opp_rank_vs_pos` is the opponent's
  defense rank vs that position (1 = toughest, 32 = easiest); BYE = no game.
- Basketball: lineups are DAILY (scoring periods are days; matchups run Mon-Sun).
  In category leagues ESPN's player rater (`rater`) is the best single value
  number. Use `matchup_projection` to see which categories are winnable or lost
  this week and `category_report` for season strengths/weaknesses and punts.
  Respect the league's pickup limit (shown as `pickups`).
- Season strategy: read `strategy_playbook` for the league's format, then use
  `category_strategy` (category leagues: build, core vs rotation spots, best-fit
  pickups), `player_values` (G-score rankings, punt-aware) and `schedule_planner`
  (matchup calendar, playoff-week schedules, light days for streaming).
- Strategy: `power_rankings` (all-play record = true strength; luck),
  `waiver_upgrades`, `free_agents`, `trade_analyzer` (both sides' value and each
  team's best-lineup change), `team_schedule` (remaining strength of schedule),
  `player_info` (news, outlook, game log). Combine several before recommending
  trades or big moves, and say what the data does and doesn't show.

ROSTER PHILOSOPHY (important): treat most of the roster as a core the user keeps.
Most managers rotate only 1-2 streaming spots per week. Judge players by season and
multi-week averages, not one night's schedule or a short cold streak, and in
category leagues think about the whole week. Never suggest dropping a player whose
season value is higher than the player being added (`add_player` previews show a
`value_check` and a CAUTION warning when that happens). Pick drops only from the
user's weakest rotation spots; if you don't know which spots they rotate, ask.
Don't drop injured stars who are expected back.

MOVES (`set_lineup`, `add_player`, `drop_player`, `propose_trade`) change the user's
real team. Always call them first WITHOUT confirm to get a preview, show the user
exactly what will change, and call again with confirm=true only after they say yes
(unless they have explicitly told you to act without asking). Trade proposals are
experimental: ask the user to double-check in the ESPN app afterwards.

Times are in the configured time zone. Data is cached up to ~45 seconds; call
`refresh` after the user makes a move in the ESPN app.
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
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
ConfirmArg = Annotated[
    bool,
    Field(description="false (default) = preview only, nothing is sent. true = actually make the move; only after the user approved the preview."),
]
NamesArg = Annotated[list[str] | None, Field(description="Player names.")]


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

    # ---- strategy ---------------------------------------------------------------
    @mcp.tool(annotations=READ_ONLY)
    def power_rankings(league: LeagueArg = None) -> str:
        """True team strength: all-play record (record if every team played every
        team every week), expected wins, luck, scoring average and recent form,
        combined into a power score. Great for 'who's actually good' and
        playoff-race talk."""
        return run(lambda h: h.power_rankings(league))

    @mcp.tool(annotations=READ_ONLY)
    def category_report(league: LeagueArg = None, team: TeamArg = None) -> str:
        """Category leagues: where a team ranks in every category (strengths,
        weaknesses, punt candidates) and which categories are swingable in the
        current matchup."""
        return run(lambda h: h.category_report(league, team))

    @mcp.tool(annotations=READ_ONLY)
    def strategy_playbook(league: LeagueArg = None) -> str:
        """The research-based playbook for this league's format, filled in with the
        league's actual rules (format, pickup limits, waivers, trade deadline, playoff
        weeks). Read it before giving season-level strategy advice."""
        return run(lambda h: h.strategy_playbook(league))

    @mcp.tool(annotations=READ_ONLY)
    def category_strategy(league: LeagueArg = None, team: TeamArg = None) -> str:
        """Category leagues: the team's odds in each category now and at full strength,
        which categories to build around or concede, every player's value to the team
        (core vs rotation spots), model strength of all teams, and the free agents that
        most raise THIS team's weekly win odds (with who they'd replace)."""
        return run(lambda h: h.category_strategy(league, team))

    @mcp.tool(annotations=READ_ONLY)
    def player_values(
        league: LeagueArg = None,
        punt: Annotated[list[str] | None, Field(description="Categories to ignore, e.g. ['TO', 'FT%'].")] = None,
        who: Annotated[str, Field(description="all, free (free agents/waivers) or rostered")] = "all",
        limit: Annotated[int, Field(ge=1, le=60)] = 30,
        recent: Annotated[bool, Field(description="Use the last 15 days instead of the season")] = False,
    ) -> str:
        """Category leagues: player rankings by G-score (per-game z-scores discounted for
        week-to-week noise), optionally ignoring punted categories, with the per-category
        breakdown. Use for trade and pickup evaluation."""
        return run(lambda h: h.player_values(league, punt, who, limit, recent))

    @mcp.tool(annotations=READ_ONLY)
    def schedule_planner(league: LeagueArg = None, team: TeamArg = None) -> str:
        """Season calendar for planning: each remaining matchup's dates and how many games
        your players have vs average, the NBA teams with the best playoff-week schedules,
        and the next two weeks' light days (open lineup slots) for streaming.
        Football: bye weeks of your players by week and playoff-week outlook."""
        return run(lambda h: h.schedule_planner(league, team))

    @mcp.tool(annotations=READ_ONLY)
    def matchup_projection(league: LeagueArg = None, team: TeamArg = None) -> str:
        """Basketball: projects where every category (or fantasy points) will end
        up this matchup from current totals + each team's remaining games, shows
        toss-up categories, and days when you have more players with games than
        starting slots. Football: ESPN's projected scores."""
        return run(lambda h: h.matchup_projection(league, team))

    @mcp.tool(annotations=READ_ONLY)
    def waiver_upgrades(league: LeagueArg = None, team: TeamArg = None) -> str:
        """Compares the best available free agents/waiver players to a team's
        weakest players, by season value and recent form, and lists clear upgrades."""
        return run(lambda h: h.waiver_upgrades(league, team))

    @mcp.tool(annotations=READ_ONLY)
    def trade_analyzer(
        give: NamesArg = None,
        receive: NamesArg = None,
        partner: Annotated[str | None, Field(description="Trade partner team (optional; inferred from the players you receive).")] = None,
        league: LeagueArg = None,
    ) -> str:
        """Evaluate a trade: each player's season and recent value, totals, how each
        team's best starting lineup changes, and (category leagues) your per-game
        category changes. Doesn't send anything."""
        return run(lambda h: h.trade_analyzer(league, give, receive, partner))

    # ---- moves --------------------------------------------------------------------
    @mcp.tool(annotations=WRITE)
    def set_lineup(
        start: NamesArg = None,
        bench: NamesArg = None,
        moves: Annotated[dict[str, str] | None, Field(description="Explicit placements: {player name: slot}, e.g. {'Josh Allen': 'QB', 'X': 'IR'}.")] = None,
        days_ahead: Annotated[int, Field(description="Basketball: 0 = today's lineup, 1 = tomorrow, ...", ge=0, le=7)] = 0,
        league: LeagueArg = None,
        confirm: ConfirmArg = False,
    ) -> str:
        """Change the user's lineup: start / bench players or put them in specific
        slots. Preview first (confirm=false), then confirm=true after approval."""
        return run(lambda h: h.set_lineup(league, start, bench, moves, days_ahead, confirm))

    @mcp.tool(annotations=WRITE)
    def add_player(
        add: Annotated[str, Field(description="Player to pick up.")],
        drop: Annotated[str | None, Field(description="Player to drop to make room (needed when the roster is full).")] = None,
        bid: Annotated[int | None, Field(description="FAAB bid in dollars, for waiver claims in FAAB leagues.", ge=0)] = None,
        league: LeagueArg = None,
        confirm: ConfirmArg = False,
    ) -> str:
        """Pick up a free agent (immediate) or put in a waiver claim (processed at
        waiver time), optionally dropping someone. Preview first, then confirm."""
        return run(lambda h: h.add_player(league, add, drop, bid, confirm))

    @mcp.tool(annotations=WRITE)
    def drop_player(
        player: Annotated[str, Field(description="Player to release.")],
        league: LeagueArg = None,
        confirm: ConfirmArg = False,
    ) -> str:
        """Release a player from the user's roster. Preview first, then confirm."""
        return run(lambda h: h.drop_player(league, player, confirm))

    @mcp.tool(annotations=WRITE)
    def propose_trade(
        partner: Annotated[str, Field(description="Team to send the offer to.")],
        give: NamesArg = None,
        receive: NamesArg = None,
        league: LeagueArg = None,
        confirm: ConfirmArg = False,
    ) -> str:
        """Send a trade offer (EXPERIMENTAL). Preview shows the offer plus a quick
        analysis; confirm=true sends it. Ask the user to verify in the ESPN app."""
        return run(lambda h: h.propose_trade(league, partner, give, receive, confirm))

    # ---- guided workflows (MCP prompts) -------------------------------------------
    @mcp.prompt(title="Daily check-in")
    def daily_checkin() -> str:
        """Check every league for lineup problems, injuries and matchup status."""
        return (
            "Do my daily fantasy check-in. Call my_leagues first. Then for EVERY league run lineup_check "
            "(basketball: also days_ahead=1 for tomorrow). For basketball leagues also run matchup_projection. "
            "Report, league by league: anything I must fix before games lock (with exact swaps or free-agent "
            "options and game times), questionable players to watch, how my matchup is going, and the 1-3 "
            "highest-impact moves. Use the move tools only as previews unless I approve."
        )

    @mcp.prompt(title="Waiver wire plan")
    def waiver_plan(league: str) -> str:
        """Who to add/drop (and how much FAAB to bid) in one league."""
        return (
            f"Build my waiver plan for league '{league}'. Use league_settings (waiver type, FAAB, pickup limits), "
            "team_roster for my team, waiver_upgrades, free_agents (by position for football; sorted by l7/l15 for "
            "basketball), category_report for category leagues, and player_info on the top candidates. Recommend "
            "specific adds with matching drops, FAAB bids where relevant, and why. Preview with add_player; don't "
            "submit until I approve."
        )

    @mcp.prompt(title="Trade ideas")
    def trade_ideas(league: str) -> str:
        """Find realistic trades that help both sides."""
        return (
            f"Find trade ideas for me in league '{league}'. Use standings, power_rankings, all_rosters, my "
            "team_roster, and category_report (category leagues) to find teams whose surplus matches my needs and "
            "vice versa. For the 2-3 best ideas run trade_analyzer and explain why the other manager might accept. "
            "Don't send anything; offer to preview propose_trade if I like one."
        )

    @mcp.prompt(title="Matchup game plan")
    def matchup_gameplan(league: str) -> str:
        """How to win this week's matchup."""
        return (
            f"Make a game plan to win my current matchup in league '{league}'. Use matchup_detail, "
            "matchup_projection, lineup_check (and days_ahead for basketball), team_roster for both teams and "
            "free_agents. Tell me which categories/positions are winnable or lost, specific streaming pickups "
            "(respecting the league's pickup limit), and lineup decisions day by day."
        )

    @mcp.prompt(title="Season outlook")
    def season_outlook(league: str) -> str:
        """Playoff chances and long-term strategy."""
        return (
            f"Give me a season outlook for league '{league}': standings, power_rankings (luck), team_schedule "
            "(strength of remaining schedule), playoff odds and trade deadline from league_settings. Tell me "
            "whether to buy or sell and the concrete moves that would most improve my playoff chances."
        )

    @mcp.tool(annotations=READ_ONLY)
    def refresh() -> str:
        """Clear cached ESPN data (use right after the user makes a move in the ESPN app)."""
        return run(lambda h: h.refresh())

    return mcp


def serve() -> None:
    logging.basicConfig(level=logging.WARNING)
    build_server().run()
