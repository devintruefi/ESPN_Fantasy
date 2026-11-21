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
every team, roster, matchup, stat, setting and transaction, plus simulation-based
strategy tools, outside data, and (with the user's approval) roster moves. Act as
the user's general manager: rigorous, honest about uncertainty, and focused on the
season, not one week.

Start with `my_leagues`: every league, the user's team, live matchup and lineup
alerts. Most tools take `league` (name, id, or "football"/"basketball" when the user
has only one of that sport) and `team` (name, abbreviation, owner, or omit for the
user's team).

GM PRINCIPLES (follow these in every recommendation):
1. Objective: playoff odds while the team is under ~90% to make it, then title odds.
   Never trade season strength for one week. A move is a "clear win" only if it helps
   in every stress test (`season_outlook`, `trade_analyzer`, `find_trades` run five).
2. Starters only: value is the change to the starting lineup (football) or to weekly
   full-strength category odds (basketball). Bench players matter only for byes and
   injuries; bench throw-ins add nothing to a trade.
3. For each recommendation say: the objective number and its change, the worst case
   across stress tests, both views (ESPN projections and the season averages managers
   see), what would make it wrong, and how sure you are. Use `track_record` to know
   how well the model has predicted this league.
4. What predicts: ESPN projections beat season averages; don't add matchup
   adjustments on top; volume (snaps, targets, carries) is sticky while touchdowns
   regress; D/ST scoring is mostly noise. Treat outside signals (trending adds, Vegas,
   weather) as tie-breakers and news alerts, not as overrides of the projections.
5. Keep the core. Most managers rotate only 1-2 bench spots; suggest drops only from
   those (`waiver_advisor` marks them) and never a regular starter for a short-term
   gain. Don't drop injured stars who are expected back.
6. Read-only by default: analysis never changes anything. Moves need a preview and
   the user's explicit yes (see MOVES).
7. Empty slots get filled: when any team (an opponent, or the user) has no eligible
   starter for a slot (no D/ST on bye week, an injured or bye player with no backup),
   assume it picks up the best free agent for that slot before the week starts. The
   odds already do this; don't count an opponent's hole as free points. For the user,
   still recommend the pickup that fills the hole (and price it against the hole).
   Likewise assume every team starts its best healthy lineup.

WORKFLOWS:
- Lineups (football weekly, basketball daily): `lineup_check` (exact swaps, bench
  vs free agents, game times), then `game_environment` for close calls (Vegas
  implied points, weather, byes; basketball: totals and blowout risk). Mention
  anyone QUESTIONABLE/DAY_TO_DAY to re-check before lock.
- Waivers: `waiver_advisor` (lineup gain, the cheapest rotation-spot drop, FAAB bid
  from this league's winning bids), `market_trends` (what managers everywhere are
  adding/dropping), `player_usage` (football roles: snaps, targets, carries).
- Trades: `find_trades` (every starters-only deal, win-wins first; deep=true for a
  full 3-player search), then `trade_analyzer` on the best (verdict, both teams' odds
  by stress test, `your_lineup_by_week`, roster/drop check, warnings), and
  `league_intel` (who is contending, trade-block players, needs/surplus, past trades).
  Prefer partners out of the race; never trade with the current opponent mid-week.
- Season: `strategy_playbook` (the league's format), `season_outlook` (playoff/bye/
  title odds with a stress-test range, matchup-by-matchup path, record needed),
  `league_intel`, `schedule_planner` (byes, playoff-week schedules, light days),
  `power_rankings`. Basketball categories: `category_strategy` (build, core vs
  rotation spots), `player_values` (G-scores, punt-aware), `matchup_projection`.

FORMAT NOTES:
- Football: `week.proj` is ESPN's projection; `opp_rank_vs_pos` is the opponent's
  defense rank vs that position (1 = toughest); BYE = no game. Check how the league
  scores kickers before suggesting a kicker move.
- Basketball: lineups are DAILY (scoring periods are days; matchups run Mon-Sun).
  ESPN's player rater (`rater`) is the best single value number. Respect the
  league's pickup limit (`pickups`).

ROSTER PHILOSOPHY (important): treat most of the roster as a core the user keeps.
Judge players by season and multi-week averages, not one night's schedule or a short
cold streak. Never suggest dropping a player whose season value is higher than the
player being added (`add_player` previews show a `value_check` and a CAUTION warning).
If you don't know which spots the user rotates, ask.

MOVES (`set_lineup`, `add_player`, `drop_player`, `cancel_claim`, `propose_trade`) change the user's
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
    def season_outlook(league: LeagueArg = None, team: TeamArg = None) -> str:
        """Simulates the rest of the regular season and the playoff bracket and returns
        the team's playoff, bye and title odds with a stress-test range, its matchup-by-
        matchup win chances, the record needed, and every team's odds. Football points:
        from every lineup (recent ESPN projections, byes, injuries, live score) plus the
        best free-agent adds. Basketball categories: from per-game category production,
        the live category totals of the current matchup, and injured-player return
        assumptions."""
        return run(lambda h: h.season_outlook(league, team))

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
        """Evaluate a trade (read-only) on what decides seasons: both teams' starting
        lineups and season odds. Football points: lineup change per week under ESPN
        projections and season averages, plus both teams' playoff/title odds under five
        stress tests (same random draws before/after), roster/drop check, byes, injury
        and rival/opponent warnings, and a verdict. Basketball categories: weekly all-play
        win % change (season and last 15 days), per-category odds, and season odds under
        stress tests. A deal is a clear win only if it helps in every stress test."""
        return run(lambda h: h.trade_analyzer(league, give, receive, partner))

    @mcp.tool(annotations=READ_ONLY)
    def find_trades(
        partner: Annotated[str | None, Field(description="Only search trades with this team (default: every team).")] = None,
        must_receive: Annotated[list[str] | None, Field(description="Players you must get (all on one team).")] = None,
        must_give: Annotated[list[str] | None, Field(description="Players you want to move.")] = None,
        max_players: Annotated[int, Field(description="Most players per side, 1-3 (basketball: up to 2).")] = 3,
        deep: Annotated[bool, Field(description="Search up to ~75 s instead of ~22 s (covers every 3-player deal "
                                                "in a 12-team league). Use when the quick search reports it ran out of time.")] = False,
        league: LeagueArg = None,
    ) -> str:
        """Search every trade (read-only) that improves your starting lineup under both
        ESPN projections and season averages (basketball: season and last-15-day form),
        starters only: every player in a deal must start for the team that gets him, no
        bench throw-ins. Lists win-wins first, then deals that surely help you, each
        stress-tested on season odds for both teams, and reports exactly what was
        searched. Takes up to ~40 seconds (deep: ~90)."""
        return run(lambda h: h.find_trades(league, partner, must_receive, must_give, max_players, deep))

    @mcp.tool(annotations=READ_ONLY)
    def league_intel(league: LeagueArg = None) -> str:
        """League memory: every manager's situation (simulated playoff/title odds:
        contender, bubble or long shot), activity (moves, recent adds/drops, FAAB left),
        trade-block players, positional needs and surplus (basketball: weakest and
        strongest categories), trades this season and who traded with whom, winning FAAB
        bids, and the teams whose needs fit yours best as trade targets."""
        return run(lambda h: h.league_intel(league))

    @mcp.tool(annotations=READ_ONLY)
    def track_record(league: LeagueArg = None) -> str:
        """How much to trust the model, and how your decisions have done. Football: how
        well projection-based win odds predicted this league's matchups (Brier score,
        calibration) and an audit of your lineups (points vs the best lineup by
        projections and in hindsight, losses a better lineup would have flipped).
        Basketball: category win rates for you and the league."""
        return run(lambda h: h.track_record(league))

    @mcp.tool(annotations=READ_ONLY)
    def market_trends(league: LeagueArg = None) -> str:
        """Who fantasy managers everywhere are adding and dropping in the last 24 hours
        (Sleeper), matched to this league: free agent, on waivers, or which team has him.
        Flags trending players you can get and your players others are dropping. A read
        on news and demand: check the reason before acting."""
        return run(lambda h: h.market_trends(league))

    @mcp.tool(annotations=READ_ONLY)
    def player_usage(
        team: TeamArg = None,
        players: Annotated[list[str] | None, Field(description="Other players to look up.")] = None,
        league: LeagueArg = None,
    ) -> str:
        """Football: real NFL usage from nflverse for your players (or a team's), any
        players you name, and free agents whose role is growing: snap share, targets,
        target share, air-yards share, carries (season vs last 3 games), touchdown share
        of points, and buy/sell flags (volume without TDs = buy, TD-dependent = sell)."""
        return run(lambda h: h.player_usage(league, team, players))

    @mcp.tool(annotations=READ_ONLY)
    def game_environment(team: TeamArg = None, league: LeagueArg = None) -> str:
        """Football: the coming week's Vegas lines (spread, total, each team's implied
        points), byes, indoor/outdoor and stadium weather (wind, rain/snow, cold) around
        kickoff, which of your players are in each game, and a D/ST streaming board (ESPN
        projection plus the opponent's implied points). Basketball: today's and
        tomorrow's NBA totals and spreads (pace, blowout risk) with your players."""
        return run(lambda h: h.game_environment(league, team))

    @mcp.tool(annotations=READ_ONLY)
    def waiver_advisor(team: TeamArg = None, league: LeagueArg = None) -> str:
        """The week's pickup plan. Football: free agents ranked by how much they raise
        your starting lineup (rest of season and playoff weeks), each paired with the
        cheapest drop from your rotation spots (never a regular starter or a stash), a
        FAAB bid based on this league's winning bids, and Sleeper trending flags.
        Basketball: category pickups by weekly win % gain, rotation spots and the best
        streaming days. Read-only: use add_player to act."""
        return run(lambda h: h.waiver_plan(league, team))

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

    @mcp.tool(annotations=READ_ONLY)
    def streaming_options(
        day: Annotated[int, Field(description="When the pickup joins: 0 = today (games not yet started), 1 = tomorrow, ...", ge=0, le=6)] = 0,
        drop: Annotated[str | None, Field(description="Who to drop for the pickup (default: the rotation spot that costs least this week).")] = None,
        prior_add: Annotated[str | None, Field(description="A pickup made today, applied before evaluating (e.g. today's add when planning tomorrow's).")] = None,
        prior_drop: Annotated[str | None, Field(description="The player dropped for prior_add.")] = None,
        top: Annotated[int, Field(ge=1, le=25)] = 10,
        league: LeagueArg = None,
    ) -> str:
        """Basketball: rank free agents by how much they raise the odds of winning THIS
        matchup (live categories + remaining games), with categories each one moves, games
        left, and games next week. Read-only: use add_player to act."""
        return run(lambda h: h.streaming_options(league, day, drop, prior_add, prior_drop, top))

    @mcp.tool(annotations=READ_ONLY)
    def pending_claims(league: LeagueArg = None) -> str:
        """The user's waiver claims still waiting to process: who each adds and drops,
        the FAAB bid, when it was submitted, and its id (for cancel_claim)."""
        return run(lambda h: h.pending_claims(league))

    @mcp.tool(annotations=WRITE)
    def cancel_claim(
        claim: Annotated[str, Field(description="The player the claim adds, or the claim id from pending_claims.")],
        league: LeagueArg = None,
        confirm: ConfirmArg = False,
    ) -> str:
        """Withdraw one of the user's pending waiver claims (to change a bid or a drop,
        cancel it and submit a new claim with add_player). Preview first, then confirm."""
        return run(lambda h: h.cancel_claim(league, claim, confirm))

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
            f"Build my waiver plan for league '{league}'. Run waiver_advisor (lineup gain, the cheapest drop from my "
            "rotation spots, FAAB bid from this league's winning bids), market_trends (what managers everywhere are "
            "adding/dropping, and whether I can get them), and for football player_usage on the top candidates "
            "(is the role real: snaps, targets, carries). Check league_settings for waiver rules and pickup limits. "
            "Recommend at most 2 moves with exact drops and bids, the season-long reason, and how sure you are. "
            "Never drop a regular starter. Preview with add_player; don't submit until I approve."
        )

    @mcp.prompt(title="Trade ideas")
    def trade_ideas(league: str) -> str:
        """Find realistic trades that help both sides."""
        return (
            f"Find trade ideas for me in league '{league}'. Run find_trades (deep=true if it reports it ran out of "
            "time) and league_intel (contenders vs sellers, trade-block players, needs/surplus, past trades). For the "
            "2-3 best deals run trade_analyzer and report: the verdict, my playoff/title odds change in the worst and "
            "best stress test, both starting lineups' change by projections and by season averages, the week-by-week "
            "lineup (your_lineup_by_week), roster/drop needs, and why the other manager would say yes (how it looks to "
            "them in the app). Starters only: no bench throw-ins. Don't send anything; offer to preview propose_trade."
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

    @mcp.prompt(title="Season outlook", name="season_outlook")
    def season_outlook_prompt(league: str) -> str:
        """Playoff chances and long-term strategy."""
        return (
            f"Give me a season outlook for league '{league}'. Read strategy_playbook first, then season_outlook "
            "(playoff/bye/title odds with the stress-test range, my matchup-by-matchup path, record needed), "
            "league_intel (who's contending, who's selling) and schedule_planner (byes, playoff-week schedules). "
            "Basketball categories: also category_strategy. Tell me whether to buy, hold or sell and the 2-3 moves "
            "that would most raise my playoff (or, if I'm safe, title) odds, each with its worst case."
        )

    @mcp.prompt(title="Weekly GM review")
    def weekly_gm_review() -> str:
        """A full read-only review of every league: standing, decisions, moves to consider."""
        return (
            "Do my weekly GM review, read-only. Call my_leagues. For EVERY league: season_outlook (odds and "
            "stress-test range), track_record (how well the model predicted this league; lineup audit), "
            "league_intel (situations, trades, FAAB), waiver_advisor and find_trades (quick search). Then report "
            "per league: where I stand and what changed since last week, how last week's decisions graded out, the "
            "1-2 moves worth making with their worst case and how sure you are, and what would make each wrong. "
            "Do not call set_lineup, add_player, drop_player or propose_trade."
        )

    @mcp.tool(annotations=READ_ONLY)
    def refresh() -> str:
        """Clear cached ESPN data (use right after the user makes a move in the ESPN app)."""
        return run(lambda h: h.refresh())

    return mcp


def serve() -> None:
    logging.basicConfig(level=logging.WARNING)
    build_server().run()
