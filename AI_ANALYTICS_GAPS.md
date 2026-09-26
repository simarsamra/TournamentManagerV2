# AI analytics: gaps in how the AI sees a tournament's structure

The server test found that the AI doesn't know about groups. In a groups-plus-knockout
tournament it called the top spot "a nail-biter between Red Rovers and Golden Boots",
but the two teams lead different groups. This file lists that bug and others like it:
places where the facts the AI is given don't say how the tournament is built, so it
tells a good-looking story that's wrong.

Nothing here is fixed yet. Each item says where the problem is and suggests a fix.
The step-by-step fix plan is [`AI_STRUCTURE_PLAN.md`](AI_STRUCTURE_PLAN.md).

## How these were found

- **Code review.** I read all four places that build facts for the model (below) and
  everything they call. None of `core/ai/` reads `bracket_type`,
  `teams_per_group_advance`, the tiebreaker order, whether a team has withdrawn, or the
  registration mode. The only group handling is one `group` field on snapshot results,
  and one rule that stops a draw being picked in a what-if for a knockout match.
- **Probe on a scratch database.** A script built three tournaments with the real
  scheduling and standings code, then printed exactly what each facts builder gives the
  model:
  - **Hybrid:** 8 teams in 2 groups of 4, top 2 go through. The group stage was played,
    the knockout seeded, then one semi-final, then the rest to the end.
  - **Knockout:** 8 teams, with a third-place match, after round 1.
  - **Round robin:** 6 teams, after 6 results, then one team withdraws.

  Items tagged **[probed]** were reproduced this way. Items tagged **[code]** come from
  reading the code only.

The four facts builders:

| Builder | Used by |
|---|---|
| `facts.build_facts` | Answers routed to one card, plus the explanation under it |
| `snapshot.build_snapshot` | Conversational answers ("ask anything") |
| `recap.build_recap_facts` | The news board and organizer recaps |
| `team_news.build_team_facts` | "My team's take" |

## The root cause

All four builders call `calculate_standings(tournament)` with no group, and give the
model one ranked table. That's correct for a league and wrong for everything else:

- In **hybrid** tournaments, that single call mixes all the groups into one table. It
  also counts **knockout matches as league points** (G-2): the call only filters by
  status, and knockout matches in a hybrid have `group=""`.
- In **bracket formats** there's no table at all. The builders send a wins list sorted
  by win count, while the news prompts still ask for "the top of the table (ranks and
  points)" and "the teams just above and below you".
- Nothing says **what a match was**: group game, quarter-final, final, third-place
  match, losers bracket, consolation.
- Nothing says **what state a team is in**: through, out, still in contention, one life
  left in the losers bracket, or withdrawn.

The number check can't catch any of this. The numbers are real; it's the words around
them ("top of the table", "sent packing", "closing in") that are wrong.

---

## Groups plus knockout (hybrid)

### G-1: One table across all groups (the reported bug). High. [probed]
**Where:** all four builders, and the what-if facts.

**What the model sees**, with the probe's groups (A = Red Rovers, Green Giants,
Silver Hawks, Black Bears; B = Golden Boots, Blue Jays, Purple Pumas, Orange Owls):
- One `standings_top` / `table` ranked 1 to 8, with no group column.
- "My team's take" for Blue Jays (2nd in group B) lists `teams_around_you` as
  Green Giants and Silver Hawks. Both are in group A, and Blue Jays never play them in
  the group stage.
- `leader` is the overall leader, not Blue Jays' group leader.

**What goes wrong:** "a nail-biter at the top between Red Rovers and Golden Boots";
"Blue Jays are chasing Green Giants"; "4th of 8" for a team that is 2nd in its group
and through to the knockouts.

**Fix:**
- Build one table per group with `calculate_standings(tournament, group=g)`, as the
  standings page does in `core/views/reporting.py` `standings_view`.
- Send `groups: {"A": [...], "B": [...]}`.
- In team news, send the team's group, and take the teams around it and its leader from
  that group only.

### G-2: Knockout wins count as league points in hybrid. High. [probed]
**Where:** `core/standings.py` `calculate_standings`. When no group is given, it counts
every confirmed match. Knockout matches in a hybrid have `group=""`, so a knockout win
adds `points_per_win` and a game played.

**Probe:** Red Rovers had 9 points from the group stage. After winning the semi-final
the "table" says 12, and after the final it says 15. In the finale, Golden Boots, who
lost a semi-final, rank 2nd on 9 points. Green Giants, the losing finalist, rank 3rd.

**What goes wrong:**
- The finale's "final standings" and each team's "final rank" rest on this made-up
  table. The losing finalist's team story says they finished 3rd.
