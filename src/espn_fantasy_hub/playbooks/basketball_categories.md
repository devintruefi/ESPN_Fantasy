# Playbook: head-to-head category basketball

## How this format is won
- {format_line}
- Weekly results are noisy: a team that is clearly better still loses a fair share of weeks. The
  edge comes from (1) being strong where it counts, (2) playing more games than the opponent, and
  (3) never wasting lineup spots.

## Valuing players
- Judge players by per-game production over the season and the last 2-4 weeks, never by one night
  or one cold week. Early-season samples are small; trust track record over 10 games of data.
- Percentages are volume-weighted: a 45% shooter on 20 attempts hurts FG% far more than one on 4.
- Weight categories by how *reliably* they swing a week. G-scores (Rosenof 2023) discount noisy
  categories; in this league steals and FG% are the noisiest, assists/points/rebounds the steadiest.
  Use `player_values` (G-scores) instead of raw rankings, and `category_strategy` for team fit.
- Fit beats generic value: the right pickup is the one that raises *your* weekly win odds in the
  categories you're building around, not the highest-ranked name.

## Punting (deliberately conceding categories)
- In most-categories leagues you only need a majority each week, so concentrating on 5-6 categories
  and conceding 2-3 is usually optimal. The academic H-score algorithm (Rosenof 2024) learns to punt
  on its own in head-to-head formats; practitioner guides agree.
- Classic pairings: punt FT% -> stack bigs (REB, BLK, FG%); punt FG% -> volume guards (PTS, 3PM, AST,
  FT%); punt AST (+TO) -> bigs and wings; punt TO pairs with stars who handle the ball (AST, PTS).
  Turnovers correlate with usage, assists and points, so TO is the cheapest category to give up.
- Don't punt a category your injured stars will fix when they return; plan for the full-strength team.
- Re-evaluate the build monthly with `category_strategy` (odds per category now vs full strength).

## Roster management
- Keep a core. Most of the roster stays put; only 1-2 rotation spots churn. Never drop a core player
  to gain one game, and never drop an injured star who is expected back.
- {pickup_line}
- Streaming: each extra game a week is worth a few points of weekly win probability. Use the rotation
  spots to pick up players who play on your light days (`schedule_planner` lists open lineup slots by
  day). Pick streamers who help your build categories.
- {waiver_line}
- Use the IR slot for anyone listed OUT so their bench spot can hold a streamer.

## Daily lineups
- Set lineups every day: start everyone with a game, bench players without one, check
  QUESTIONABLE/DAY_TO_DAY players before lock (`lineup_check`, use days_ahead for tomorrow).
- Late in the week, use `matchup_projection`: chase categories that are toss-ups, stop streaming for
  categories that are already won or lost, and protect FG%/FT% leads by not starting low-percentage
  players when a percentage lead is narrow (counting stats don't care, percentages do).

## Trades
- {trade_line}
- Target players whose value is concentrated in your build categories from teams whose build
  doesn't need them (`trade_analyzer`, `category_strategy`). Sell players whose value comes mostly
  from categories you're conceding.
- Buy low on injured stars from teams falling out of the playoff race; sell high on hot streaks.
- Before the deadline, prefer players whose NBA teams play 4 games in each fantasy playoff week.

## Season calendar
- {playoff_line}
- The goal of the regular season is a top seed (byes skip a round of weekly variance), then to be
  at full strength with good schedules for the playoff weeks.
