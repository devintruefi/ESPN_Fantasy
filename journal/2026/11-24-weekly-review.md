# 2026-11-24 weekly review

Read-only scheduled run (FB week 12, BB matchup 6 in progress). Nothing was changed in any league.

## Grading
- Matchup predictions graded today: 3 (all logged 2026-11-17).
  - FB-A wk 11 vs team 1: 83% -> lost 88.3-103.5 (Brier 0.689). The 83% was set Tuesday on
    incomplete early-week projections (95.7-62.1); by Sunday the model had it at 39%.
  - FB-B wk 11 vs team 3: 64% -> won 109.2-94.1 (Brier 0.130).
  - BB matchup 5 vs team 12: 81% -> lost 3-6 (Brier 0.656).
- Running Brier (journal, graded matchups): FB-A 0.689 (n=1), FB-B 0.130 (n=1), BB 0.656 (n=1),
  overall 0.492 (n=3). Worse than a coin flip (0.25), but n=3 is noise.
  Compare track_record: FB-A 0.210 (66 matchups), FB-B 0.217 (66), BB none reported.
- Past recommendations (all from 11-17): none were made.
  - FB-A trade with team 10 (Bijan Robinson+McBride+Diggs for Tuten+Higgins+Olave): skipped.
    A smaller version of the deal with the same partner is still the top option (below).
  - FB-A waiver Deshaun Watson for Alec Pierce: skipped. Pierce was dropped for Chiefs D/ST and
    Cam Ward was added at QB instead. Watson is still available and still the top QB add.
  - FB-B trade with team 9 (Jacobs+Love for McBride): skipped. Title odds 27.1 -> 26.2 without it.
  - BB trade with team 8 (Ace Bailey for Trae Young+Jrue Holiday): skipped.
- Sunday 11-22 lineup calls:
  - FB-A: optional T.J. Hockenson over DK Metcalf (+0.6 proj). Hockenson is in this week's
    "should have started" list, so it would have helped. The team scored 88.3 against a 98.6
    best-by-projection lineup, but would still have lost (103.5).
  - FB-B: the weather tie-break kept Metcalf over Travis Etienne Jr.; Etienne was the miss
    (6.9 pts on the bench). The team won anyway.
- Lessons:
  1. Matchup odds logged on Tuesday are stale for football. From now on, record a Sunday odds
     value in the note (or as a second row) so grading shows how much is just timing.
  2. Early-matchup BB live odds (81% on day 1-2) overstated the lead. Discount BB matchup odds
     logged before mid-week.
  3. Sub-1-point FLEX tie-breaks based on weather or implied totals showed no edge (0 for 2 this
     week). Use the projection unless the gap is at least 2 points.

## FB-A (objective: playoff odds, under 90%)
- Standing: 5-6, seed 6. Playoffs 34.8% (stress 31.2-36.0), title 1.1% (1.0-1.7).
  Change since 11-17: playoffs -32.0 pts (66.8 -> 34.8), title -2.3 pts, after the wk 11 loss to team 1.
- This week (wk 12): vs team 15 (3-8), win 64%, projected 110.7-99.8. Then wk 13 and wk 14 vs team 5:
  38% and 23%. George Pickens and Trey McBride are both on bye in wk 14.
  7-7 gets in 75%, 6-8 13%, 8-6 100%.
- Track record: Brier 0.210 over 66 matchups. Favorites at 50-60% won 62%, so the model is a bit
  timid here. Lineups: avg 14.7 pts/wk on the bench, gap to the best projected lineup 0.7, and
  0 losses that the best projected lineup would have won.
- Flags: QB is position rank 12. DK Metcalf's snap share is falling (90% -> 79%). Tank Dell is being
  dropped widely on Sleeper.