- The snapshot's `matches_left` and `max_possible_points` count the final as worth
  3 more points (Red Rovers: 1 match left, maximum 15).

**Not only the AI:** these pages also rank hybrids with the same call:
- the analytics page: Points Overview, and the base table for the what-if simulator
  (`analytics_view`, `core/views/reporting.py:211`);
- the team dashboard's rank (`core/views/auth.py:319`).

The standings page and the knockout seeding are fine: they ask for one group at a time.

**Fix:** for hybrids, build the tables per group (G-1) and never sum across groups. If
`calculate_standings(tournament)` itself is changed, the analytics page and team
dashboard need the same fix and their own tests.

### G-3: Nothing says how many teams go through, or who has. High. [code]
**Where:** all four builders. `teams_per_group_advance` is never included.

**What goes wrong:**
- The model can't say "top 2 go through". It can't tell "through", "out" or "still in
  it" apart, so it can't write "Red Rovers are closing in on a semi-final spot" (the
  test's own example).
- `points_behind_leader`, `points_ahead_of_next` and `max_possible_points` are measured
  against the overall leader, and include knockout matches (G-2). So "can Silver Hawks
  still catch the leader?" is answered against the wrong team.

**Fix:**
- Add `advance_per_group` to the facts.
- For each team in the group stage, work out a qualification status in code from its
  group only: `through`, `out` or `in contention`, using points still available in
  group matches.
- Give gaps to the last qualifying place in the group rather than to the overall leader.

### G-4: Knockout matches aren't labelled as knockout, or by round. High. [probed]
**Where:**
- recap `new_results` and `coming_up`;
- snapshot `results` and `fixtures`;
- team news `your_results` and `your_next_matches`.

**Probe:**
- The semi-final "Red Rovers 3-1 Blue Jays" looks like any other group result in the
  recap.
- The other semi-final is `coming_up` as "Green Giants v Golden Boots", with no stage.
- The snapshot shows knockout rounds as `round: 4` and `round: 5`, continuing the group
  numbering. The final is listed as "Red Rovers v TBD, round 5".

**What goes wrong:**
- The news board describes a semi-final win as another group-stage three points.
- A preview misses that the next match is the final.
- The conversation calls a semi-final "round 4".

**Fix:** a `stage` on every result and fixture, worked out in code from the match:
"Group A", "Quarter-final", "Semi-final", "Final", "Third-place match". The bracket
templates already name rounds this way (`standings_content.html`).

### G-5: Being knocked out isn't in the facts. Medium. [probed]
**Probe:** after Blue Jays lost their semi-final, their team news had
`your_next_matches: []`, a rank of 4th of 8 (G-1 and G-2), and nothing else.
Teams 3rd and 4th in each group after the group stage get the same treatment.

**What goes wrong:**
- The "table" part hypes a team that's already out.
- A team knocked out in the group stage is told the chase is on.

**Fix:** a per-team status: `in group stage`, `through to <stage>`, `out in <stage>`,
`champion`. The team news prompt should use it ("your run ended in the semi-final").

### G-6: The hybrid finale has no runner-up or third place. Medium. [probed]
**Where:** `recap.final_placings`. Only leagues get a top three. Other formats get just
`tournament.champion`.

**Probe:** hybrid finale facts: `champion: Red Rovers`, `runner_up: null`,
`third: null`. The losing finalist's team story is told "your final rank 3", from the
G-2 table.

**Fix:**
- The runner-up is the loser of the final (the code in `core/views/auth.py` around
  line 330 already finds it). Third is the winner of the third-place match, if there
  is one.
- In the finale, drop the merged table for hybrids. Show the group tables as they ended
  the group stage, and each team's stage reached.

### G-7: Recap position changes compare places in the merged table. Medium. [code]
**Where:** `recap.build_recap_facts` builds `position_changes_since_last_recap` from the
merged `standings_top`.

**What goes wrong:** a group B result moves a group A team "down to 4th" when nothing
changed in group A. Once the knockouts start, knockout wins move teams "up" (G-2).

**Fix:** compute changes within each group, and only during the group stage.

### G-8: Questions and what-ifs don't know about groups. Medium. [code]
**Where:**
- the router's `SYSTEM_PROMPT` ("standings: the league table");
- `analytics.simulate` / what-if facts.

**What goes wrong:**
- "Who leads group B?" and "Who's through to the semis?" go to the merged table (G-1),
  or to "unknown".
- The what-if re-ranks the merged table with knockout points included, so "would Blue
  Jays go through if they beat Purple Pumas?" can't be answered.

**Fix:**
- Let the route carry an optional group.
- Re-rank a what-if within the match's group, and report the qualification status
  before and after (G-3).
- Add a few group questions to `core/ai/eval/questions.json`.

