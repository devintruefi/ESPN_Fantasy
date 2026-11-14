# ESPN Fantasy Hub

An MCP server that connects Claude to your ESPN **fantasy football** and
**fantasy basketball** leagues: every team, roster, matchup, stat, setting and
transaction, plus strategy tools and (with your approval) roster moves.

Ask Claude things like:

- "Check all my lineups. Anyone hurt, on bye, or without a game tonight?"
- "Who should I pick up at RB this week in my work league, and what should I bid?"
- "Am I going to win my basketball matchup? Which categories can I still flip?"
- "Find me a trade: I'm deep at WR and need a RB. Who's a good partner?"
- "Who's actually the best team in my league, and who's been lucky?"

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
| `power_rankings` | All-play record (true strength), expected wins, luck, scoring and recent form |
| `category_report` | Category leagues: rank in every category, strengths/weaknesses, punts, swingable categories now |
| `waiver_upgrades` | Free agents who beat your weakest players |
| `trade_analyzer` | Both sides' value, each team's best-lineup change, your category changes |

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
ideas, Matchup game plan, Season outlook.

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
- **Freshness:** data is cached up to ~45 seconds. After a move in the ESPN
  app, ask Claude to refresh.
- **Unofficial:** ESPN has no public fantasy API. This uses the endpoints the
  ESPN site uses (via [`espn-api`](https://github.com/cwendt94/espn-api)),
  so an ESPN change can break things until updated.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest                          # offline tests against saved ESPN responses
python tests/tools/capture_fixtures.py    # refresh fixtures from two public leagues
```

Fixtures come from public leagues with every member name, account id and team
name replaced by placeholders.
