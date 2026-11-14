# ESPN Fantasy Hub — notes for Claude

MCP server for ESPN fantasy football + basketball. Python 3.10+, built on
`espn-api` (request layer + ID maps only) and the MCP Python SDK (FastMCP).

## Layout
- `src/espn_fantasy_hub/espn.py` — cached raw ESPN fetches (`LeagueClient`), error mapping.
- `views.py` — pure parsing of raw ESPN JSON into compact dicts (`LeagueView`, `ProSchedule`).
- `analysis.py` — lineup check; `strategy.py` — power rankings, categories, upgrades, trades, projections.
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
- Never commit secrets (`.env*`, `.vercel/`, config.toml are gitignored). `/health` shows yes/no only.

## Test / deploy
- `.venv/bin/pytest` (all offline). Fixtures: `tests/fixtures/*.json.gz`, refreshed with `tests/tools/capture_fixtures.py`.
- Pushing to `main` auto-deploys via the linked Vercel project (Python preset, entry `app.py`).
- Live check: `GET /health`, then MCP at `/mcp/<MCP_ACCESS_TOKEN>` (Streamable HTTP, JSON responses, POST only).