---

## Knockout, double elimination and consolation

### K-1: The prompts ask for a table that bracket formats don't have. High. [probed]
**Where:**
- The news prompts: `recap.RECAP_PARTS` asks for "the top of the table (ranks and
  points)", and team news asks for "where they stand and the teams just above and below
  them".
- The facts for these formats are `results_top`, a wins list sorted by win count.

**Probe:** after knockout round 1, `results_top` lists the four winners at 1-0 in
alphabetical order, then the four losers at 0-1.

**What goes wrong:** the model is asked for a table and handed a list that looks like
one, so it writes "Blue Jays top the table". In a knockout nobody tops anything. They
reached the semi-finals.

**Fix:**
- Format-specific prompt parts: for brackets, "who is still in, and the next round",
  instead of "table".
- Replace `results_top` with a bracket summary: teams still in, the next round, and who
  went out in each round.

### K-2: Rounds aren't named. High. [probed]
The same problem as G-4, for every bracket format. The knockout probe's `coming_up`
shows the two semi-finals with no stage, and the snapshot shows the final as
"round 3, TBD v TBD".

**Fix:** the same `stage` label as G-4. For double elimination, also say which bracket
("winners bracket semi-final", "losers bracket round 2", "grand final",
"grand final decider").

### K-3: Out, still in, or one life left? Not in the facts. High. [probed / code]
**Probe:** in the knockout probe, Black Bears (lost in round 1) get team news with one
loss and `your_next_matches: []`. Nothing says they're out.

**Code:**
- Double elimination: a team that loses in the winners bracket drops to the losers
  bracket and is still alive. Nothing in the facts says so, and the prompt encourages
  lines like "sent packing".
- The grand final and the bracket-reset decider aren't marked either, so the model may
  crown a champion after the first grand final.

**Fix:** the per-team status from G-5, plus a double-elimination variant:
`winners bracket`, `losers bracket (one more loss and out)`, `out`, `champion`.

### K-4: Consolation-bracket results count the same as the main draw. Medium. [code]
**Where:**
- `analytics.team_performance` sorts on wins over every match.
- The recap and snapshot results don't show the bracket.

**What goes wrong:**
- A team that goes deep in the consolation bracket ranks alongside the main-draw
  finalists.
- The consolation final can be written up as "the final".

**Fix:** the bracket in each result's `stage` ("Consolation semi-final"). Rank on main
draw progress first.

### K-5: Bracket finales only name the champion. Medium. [code]
The same code as G-6. For knockout, consolation and double elimination, the runner-up
and third place are known from the final and the third-place match, but aren't in the
facts. The finale prompt asks to celebrate "the runner_up and third if in FACTS", so
they never get mentioned.

### K-6: The snapshot lists "TBD v TBD" matches. Low. [probed]
**Where:** `snapshot.build_snapshot`. `fixtures` includes placeholder matches whose
teams aren't known yet, and `tournament.matches_left` counts them. The recap already
filters them out (`upcoming_fixtures`).

**What goes wrong:** "TBD" reads like a team name ("TBD play TBD"). The count of
matches left mixes real fixtures with bracket slots.

**Fix:** leave out matches with no known team, or keep them as a count
(`"later_rounds": 3`) labelled with their stage.

### K-7: A team with a first-round bye looks like it hasn't started. Low. [code]
**Where:** team news and the snapshot. A `bye` match isn't counted as finished or as
upcoming.

**What goes wrong:** a team that got a bye has no results, so its story says "the
adventure is yet to begin", even though it's already in round 2.

**Fix:** include byes as a result row: "advanced with a bye".

---

## Withdrawals

### W-1: Withdrawn teams are in the tables with no flag. Medium. [probed]
**Where:**
- `facts.build_facts` `standings_top`;
- recap `standings_top`;
- team news `teams_around_you`, `leader` and `teams_in_table`;
- `final_placings` for leagues.

All of these use every `calculate_standings` row, and that includes withdrawn teams.

**Probe:** once Blue Jays withdrew, the recap still ranks them 2nd. Red Rovers' team
news names Blue Jays as the team just below them.

**What goes wrong:**
- "Blue Jays are breathing down your neck", about a team that has left.
- A withdrawn team could be named league runner-up in the finale.

**Fix:** mark withdrawn rows (`"withdrawn": true`) or leave them out, the same way in
every builder. Never give a withdrawn team a placing.

### W-2: The snapshot drops withdrawn teams but keeps their rank numbers. Low. [probed]
**Probe:** the snapshot's ranks go 1, 3, 4, 5, 6: Blue Jays' 2nd place is missing.
`points_ahead_of_next` is worked out on the shortened list, so it compares teams that
aren't neighbours in the ranks shown.

