# ESPN Fantasy Hub

An MCP server that connects Claude to your ESPN **fantasy football** and
**fantasy basketball** leagues: every team, roster, matchup, stat, setting and
transaction, plus a "GM" layer (season simulations with stress tests, a trade
finder, league memory, a track record of how well it predicts, and outside data
from Sleeper, nflverse, Vegas lines and weather) and, with your approval, roster
moves.

Ask Claude things like:

- "Check all my lineups. Anyone hurt, on bye, or without a game tonight?"
- "Who should I pick up at RB this week in my work league, and what should I bid?"
- "Am I going to win my basketball matchup? Which categories can I still flip?"
- "Find me a trade: I'm deep at WR and need a RB. Who's a good partner?"
- "Who's actually the best team in my league, and who's been lucky?"
- "What are my playoff odds, and what's the worst case?"
- "Find every trade that makes my starting lineup better and is fair to the other side."

## Tools

**See everything**

| Tool | What it returns |
|---|---|
| `my_leagues` | Dashboard: every league, your team, record, seed, live matchup, lineup alerts |
| `league_settings` | Scoring (PPR / categories), roster slots, playoffs, waivers/FAAB, pickup limits, trade deadline |
| `standings` | Every team: record, seed, PF/PA, streak, playoff odds, waiver order, FAAB left, owners |
| `team_roster` | Any team's roster: slots, injuries, season/recent stats, projections, games this week |
| `all_rosters` | Every roster in one compact view (trade scouting) |
| `scoreboard` | All matchups: live scores, projections, win probability, category battles |
| `matchup_detail` | Box score: both lineups, each player's points/stats, game status, games left |
| `team_schedule` | Results, upcoming opponents, strength of remaining schedule |
| `free_agents` | Best available (free agents + waivers) by position, projection, form or trend |
| `player_info` | One player: owner, injury, ESPN outlook, game log, upcoming matchups, news |
| `recent_transactions` | Adds, drops, waiver claims (with FAAB bids), trades |
| `draft_results` | Every pick, auction price, keepers |

**Strategy**

| Tool | What it returns |
|---|---|
| `lineup_check` | Starters who are OUT, injured, on bye or without a game, the exact bench swap, free agents who play that day when the bench can't help, empty/IR slot tips, pickups left |
| `matchup_projection` | Basketball: projected final category totals (or points) for both teams this week, toss-ups, days with more players than slots |
| `season_outlook` | Simulated playoff, bye and title odds for every team with a stress-test range, your matchup-by-matchup win chances, the record you need; football also lists bye holes and the free agents that most raise your lineup |
| `strategy_playbook` | The research-based playbook for the league's format, filled in with its rules (football points, basketball categories) |
| `category_strategy` | Category leagues: odds in every category now and at full strength, which to build around or concede, core vs rotation spots, best-fit pickups |
| `player_values` | Category leagues: G-score rankings (per-game value discounted for week-to-week noise), punt-aware |
| `schedule_planner` | Games per week vs average, light days for streaming, playoff-week schedules; football byes by week |
| `power_rankings` | All-play record (true strength), expected wins, luck, scoring and recent form |
| `category_report` | Category leagues: rank in every category, strengths/weaknesses, punts, swingable categories now |
| `waiver_upgrades` | Free agents who beat your weakest players |
| `trade_analyzer` | A verdict on one trade: both teams' playoff/title odds under five stress tests, both starting lineups' change (projections and season averages), your lineup week by week, roster/drop check, opponent and rival warnings |
| `find_trades` | Every starters-only trade that improves your lineup (1-for-1 up to 3-for-3), win-wins first, the best ones stress-tested; `deep` for a full search |
| `league_intel` | Every manager: contender/bubble/long shot, activity, FAAB left, trade block, needs and surplus, trades this season; the teams that fit yours as trade partners; winning FAAB bids |
| `track_record` | How well the win odds have predicted this league (Brier score, calibration) and an audit of your lineups (points left on the bench, losses a better lineup would have flipped) |
| `waiver_advisor` | Pickups ranked by lineup gain, each with the cheapest drop from your rotation spots and a FAAB bid from this league's history |
| `market_trends` | What managers everywhere are adding and dropping (Sleeper, 24 h), and whether you can get them here |
| `player_usage` | Football: snaps, targets, target share, carries (season vs last 3), TD dependence, buy/sell flags (nflverse) |
| `game_environment` | Football: Vegas lines and implied points, byes, stadium weather, your players by game, D/ST streaming board; basketball: totals and blowout risk |

**Moves** (only when `ESPN_ALLOW_WRITES=1`)

| Tool | What it does |
|---|---|
| `set_lineup` | Start/bench players or place them in slots (basketball: any day this week) |
| `add_player` | Pick up a free agent, or a waiver claim with a FAAB bid, optionally dropping someone |
| `drop_player` | Release a player |
| `propose_trade` | Send a trade offer (experimental) |

Every move is **preview first**: Claude shows exactly what will change and only
sends it after you say yes (`confirm=true`). Claude's own permission prompt for
these tools is a second check; once you trust it you can set them to "always allow".

