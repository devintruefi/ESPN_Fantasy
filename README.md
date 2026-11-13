# ESPN Fantasy Hub

An MCP server that gives Claude (Claude Desktop or Claude Code) a full,
**read-only** view of your ESPN **fantasy football** and **fantasy basketball**
leagues, so you can ask things like:

- "Check all my lineups. Anyone hurt, on bye, or without a game?"
- "Who are the best waiver adds at RB this week in my work league?"
- "I'm losing blocks and steals this week. Who on waivers helps, and how many games do they have left?"
- "Find me a trade: I'm deep at WR and need a RB. Who's a good partner?"
- "How does my rest-of-season schedule compare to the teams I'm chasing for a playoff spot?"

It reads the same data the ESPN site and app show, for **every team in each league**.

## What Claude can see

| Tool | What it returns |
|---|---|
| `my_leagues` | Dashboard: every league, your team, record, seed, live matchup, lineup alerts |
| `league_settings` | Scoring (points rules or categories, PPR), roster slots, playoffs, waivers/FAAB, trade deadline |
| `standings` | Every team: record, seed, PF/PA, streak, playoff odds, waiver order, FAAB left, owners |
| `team_roster` | Any team's roster: slots, injuries, season and recent stats, projections, games this week |
| `all_rosters` | Every roster in the league in one compact view (trade scouting) |
| `scoreboard` | All matchups for a week: live scores, projections, win probability, category battles |
| `matchup_detail` | Box score: both lineups, each player's points/stats, game status, games left |
| `team_schedule` | Season results, upcoming opponents, strength of remaining schedule |
| `lineup_check` | Starters who are OUT, injured, on bye, or without a game, plus the exact bench swap. Also flags empty slots, better bench options, and healthy players stuck in IR |
| `free_agents` | Best available (free agents and waivers), by position, sorted by projection / recent form / trend |
| `player_info` | One player: who owns him, injury, ESPN outlook, game log, upcoming matchups, latest news |
| `recent_transactions` | Adds, drops, waiver claims (with FAAB bids), trades |
| `draft_results` | Every draft pick, auction price, keepers |
| `refresh` | Clears cached data after you make a move in the ESPN app |

**It cannot make moves.** It never sets lineups, claims players or sends trades.
Claude tells you exactly what to do, and you do it in the ESPN app.

## 1. Install

You need [uv](https://docs.astral.sh/uv/getting-started/installation/), a
one-line install:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then install this server from the folder you downloaded:

```bash
uv tool install /path/to/espn-fantasy-hub
```

This puts an `espn-fantasy-hub` command on your PATH. To update later, run
`uv tool install --reinstall /path/to/espn-fantasy-hub`.

## 2. Connect your ESPN account

Private leagues (almost all of them) need two cookies from your ESPN login.
They stay on your computer, in a private config file, and are only ever sent
to ESPN.

1. Log in at <https://www.espn.com/fantasy/> in Chrome.
2. Open DevTools (`Cmd+Option+I` on Mac, `F12` on Windows), then go to
   **Application → Cookies → https://www.espn.com**.
3. Find **`espn_s2`** (a long string) and **`SWID`** (looks like
   `{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}`).
4. Run the guided setup and paste them in:

```bash
espn-fantasy-hub setup
```

Your leagues are **found automatically** from your account. If any are missing, add them in setup;
the league id is the `leagueId=` number in the league's URL. The setup saves
`~/.config/espn-fantasy-hub/config.toml` and then runs a check:

```text
✓ football   Sunday Funday — id 12345678, season 2026, 12 teams, your team: Bijan Mustard (6-3)
✓ football   Work League — id 87654321, season 2026, 10 teams, your team: Taco Corp (5-4)
✓ basketball Hoops — id 11223344, season 2027, 12 teams, your team: Splash Bros (7-2-1)
```

Run `espn-fantasy-hub doctor` anytime to recheck.

## 3. Add it to Claude

**Claude Desktop:** open Settings → Developer → Edit Config, and add the server
(use the full path printed by `which espn-fantasy-hub` on Mac, or
`where espn-fantasy-hub` on Windows):

```json
{
  "mcpServers": {
    "espn-fantasy": {
      "command": "/Users/YOU/.local/bin/espn-fantasy-hub"
    }
  }
}
```

Restart Claude Desktop. "espn-fantasy" should appear in the tools menu.

**Claude Code:**

```bash
claude mcp add --scope user espn-fantasy -- espn-fantasy-hub
```

## Config reference

`~/.config/espn-fantasy-hub/config.toml`. Environment variables
`ESPN_S2`, `ESPN_SWID` and `ESPN_LEAGUES` override the file.

```toml
espn_s2 = "AEB..."
swid = "{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}"
auto_discover = true        # find your leagues from your ESPN account
# timezone = "America/Chicago"  # default: your computer's time zone

[[league]]                  # optional: add or nickname leagues by hand
sport = "football"          # or "basketball"
id = 12345678
name = "Work league"        # optional nickname you can use when talking to Claude
# team_id = 3               # only if your team isn't detected automatically
# season = 2026             # only to pin a season
```

## Good to know

- **Cookies expire** (usually after about a year, or when you log out everywhere). If Claude
  says ESPN refused access, copy fresh cookies and rerun `espn-fantasy-hub setup`.
- **Data freshness:** live scores are cached for about 30 seconds and league data for about 90 seconds.
  After making a move in the ESPN app, ask Claude to refresh.
- **Basketball** lineups are daily. `lineup_check` checks today by default.
  Ask about tomorrow and Claude passes `days_ahead=1`. "Games left this week" counts
  through Sunday (ESPN's standard week). All-Star and playoff matchups can be longer.
- **Category leagues** use ESPN's Player Rater (`rater`), which already weighs
  your league's categories, as the single "how good is he" number.
- **Unofficial:** ESPN has no public fantasy API. This uses the same endpoints as
  the ESPN website (through the widely used
  [`espn-api`](https://github.com/cwendt94/espn-api) library), so an ESPN change
  can break things until it's updated.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest            # offline tests against saved ESPN responses
python tests/tools/capture_fixtures.py   # refresh fixtures from two public leagues
```

Fixtures come from public leagues with every member name, account id and team
name replaced by placeholders.