**What goes wrong:** "Golden Boots are 3rd", while the page shows them 3rd behind a
withdrawn team, and the model has no idea why 2nd is missing.

**Fix:** the same treatment as W-1, so every builder agrees.

### W-3: Forfeits after a withdrawal are written up as wins. Low. [code]
**Where:** `core/withdrawals.py`.
- With the "forfeit" policy, a withdrawn team's remaining matches become forfeits,
  which the news writes up with puns about the "winner".
- With "void", those matches just disappear.

In both cases, the fact that a team withdrew never reaches the model.

**Fix:**
- Add a `withdrawals` list to the recap facts (team and date).
- Mark forfeit results that came from a withdrawal ("walkover: X withdrew").
- Tell the prompt to report these plainly.

---

## Ranking and wording

### T-1: Why a tied team ranks higher is never given. Medium. [probed]
**Probe:** in the league, Red Rovers and Blue Jays are both on 6 points with the same
game difference, and are ranked 1st and 2nd. The facts don't include the tiebreaker
order (`tournament.tiebreaker_order`) or which tiebreaker decided it.

**What goes wrong:** the model explains it with words, not numbers ("top on goal
difference"), so the number check can't catch it. Here it's false: it was decided by
games won, head-to-head, or the fixed last-resort order.

**Fix:**
- Add `tiebreakers` in order.
- For each pair of teams level on points, add `separated_by` (worked out in code).

### T-2: Score words don't fit the sport. Low. [code]
Scores are sets or games in badminton, tennis, volleyball and table tennis, goals in
soccer, and runs in cricket. The facts call the difference `game_diff`, and the model
says "goal difference" in a badminton event.

**Fix:** a per-sport `score_unit` in the tournament facts ("sets", "goals", "runs"),
and name the difference to match.

### T-3: Individual events are written as teams. Low. [code]
In individual mode the competitors are players. `participant_label` is "Player" when
`players_per_team == 1`. The prompts and "My team's take" still say "team",
"your team" and "teams around you".

**Fix:** pass the participant word to the prompts, and adjust the panel label.

---

## Match states and changed results

### S-1: Played matches awaiting confirmation show as fixtures. Low. [code]
**Where:** `snapshot.UPCOMING` includes `pending_confirmation` and `disputed`, so a
played match whose score isn't confirmed yet is listed as a fixture and counted in
`matches_left`.

**What goes wrong:** "Aces still have to play Bolts", about a match finished an hour
ago.

**Fix:** list those matches separately ("awaiting confirmation"), without the score.

### S-2: A corrected score keeps its old headline. Medium. [code]
**Where:** `recap.news_board`.
- Headlines are stored per match id and shown next to the score from the database.
- `covered_match_ids` means a later recap never writes about that match again.

**What goes wrong:** an organizer override or a dispute resolution changes a score, and
the board shows "Aces crush Bolts 3-0" next to 1-3. The main story keeps the old
result too.

**Fix:**
- When a covered match's result changes, drop its headline.
- Take it out of `covered_match_ids` so the next update covers it again. Or store the
  score each headline was written for, and hide a headline whose score no longer
  matches.

### S-3: The snapshot's result date can be broken. Low. [probed]
**Where:** `snapshot.py` builds `"date": _when(m.scheduled_time)[:10]`.

**Probe:** an unscheduled match's date comes out as `"not schedu"`. The date is also
the scheduled day, not the day it was played. The news board uses `played_on`, so the
two can disagree.

**Fix:** use `recap.played_on`, and `None` when there's no date.

---

## Suggested order of work

1. **A shared structure helper.** One function in `core/analytics.py` that works out
   the tournament's structure once:
   - stage (group or knockout);
   - per-group tables and the advance count;
   - each team's status (through, out, in contention, losers bracket, withdrawn,
     champion);
   - a stage label for every match.

   All four builders use it. This fixes G-1, G-3 to G-7 and K-1 to K-5 together. The
   G-2 part of G-6 needs step 2.
2. **Hybrid table (G-2).** Stop summing across groups and counting knockout points,
   including on the analytics page and team dashboard, with tests. This is the one fix
   that changes numbers people already see on the site.
3. **Withdrawals (W-1 to W-3) and tiebreakers (T-1).**
4. **Prompts:** a news story per format (league, groups, bracket), using the new
   statuses instead of "table".
5. **Tests and eval:**
   - a facts test per format and structure in `core/tests_ai.py`, using the probe
     tournaments above;
   - group, knockout and withdrawal cases in `core/ai/eval/questions.json`, so
     `ai_eval` on the server catches regressions.
6. **The rest (S-1 to S-3, T-2, T-3, K-6, K-7):** each is small and separate.
