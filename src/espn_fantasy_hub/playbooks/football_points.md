# Playbook: head-to-head points football

## How this format is won
- {format_line}
- One win or loss per week, decided by total points. Weekly scores are noisy: a team lands within
  about 20 points (1 SD) of its projected total only two weeks in three, so a clearly better roster still
  loses a fair share of weeks. The edge comes from (1) a deeper set of starters, (2) never wasting
  a lineup spot on a bye or injury, and (3) peaking for the playoff weeks.
- {playoff_line} A first-round bye is worth a lot: winning two playoff games instead of three
  roughly doubles title odds for an evenly matched team.

## Valuing players
- Trust ESPN's weekly projection for start/sit. In this project's backtest it beat each player's
  season average at every position, and blending the average in never helped.
- Don't adjust projections for the matchup yourself. ESPN already prices it; adding a
  defense-vs-position bump on top made predictions worse out of sample.
- For future weeks, a player's recent weekly projections (last 3-4 weeks) are the best value
  number, much better than his season average. `season_outlook` uses exactly this.
- Buy usage, sell touchdowns: targets, target share and carries are stable; touchdown rates mostly
  regress. A player living on TDs is a sell; heavy volume with few TDs is a buy.
- Receptions: full PPR adds 0.5 points per catch over half PPR. Pass-catching backs, slot receivers
  and tight ends gain the most in full PPR; early-down backs gain the most in half PPR or standard.

## Weekly routine
- Before each slate run `lineup_check`: starters on bye, OUT/IR/DOUBTFUL, or projected below a
  healthy bench player. Lineups lock per game, so keep a Sunday-night or Monday player at FLEX when
  a starter is questionable, and re-check inactives about 90 minutes before kickoff.
- Variance: as a big favorite take the steadier option; as an underdog in a given week, the
  boom-or-bust one. (Only for close calls; projections first.)
- Byes: look two weeks ahead (`schedule_planner`, `season_outlook`) and fill holes before the
  waiver run, not on Sunday morning.

## Waivers and streaming
- {waiver_line}
- FAAB: most winning bids are small; big splashes rarely pay off. Bid $0-1 on streamers, spend real
  money only on a clear breakout (new starting role, injured starter ahead), and keep some budget for
  playoff-week injuries.
- D/ST weekly points are mostly noise (a defense's own average and the opponent's offense barely
  predict them); ESPN's projection for the current week is the best signal. Stream mainly to cover
  byes or for a clearly favorable current-week projection.
- {kicker_line}
- Handcuffs rarely pay off early, but late in the season a starter's backup can decide a playoff week:
  stash one for your most important RB once your bench has a dead spot.

## Trades
- {trade_line}
- Judge every trade by your best-lineup change, not total value: `trade_analyzer` shows both teams'
  lineup change. Consolidation (2-for-1) works when the player you get starts and the two you give
  would sit on your bench.
- Trade with teams out of the race; avoid strengthening the teams you're competing with for a
  playoff spot or that you still play. Don't accept a trade with your current opponent before that
  week's games are over.

## Roster philosophy
- Keep the core. Churn only bench spots that can't help: injured players with no timetable, players
  behind starters, and positions you can stream. Never drop a healthy starter-level player for a
  one-week matchup.
- Injured stars expected back before or during the playoffs are worth an IR slot.