**Guided workflows** (MCP prompts): Daily check-in, Waiver wire plan, Trade
ideas, Matchup game plan, Season outlook, Weekly GM review (read-only).

## How the GM layer decides

- **Objective:** playoff odds while you're under ~90% to make it, then title odds. One
  week is never traded for the season.
- **Starters only:** a move's value is what it does to your starting lineup (basketball:
  weekly category odds at full strength). Bench throw-ins count for nothing.
- **Stress tests:** every season simulation runs five ways (football: ESPN projections,
  more randomness, season averages, a 50/50 blend, slower injury returns; basketball:
  season averages, more randomness, last-15-day form, injured players back later or
  never). A move is a clear win only if it helps in all five.
- **What it rests on:** in backtests on real leagues ESPN's weekly projections beat
  season averages at every position, matchup adjustments on top made things worse, and
  projection-based weekly win odds scored a Brier of 0.22 (coin flip 0.25).
  `track_record` shows how that holds up in your own league.
- **Outside data** (Sleeper, nflverse, ESPN's Vegas lines, Open-Meteo) is used for news
  and tie-breakers, never to override projections, and each source failing is reported,
  not fatal.

## Hosted setup (Claude on web, desktop and phone)

1. Import this repo at vercel.com/new and keep the **Python** preset (Vercel
   loads `app` from `app.py`). Any host that runs Python ASGI apps works too
   (`uvicorn app:app`).
2. Add environment variables (Project → Settings → Environment Variables,
   **Production**), then redeploy:

   | Variable | Value |
   |---|---|
   | `MCP_ACCESS_TOKEN` | A long random secret (e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`). It's the password: anyone with the URL can use your leagues |
   | `ESPN_S2`, `ESPN_SWID` | Your ESPN cookies (see below). Mark them Sensitive |
   | `ESPN_ALLOW_WRITES` | `1` to enable moves (leave unset for read-only) |
   | `ESPN_TIMEZONE` | e.g. `America/New_York` |
   | `ESPN_LEAGUES` | Optional, e.g. `football:123,basketball:456`. Your leagues are normally found automatically |

3. Check `https://<project>.vercel.app/health`. It shows what the server
   sees, as yes/no only: `access_token`, `espn_login`, `writes_enabled`, `timezone`.
4. In Claude: **Settings → Connectors → Add custom connector**, URL
   `https://<project>.vercel.app/mcp/<MCP_ACCESS_TOKEN>`.

**ESPN cookies:** log in at espn.com in Chrome, open DevTools (F12) →
Application → Cookies → `https://www.espn.com`, and copy `espn_s2` and `SWID`
(including the braces). They usually last months. If Claude says ESPN refused
access, copy fresh ones into Vercel and redeploy.

**Rotating the access token:** change `MCP_ACCESS_TOKEN` in Vercel, redeploy,
and update the connector URL in Claude.

## Local setup (Claude Desktop / Claude Code on one computer)

```bash
uv tool install /path/to/espn-fantasy-hub
espn-fantasy-hub setup     # paste cookies; writes ~/.config/espn-fantasy-hub/config.toml
espn-fantasy-hub doctor    # checks every league
```

Claude Desktop config (`Settings → Developer → Edit Config`):

```json
{ "mcpServers": { "espn-fantasy": { "command": "/Users/YOU/.local/bin/espn-fantasy-hub" } } }
```

Claude Code: `claude mcp add --scope user espn-fantasy -- espn-fantasy-hub`

Config file reference (`~/.config/espn-fantasy-hub/config.toml`; env vars override it):

```toml
espn_s2 = "AEB..."
swid = "{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}"
auto_discover = true        # find your leagues from your ESPN account
allow_writes = false        # true enables moves (always previewed first)
# timezone = "America/Chicago"

[[league]]                  # optional: add or nickname leagues by hand
sport = "football"          # or "basketball"
id = 12345678
name = "Work league"
# team_id = 3               # only if your team isn't detected automatically
```

## Good to know

- **Basketball** lineups are daily: `lineup_check` checks today; ask about
  tomorrow and Claude passes `days_ahead=1`. Weekly projections assume the
  matchup ends Sunday (All-Star/playoff matchups can be longer).
- **Category leagues** use ESPN's Player Rater as the "how good is he" number.
- **Slow tools:** `find_trades` takes up to ~40 seconds (`deep`: ~90); everything
  else answers in a few seconds.
- **Freshness:** data is cached up to ~45 seconds. After a move in the ESPN
  app, ask Claude to refresh.
- **Unofficial:** ESPN has no public fantasy API. This uses the endpoints the
  ESPN site uses (via [`espn-api`](https://github.com/cwendt94/espn-api)),
  so an ESPN change can break things until updated.

See [docs/GM_GUIDE.md](docs/GM_GUIDE.md) to set Claude up as your fantasy GM (project instructions, weekly rhythm,
how to check its track record, scheduled read-only briefings).

## Development

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest                          # offline tests against saved ESPN responses
python tests/tools/capture_fixtures.py    # refresh fixtures from two public leagues
```

Fixtures come from public leagues with every member name, account id and team
name replaced by placeholders.
