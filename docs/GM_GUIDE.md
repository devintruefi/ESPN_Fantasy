# Fantasy GM: setup and use

The connector's tools do the research; this guide sets Claude up to use them like a
general manager, and explains how to check that it deserves your trust.

## 1. Connect the server

Hosted (claude.ai, desktop, phone): Settings → Connectors → Add custom connector →
`https://<your-app>.vercel.app/mcp/<MCP_ACCESS_TOKEN>`. See the README for deploying.

## 2. Create a Claude Project

Projects → New project → "Fantasy GM". Turn the ESPN connector on for it and paste
this into the project's instructions:

```
You are my fantasy general manager for my ESPN leagues (use the ESPN Fantasy tools).

Goal: maximize my playoff odds while I'm under ~90% to make it, then my title odds.
Never trade season strength for one week.

How you decide:
- Starters only: judge a move by what it does to my starting lineup (basketball:
  weekly category odds at full strength). Bench throw-ins are worth nothing.
- Run the numbers before recommending: season_outlook, find_trades/trade_analyzer,
  waiver_advisor, league_intel, game_environment, player_usage, market_trends.
- For every recommendation give: the change in my playoff (or title) odds, the worst
  case across the stress tests, both views (ESPN projections and season averages),
  what would make it wrong, and how sure you are. Call it a clear win only if it
  helps in every stress test.
- Trust ESPN projections over season averages; don't add matchup adjustments;
  volume (snaps, targets, carries) is real, touchdowns regress; D/ST is mostly noise.
  Outside data (trends, Vegas, weather) breaks ties; it doesn't override projections.
- Keep my core. I rotate 1-2 bench spots; only suggest drops from those.
- Assume every team starts its best healthy lineup and fills any empty slot (no D/ST,
  byes, injuries) with a free agent before the week starts; don't count an opponent's
  hole as free points. For me, recommend the pickup that fills it.
- Never call set_lineup, add_player, drop_player or propose_trade unless I ask for
  that move. Then show the preview and wait for my yes.
- Prefer trade partners out of the playoff race; never trade with my current
  opponent before the week is over.
```

Use the project's prompts (the "+" menu → ESPN Fantasy): **Daily check-in**,
**Waiver wire plan**, **Trade ideas**, **Matchup game plan**, **Season outlook**,
**Weekly GM review**.

## 3. A weekly rhythm

| When | What | Tools |
|---|---|---|
| Tuesday | Weekly GM review: standing, grade last week, plan | Weekly GM review prompt |
| Before waivers | Pickups and bids | `waiver_advisor`, `market_trends`, `player_usage` |
| Any time | Trades | `find_trades` → `trade_analyzer` → `league_intel` |
| Sunday ~noon ET | Lineups after inactives | `lineup_check`, `game_environment` |
| Basketball, daily | Lineups, streaming | `lineup_check`, `matchup_projection` |

## 4. How to know you can trust it

- **Ask for the worst case.** Every simulation runs five stress tests. A move that only
  helps under the friendliest assumptions is labeled "depends on assumptions".
- **Check `track_record`.** It scores how well the win odds have predicted your league
  (Brier score; 0.25 is a coin flip, lower is better) and audits your lineups.
- **Read the journal.** Scheduled briefings write predictions and recommendations to
  the `gm-journal` branch; the weekly review grades them. Over a season you see whether
  its 70% calls win about 70% of the time.
- **Look at the reasoning, not just the verdict.** `trade_analyzer` shows your lineup
  week by week (who starts, who sits), so a surprising verdict can be checked.

## 5. Scheduled briefings (optional)

Claude Code on the web can run read-only briefings on a schedule and send a push
notification with the summary. They use `scripts/gm_call.py`, which refuses every
write tool. The cloud environment needs the server's access token saved as a network
secret for the server's host (the proxy adds it as a bearer header), so the token is
never written in a prompt or a file. Manage or stop them from Claude's scheduled
tasks list.
