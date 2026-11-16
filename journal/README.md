# GM journal

Written by the scheduled, read-only GM briefings (and by hand). It is the memory that
lets the GM grade its own predictions and decisions over a season, so you can see how
far to trust it.

**This repository is public.** Entries use league labels and team ids only, never
league, team or manager names. Player names are fine.

| Label | League |
|---|---|
| FB-A | Football, full PPR |
| FB-B | Football, half PPR |
| BB | Basketball, 9-category head-to-head |

## Files

- `predictions.csv`: one row per forecast, graded later.
  `made_on,league,kind,subject,prob,outcome,graded_on,note`
  - `kind=matchup`: probability the user's team wins that matchup (`subject` = week or
    matchup number). `outcome` = 1 win, 0 loss, 0.5 tie.
  - `kind=playoffs` / `kind=title`: season odds snapshot; graded when the regular
    season or the playoffs end.
  - `kind=move`: a recommended move. `prob` = the model's worst-case change in the
    objective (percentage points); `outcome` = what happened (made / skipped), and the
    note says how it played out.
- `YYYY/MM-DD-<kind>.md`: one entry per briefing (`sunday-lineups`, `nba-daily` only
  when something needed attention, `weekly-review`).

## Entry format

```
# <date> <kind>
## FB-A
- Standing: record, playoff % (stress range), title %
- This week: opponent team id, win %, projected score
- Flags: lineup problems, injuries, weather, byes
- Recommendations: move | objective change (worst case .. best case) | confidence | what would make it wrong
- Decisions taken: what the user did (if known)
```

## Grading (weekly review)

1. Fill `outcome` for every matchup prediction whose matchup is over.
2. Report the running Brier score of matchup predictions (lower is better; 0.25 is a
   coin flip) and compare with `track_record` for the same league.
3. For each past recommendation, say whether it was made and whether it helped, using
   the lineup audit (points vs the best lineup by projection) where it applies.
4. Note any pattern (e.g. "overrated injured players' return dates") as a lesson to
   apply next time.
