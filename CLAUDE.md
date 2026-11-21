# ESPN Fantasy Hub — notes for Claude

MCP server for ESPN fantasy football + basketball. Python 3.10+, built on
`espn-api` (request layer + ID maps only) and the MCP Python SDK (FastMCP).

## Layout
- `src/espn_fantasy_hub/espn.py` — cached raw ESPN fetches (`LeagueClient`), error mapping.
- `views.py` — pure parsing of raw ESPN JSON into compact dicts (`LeagueView`, `ProSchedule`).
- `analysis.py` — lineup check; `strategy.py` — power rankings, categories, upgrades, projections.
- `projections.py` — public ESPN projection feed scored with league rules; `ffmodel.py` — football world,
  season sim, stress-test `SCENARIOS`; `trades.py` — football trade evaluation/search.
- `catmodel.py` — category G-scores and weekly matchup model; `catseason.py` — basketball season sim,
  stress tests, category trades.
- `intel.py` — league memory and track record; `external.py` — outside fetches (Sleeper, nflverse, id map,
  ESPN site scoreboards, Open-Meteo), cached and fail-soft; `insights.py` — pure logic on that data.
- `writes.py` — move payloads, lineup planning, POST to ESPN; `service.py` (`Hub`) — one method per tool.
- `server.py` — FastMCP tools/prompts + INSTRUCTIONS; `remote.py` — token-protected stateless HTTP (Vercel).
- `app.py` — Vercel entry (Python preset). `cli.py` — stdio server, `setup`, `doctor`.

## Rules of thumb
- Keep `views`/`analysis`/`strategy` pure (no network) so they're testable on fixtures.
- ESPN stat splits: `statSourceId` 0 actual / 1 projected; `statSplitTypeId` football 0 season, 1 week;
  basketball 0 season, 1/2/3 last 7/15/30, 5 single game. Box-score matchup totals have ids ending in `null`.
- ESPN sometimes sends stats as text: use `views.num()` before arithmetic.
- espn-api's football stat map has duplicate names (61 is receiving yards *per game*); see `FOOTBALL_STAT_FIXES`.
- Basketball day math uses US Eastern (`ESPN_TZ`); display uses the configured time zone.
- `lineupLocked` describes the current scoring period only; future days use game start times.
- Moves are always preview-first (`confirm=false`) and gated by `ESPN_ALLOW_WRITES`.
- GM rules: objective = playoff odds under 90%, else title; starters only; a "clear win" helps in every
  stress test; trade lineup math gives every team the best free agent per position (holes vs waivers).
- Empty slots get filled (user's standing rule): any team with no eligible starter for a slot (no D/ST,
  bye, injury) is assumed to pick up the best free agent for it before the week starts, and every team
  starts its best healthy lineup. `FootballWorld.lineup_total` does this by default; pass `fill=False`
  only when pricing the user's own pickup against the real hole (waiver advice, best adds).
- Basketball pickups (user's standing rule): a move made after that day's first game has started takes
  effect the next day for both sides: the player added can't play until tomorrow, and the player dropped
  still plays today. So make tomorrow's add any time today. `streaming_options` models this.
- Waiver claims: `pending_claims` lists the queue with ids; `cancel_claim` withdraws one (WAIVER +
  executionType CANCEL + relatedTransactionId). Several claims sharing one drop = only one can win.
- Speed matters on Vercel (120 s limit, clients may time out sooner): avoid `statistics.mean` in hot loops;
  `find_trades` has a time budget and reports coverage.
- Outside data must never break a tool: fetchers return None on failure and tools say what was missing.
- Scheduled briefings and the decision journal live on the `gm-journal` branch (no deploys from it).
- Never commit secrets (`.env*`, `.vercel/`, config.toml are gitignored). `/health` shows yes/no only.

## Test / deploy
- `.venv/bin/pytest` (all offline). Fixtures: `tests/fixtures/*.json.gz`, refreshed with `tests/tools/capture_fixtures.py`.
- Pushing to `main` auto-deploys via the linked Vercel project (Python preset, entry `app.py`).
- Live check: `GET /health`, then MCP at `/mcp/<MCP_ACCESS_TOKEN>` (Streamable HTTP, JSON responses, POST only).