- Recommendations:
  1. Trade with team 10: give George Pickens + Trey McBride; get Tee Higgins + Luther Burden III |
     playoffs +3.1 .. +6.7 pts (trade_analyzer, all 5 stress tests) | medium | Wrong if Burden's role
     shrinks or Higgins misses time. Also wrong if the TE drop-off (McBride to Hockenson) is bigger
     than projected. The deal clears the wk 14 double bye, which accounts for most of the gain
     (wk 14 lineup 100.8 -> 109.3). It lifts team 10's title odds +3.9..+8.2, but team 10 is not a
     playoff-spot rival. It looks fair in the app (34.1 vs 32.3 season avg).
  2. Waiver: add Deshaun Watson (QB, CLE), drop Rachaad White (rotation spot), bid $10-13 of $73 |
     not stress-tested: starting lineup +1.6 pts/wk by projections, +0.6 by season averages | low |
     Wrong if Cam Ward's recent volume (36 attempts/game over the last 3) holds up, since the season-average
     view shows little gain.
- Decisions taken: unknown.

## FB-B (objective: title odds, playoffs over 90%)
- Standing: 7-4, seed 3. Playoffs 99.9% (stress 99.6-99.9), title 26.2% (22.4-26.2).
  Change since 11-17: playoffs +1.2 pts, title -0.9 pts. Bye odds 26.7%.
- This week (wk 12): vs team 4 (9-2), win 60%, projected 121.1-114.4. Then wk 13 vs team 16 (75%) and
  wk 14 vs team 13 (82%).
- Track record: Brier 0.217 over 66. Favorites at 50-60% won only 46%, so treat those games as coin
  flips. Lineups: gap to the best projected lineup 0.8, with 1 loss a projection lineup would have
  won (wk 10).
- Flags: Derrick Henry's carries are falling (18.0 -> 13.3/game over the last 3). Brenton Strange's
  target share is falling (15% -> 8%). Bhayshul Tuten is QUESTIONABLE.
- Recommendations:
  1. Trade with team 9: give DeVonta Smith + Texans D/ST; get Trey McBride + Rashee Rice |
     title +2.4 .. +6.2 pts (trade_analyzer, all 5 stress tests) | medium | Wrong if the streamed
     D/ST (Commanders, best free agent) scores well under Texans D/ST, or Rice's role stays small.
     McBride is on bye wk 14, which makes wk 14 a slight net loss (-2.3). It looks lopsided against
     the partner in the app (18.6 vs 22.1 season avg), so it should be easy to get accepted.
  - Considered and not recommended: team 9 again, Burrow+Tuten+Golden for Jahmyr Gibbs+Drake Maye
    (+0.8..+5.3). Maye is OUT and Tuten is questionable, and it gives up 3 starters.
- Decisions taken: unknown.

## BB (objective: title odds, playoffs over 90%)
- Standing: 4-1, seed 1. Playoffs 91.9% (stress 40.7-91.9), title 25.3% (1.7-25.3).
  Change since 11-17: playoffs -7.4 pts, title -17.0 pts. The weakest stress test is now "last-15-day
  form" (playoffs 40.7%, title 1.7%): recent production has dropped. On 11-17 it was "injured never
  return". Injured: Stephen Curry, Ja Morant, RJ Barrett.
- This week (matchup 6): vs team 1, model 17% today (logged 34% on 11-23), with the live category
  score shown as 6-3. Next: team 8 (52%), team 4 (66%).
- Track record: 4-1 (categories 5-4, 7-2, 6-3, 5-4, 3-6). Category ranks: FG% 1, PTS 2, AST 3;
  weakest TO 7, 3PM 7, FT% 7.
- Recommendations (pick one; both include Ace Bailey):
  1. Trade with team 6: give Julius Randle + Ace Bailey; get Bam Adebayo | title +0.4 .. +4.6 pts
     (trade_analyzer, all 5 stress tests) | medium | Wrong if the lost PTS/3PM volume (-4.3 PTS,
     -1.2 3PM per game) matters more than TO/STL gains (+15.6 / +10.4 pts of category win %).
     The worst case is the last-15-day test, where the team is weak anyway. The deal frees a roster
     spot for a streamer (Brook Lopez +2.2% weekly win).
  2. Trade with team 13: give Ace Bailey + Mo Bamba; get Cooper Flagg | title +0.1 .. +8.1 pts |
     low-medium | Wrong if punting BLK (-16.8) and TO (-9.2) costs more than the AST/PTS/STL gains.
     The worst case is about flat under last-15-day form. Team 13 is the matchup 14 opponent.
- Decisions taken: unknown.
