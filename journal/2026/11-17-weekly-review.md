# 2026-11-17 weekly review

First weekly review in the journal: no earlier entries, so no "change since last review" and
no journal predictions to grade yet. Matchup predictions for this week are logged below so the
next review can start grading. (Written by the scheduled run; saved from the setup session
because the scheduled session could not push.)

## Grading
- Journal matchup predictions graded: 0 (none existed before today). Running Brier: n/a.
- Model's own track record (track_record): FB-A Brier 0.220 over 60 matchups, FB-B 0.218 over
  60 (coin flip = 0.25). BB: no Brier reported yet (4 matchups, categories only).
- Calibration note: FB-A favorites at 50-60% won 63% (model a bit timid); FB-B favorites at
  50-60% won only 45% (near coin flips there are coin flips). Lesson: don't lean on 50-60% edges in FB-B.
- Past recommendations: none in the journal yet.

## FB-A (objective: playoff odds, under 90%)
- Standing: 5-5, seed 5. Playoffs 66.8% (stress 64.4-68.5), title 3.4% (2.7-4.2).
- This week (wk 11): vs team 1, win 83%, projected 95.7-62.1 (Bijan Robinson, Seahawks D/ST on bye).
  Remaining: wk 12 vs team 15 (64%), wk 13 & 14 vs team 5 (35%, 29%). 7-7 gets in ~74%, 8-6 ~100%.
- Track record: 5-5; avg 15.2 pts/wk left on bench, but gap to best projected lineup is -0.2 and
  0 losses a projection-best lineup would have won: lineup decisions have been sound.
- Flags: QB is the weak spot (position rank 12). George Pickens snap share falling.
- Recommendations:
  1. Trade with team 10: give Bijan Robinson, Trey McBride, Stefon Diggs; get Bhayshul Tuten,
     Tee Higgins, Chris Olave | playoffs +3.7 .. +4.1 pts (trade_analyzer, all 5 stress tests) |
     medium | wrong if Tuten's workload drops or Olave misses time; also lifts team 10's title
     odds +3.9..+5.5; app season averages roughly equal (50.7 vs 51.8), so it may be a hard sell.
  2. Waiver: add Deshaun Watson (QB, CLE), drop Alec Pierce (WR, IR, rotation spot), bid $3-10 of
     $93 | not stress-tested: starting lineup +2.3 pts/wk by projections, +0.7 by season averages |
     low | wrong if Watson's projection is inflated (season-average view says almost no gain).
- Decisions taken: unknown.

## FB-B (objective: title odds, playoffs over 90%)
- Standing: 6-4, seed 3. Playoffs 98.7% (stress 97.2-99.3), title 27.1% (25.0-28.5).
- This week (wk 11): vs team 3, win 64%, projected 115.3-105.2 (Josh Jacobs, Matthew Golden on bye).
  Remaining: wk 12 vs team 4 (69%), wk 13 vs team 16 (73%), wk 14 vs team 13 (74%).
- Flags: Derrick Henry carries falling (18.5 -> 12.7/game last 3). TE slot is weak.
- Recommendations:
  1. Trade with team 9: give Josh Jacobs, Jordan Love; get Trey McBride | title +1.8 .. +9.3 pts
     (trade_analyzer, all 5 stress tests) | medium | wrong if Jacobs' RB slot is filled worse than
     modeled or McBride's target share keeps sliding; McBride is on bye wk 14.
  - Considered and rejected: a team 8 deal for Brock Bowers + Emmett Johnson (+2.6..+5.9) because
    Emmett Johnson never starts for you (bench throw-in).
- Decisions taken: unknown.

## BB (objective: title odds, playoffs over 90%)
- Standing: 4-0, seed 1. Playoffs 99.3% (stress 77.1-99.0), title 42.3% (5.0-41.4).
  The wide range is the "injured players never return" test (Stephen Curry, Walker Kessler,
  Ja Morant): title falls to 5% if they don't come back.
- This week (matchup 5): vs team 12, win 81% (live categories + games left). Next: team 1 (31%).
- Track record: 4-0 (categories 5-4, 7-2, 6-3, 5-4). Category win rates: PTS and FG% 100%, TO 25%
  (rank 9), AST and 3PM rank 6.
- Recommendations:
  1. Trade with team 8: give Ace Bailey; get Trae Young, Jrue Holiday (drop Jerami Grant, a rotation
     spot) | title +2.9 .. +6.0 pts (trade_analyzer, all 5 stress tests) | medium | wrong if punting
     BLK and TO costs more than AST gains, or Trae Young misses time; their lineup value drops, so
     they may refuse.
- Decisions taken: unknown.
