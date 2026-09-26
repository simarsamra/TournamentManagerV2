# Plan: make the AI understand tournament structure

This plan fixes the gaps in [`AI_ANALYTICS_GAPS.md`](AI_ANALYTICS_GAPS.md): the AI treats
every tournament as one league. It doesn't know about groups, knockout rounds, bracket
sides, withdrawals or tiebreakers.

It is written for an AI coding agent to follow task by task. Every task lists the gaps it
closes, the files to touch, the exact behaviour to build, the tests to write first, a
mutation check, and when it's done.

Written against `claude/ai-analytics-ollama-plan` at `de71909`. The suite then had
**568 tests, 0 failures (3 skipped)**.

---

## 0. Ground rules

These are the same as `AI_ANALYTICS_PLAN.md` §0, restated so this file stands alone.

1. **One task, one commit.** Message: `<type>(<area>): <summary> [ST-<n>]`, with the gap
   ids in the body ("Fixes G-1, G-3."). End with the attribution lines the session gives
   you.
2. **Write the regression test first and watch it fail** for the reason the gap
   describes. Only then fix.
3. **Every task ends green on both backends** (SQLite locally, PostgreSQL in CI), with
   `ruff check .` clean. Run the whole suite, not just the new tests.
4. **Mutation check.** After the fix, undo its key line by hand, confirm the new test
   fails, then restore the line. Each task names the line to undo.
5. **Push, then confirm on real CI:** run the workflow with `workflow_dispatch` on the
   branch and wait for success before starting the next task.
6. **CI never talks to a model.** Use `core.ai.testing.FakeOllama`; the test runner
   blocks real network access.
7. **Find code by quoted text, not line numbers.** Line numbers below are hints as of
   `de71909`.
8. **The model explains, the app calculates** (`AI_ANALYTICS_PLAN.md` §1.1). Every
   status, stage name, gap and placing in this plan is computed in Python. The model
   only reads them. Never add a prompt line asking the model to work any of this out.
9. **Facts rules still hold** (§1.3): only display labels (`_team_display_label` /
   `label_standings`), never notes, users, availability or player names.
10. **Stop at `DECISION REQUIRED`.** Each decision below has a default. Use it unless the
    owner has written a different answer into §2 of this file. If they have, follow
    theirs.

Branch: `claude/ai-analytics-ollama-plan` (or the branch the owner names).

---

## 1. Design

### 1.1 One structure object, computed once

Add a new module, **`core/structure.py`**: plain functions of model objects, no HTTP,
next to `core/analytics.py`. It works out everything about the tournament's shape once,
and all four facts builders read from it:

- `facts.build_facts`: routed card answers;
- `snapshot.build_snapshot`: conversational answers;
- `recap.build_recap_facts`: the news board and recaps;
- `team_news.build_team_facts`: "My team's take".

Public API (names are binding; later tasks and tests use them):

```python
KIND_LEAGUE, KIND_GROUPS, KIND_BRACKET = "league", "groups", "bracket"

def structure_kind(tournament) -> str
    # round_robin, double_round_robin  -> "league"
    # hybrid                           -> "groups"
    # knockout, double_elimination,
    # consolation                      -> "bracket"

def stage_labels(tournament, matches) -> dict[int, str]
    # match pk -> "Group A" | "Round 3" | "Quarter-final" | "Final" | ... (ST-3)

@dataclass
class TeamState:
    team_id: int
    label: str            # display label
    group: str = ""       # hybrid only
    status: str = ""      # one of TEAM_STATUSES
    detail: str = ""      # stage reached / lost in / next, or "5th" for a placing
    withdrawn: bool = False

@dataclass
class Structure:
    kind: str
    phase: str            # "league" | "group_stage" | "knockout" | "finished"
    advance_per_group: int | None
    groups: dict[str, list[dict]]  # hybrid: {"A": calculate_standings(t, group="A") rows, ...}
    table: list[dict]              # league: calculate_standings(t) rows; [] otherwise
    teams: dict[int, TeamState]    # every team in the tournament, withdrawn included
    stages: dict[int, str]         # stage_labels() for every match
    placings: dict                 # champion / runner_up / third / semi_finalists (labels)
    tiebreakers: list[str]         # tournament.get_tiebreaker_order(), in plain words

def build_structure(tournament, label) -> Structure
    # label(team) -> display label, the builders' own function
```

Every row in `groups` and `table` has `display_label` set, as `label_standings` does, and
a `status`. The tie explanation (`separated_by`) is added in ST-5.

**Query budget.** `build_structure` loads:
- all the tournament's matches once, with `select_related("team1", "team2", "winner")`;
- all participations once;
- one `calculate_standings` per group (hybrid) or one call in total (league).

It must not run a query per team or per match. ST-4 pins this with
`CaptureQueriesContext`.

### 1.2 Turning the structure into facts

Add **`core/ai/structure_facts.py`**. It turns a `Structure` into the JSON fragment each
builder puts in front of the model, so all four describe the tournament in the same
words:

```python
def standings_facts(structure, *, rows=TOP_ROWS, focus_team_id=None) -> dict
    # league  -> {"table": [row, ...]}
    # groups  -> {"groups": [{"group": "A", "advance": 2, "table": [row + "status"]}, ...],
    #             "phase": "group stage" | "knockout" | "finished"}
    # bracket -> {"bracket": {"next_round": "Semi-final", "still_in": [...],
    #             "knocked_out": [{"team", "out_in"}], ...}}

def team_facts(structure, team_id) -> dict
    # {"your_group": "B", "your_status": "through to the knockouts",
    #  "your_group_table": [...]} for groups;
    # {"your_status": "out in the semi-final"} for brackets; and so on

STATUS_TEXT = {...}  # status -> the words the model may quote
```

The row format is `facts._standings_rows` plus `status` (and, from ST-5,
`separated_by`). Group tables are never merged. No fragment ever has a top-level ranked
table that mixes teams from two groups.

### 1.3 Prompt rule for every builder

Every prompt that shows the model a table gets this rule, word for word, so the
wording stays consistent and one test can check it:

> Only compare teams in the same group. Use each team's status as given ("through",
> "out", "in contention", "one life left"); never work it out yourself. In a bracket
> there is no table: talk about who is still in and the next round.

---

## 2. Decisions

Use the default unless the owner has written another answer here.

| # | Question | Default |
|---|---|---|
| D-1 | In a hybrid, should `calculate_standings(tournament)` with no group count only group-stage matches? This changes the numbers on the analytics page and the team dashboard (gap G-2). | **Yes.** Knockout matches earn no points anywhere. |
| D-2 | Withdrawn teams in the tables the AI sees: drop them, or keep them flagged? | **Keep them, flagged `"withdrawn": true`, at the same rank the standings page shows.** They never get a placing, qualification or "teams around you" slot. |
| D-3 | Stage names: the same rule as the bracket templates (by the number of matches in the round)? | **Yes** (see ST-3). |
| D-4 | Should the team dashboard show a hybrid team's rank within its group ("#2 in Group B")? | **Yes** (ST-1). |
| D-5 | Should the analytics page's Points Overview and what-if simulator show one table per group for hybrids? | **Yes, but last** (ST-13, optional). The AI work doesn't depend on it. |

Owner's answers (if any):

- 2026-09-26: the owner accepted all five defaults (D-1 to D-5).

---

## 3. Execution order

| Task | Summary | Gaps closed | Depends on |
|---|---|---|---|
| ST-0 | Test tournaments: one builder per format | — | — |
| ST-1 | Hybrid standings stop counting knockout matches | G-2 | ST-0 |
| ST-2 | Withdrawn teams can't be seeded; the "W" badge shows | X-1, X-2 | ST-0 |
| ST-3 | `structure.stage_labels` | (G-4, K-2 groundwork) | ST-0 |
| ST-4 | `structure.build_structure`: team states, phase, placings | (G-3, G-5, G-6, K-3, K-4, K-5, W-1 groundwork) | ST-1, ST-2, ST-3 |
| ST-5 | Tie explanation (`separated_by`) | T-1 | ST-4 |
| ST-6 | Routed answers and what-ifs by group | G-1, G-8 | ST-4, ST-5 |
| ST-7 | Conversation snapshot | G-1, G-3, G-4, K-2, K-6, W-2, S-1, S-3 | ST-4, ST-5 |
| ST-8 | News board and recaps | G-1, G-4, G-6, G-7, K-1, K-2, K-5, W-1, W-3 | ST-4, ST-5 |
| ST-9 | "My team's take" | G-1, G-5, K-3, K-4, K-7 | ST-4 |
| ST-10 | Corrected results | S-2 | ST-8 |
| ST-11 | Sport and participant wording | T-2, T-3 | ST-7, ST-8, ST-9 |
| ST-12 | Format-matrix test, eval cases, docs | all (guard) | ST-6 to ST-11 |
| ST-13 | *(optional)* Analytics page by group | page side of G-1, G-8 | ST-1, ST-4 |

X-1 and X-2 were found while writing this plan; they aren't in the gaps file:

- **X-1:** `standings.check_group_stage_complete` seeds `standings[:teams_per_group_advance]`
  from `calculate_standings(tournament, group=g)`. That includes withdrawn teams, so a
  withdrawn team can be seeded into the knockout bracket.
- **X-2:** the standings page's withdrawn badge
  (`{% if s.team.status == "withdrawn" %}` in `standings_content.html`) never shows,
  because `Team.status` is only ever `active` or `disbanded`. Withdrawal is recorded on
  the participation.

---

## ST-0: Test tournaments, one builder per format

**Why:** every later task tests against the same realistic tournaments, built with the
real scheduling and standings code, not hand-made matches.

**File:** new `core/testing_tournaments.py`. It's a plain module, not a test module, so
`tests_ai.py`, `tests_structure.py` and `tests_analytics.py` can all import it.
`core/ai/testing.py` is the precedent.

**Build:**
- `play(match, s1, s2)`:
  1. `match.refresh_from_db()`, because earlier results may have filled its slots;
  2. set the scores and `score_submitted_at`, and save;
  3. call `core.views.helpers._lock_match_score(match)`. That's the production path:
     it sets the winner, advances winners (and losers in double elimination), fills
     the third-place match, generates the consolation bracket, seeds a hybrid's
     knockout when the groups finish, and completes the tournament (setting
     `champion`) after the final. Never re-implement any of these steps in the helper.
     If `_lock_match_score` returns `False` (for example, a draw in a knockout
     match), raise, so a test can't carry on with a match that wasn't played.
- `forfeit(match, winner)`.
- Tournament builders. Each takes `organizer`, creates teams and participations, and
  calls `scheduling.generate_fixtures`:
  - `make_league(organizer, names, double=False)`
  - `make_hybrid(organizer, names, groups=2, advance=2, third_place=False)`
  - `make_knockout(organizer, names, third_place=False)`
  - `make_double_elimination(organizer, names, reset=True)`
  - `make_consolation(organizer, names)`
- Helpers:
  - `play_group_stage(t, strength)`: the stronger team (by `strength[name]`) wins
    every group match 2-0, in match-number order. `_lock_match_score` seeds the
    knockout after the last one; assert that it did;
  - `play_round(t, bracket_type, round_number, upset=())`: play every ready match in
    the round, and let the teams named in `upset` win when they're playing;
  - `withdraw(t, team)`: call the real function in `core/withdrawals.py`, not a bare
    status update.
- The standard names: `NAMES = ["Red Rovers", "Golden Boots", "Blue Jays",
  "Green Giants", "Silver Hawks", "Purple Pumas", "Orange Owls", "Black Bears"]`.
  With snake seeding into 2 groups, group A is Red Rovers, Green Giants, Silver Hawks
  and Black Bears; group B is Golden Boots, Blue Jays, Purple Pumas and Orange Owls.
  `STRENGTH` ranks them in `NAMES` order (Red Rovers strongest).
- **Named scenarios.** Later tasks refer to these by name; each is a function that
  returns the tournament:
  - `hybrid_after_groups()`: `play_group_stage` with `STRENGTH`. Final group tables:
    A = Red Rovers 9, Green Giants 6, Silver Hawks 3, Black Bears 0;
    B = Golden Boots 9, Blue Jays 6, Purple Pumas 3, Orange Owls 0. The semi-finals
    are Red Rovers v Blue Jays and Green Giants v Golden Boots.
  - `hybrid_after_one_semi()`: the same, then Red Rovers beat Blue Jays 3-1.
  - `hybrid_finished()`: the same, then Green Giants beat Golden Boots 2-1 (an
    upset), then Red Rovers beat Green Giants 2-1 in the final.
  - `knockout_after_round_1()`: 8 teams, where the higher `STRENGTH` wins every
    quarter-final.
  - `league_with_withdrawal()`: 6 teams (the first six `NAMES`). Play the first 6
    matches with team1 winning 1-0, then withdraw Blue Jays with the "void" policy.

  The probe numbers in `AI_ANALYTICS_GAPS.md` come from these same steps. If a
  builder's numbers come out different, trust the code, and update the expected
  values in the scenario docstring before writing any test on them.

**Tests** (`core/tests_structure.py`, class `TournamentBuilderTests`):
- The hybrid builder puts those teams in those groups.
- After `play_group_stage`, the knockout first round is Red Rovers v Blue Jays and
  Green Giants v Golden Boots.
- Knockout: after round 1, the round-2 matches have both teams.
- Double elimination: after winners-bracket round 1, every loser is in a
  losers-bracket match.
- Consolation: after round 1, a consolation bracket exists.

**Mutation check:** drop `advance_winner` from `play`. The knockout test fails.

**Done when:** the tests pass, and nothing outside tests imports the module.

---

## ST-1: Hybrid standings stop counting knockout matches (G-2, D-1, D-4)

**Where:**
- `core/standings.py`: `calculate_standings`, `_head_to_head_matches`.
- `core/views/auth.py`: the team dashboard, near `standings = calculate_standings(tournament)`.
- `templates/core/partials/dashboard_content.html`: the `team_standing` block.

**Build:**
1. In `calculate_standings`: when `tournament.format == "hybrid"` and `group` is falsy,
   exclude `group=""` from both the confirmed-matches queryset and the forfeits queryset.
2. In `_head_to_head_matches`: apply the same exclusion, so tiebreakers ignore knockout
   results too.
3. Team dashboard (D-4): for a hybrid, find the team's group from its participation.
   Take `team_standing` from `calculate_standings(tournament, group=that_group)`, and
   put `team_standing_group` in the context. The template shows
   `#{{ rank }} in Group {{ group }}` when `team_standing_group` is set.
   Round-robin tournaments are unchanged.

**Tests first** (`core/tests_structure.py`, `HybridStandingsTests`):
- Build the hybrid with `play_group_stage`, then play one semi-final (Red Rovers beat
  Blue Jays 3-1).
- `calculate_standings(t)`: Red Rovers have **9 points from 3 played**. Before the fix
  it's 12 from 4; watch that fail first.
- After the final as well: still 9 for Red Rovers, and Green Giants have 6.
- A head-to-head tiebreaker ignores knockout results. Two teams level on group points,
  where the one ranked lower won a knockout match against the other: the group order
  doesn't change.
- Team dashboard for a Blue Jays player: `team_standing.rank == 2`,
  `team_standing_group == "B"`, and the page shows "in Group B".
- The existing `SimulatorHybridTests` and A-13 query-count tests in
  `core/tests_analytics.py` still pass unchanged.

**Mutation check:** remove the exclusion from the confirmed queryset. The 9-point test
fails.

**Done when:** the whole suite is green. The analytics page now shows group-stage points
only for hybrids. Note this in the README's hybrid section as a behaviour change.

---

## ST-2: Withdrawn teams can't be seeded; the "W" badge shows (X-1, X-2)

**Where:**
- `core/standings.py`: `check_group_stage_complete`, the `advancing` loop.
- The views that feed `standings_content.html` and `public_standings.html`
  (`core/views/reporting.py`).

**Build:**
1. Build `withdrawn_ids` once: the tournament's participations with
   `status="withdrawn"`. In the `advancing` loop, take the top
   `teams_per_group_advance` rows **whose team isn't withdrawn**. The next team moves
   up; that's how a real competition fills the gap.
2. In the views that already set `display_label`, mark withdrawn rows with
   `row["withdrawn"] = True`. In `standings_content.html`, change the badge test to
   `{% if s.withdrawn %}`. `public_standings.html` has no badge; add the same one next
   to the team name, so both pages agree.

**Tests first:**
- Hybrid: play group rounds 1 and 2, so Red Rovers lead group A on 6 points. Withdraw
  them with the "void" policy, then play the rest of the group stage. The knockout
  bracket has Green Giants and Silver Hawks from group A, and no Red Rovers.
- The standings page shows the `badge-withdrawn` element for a withdrawn team and not
  for others. Check the round-robin page and a hybrid group table.

**Mutation check:** drop the withdrawn filter. Red Rovers are seeded again.

---

## ST-3: `structure.stage_labels` (groundwork for G-4, K-2; D-3)

**File:** new `core/structure.py` (just `structure_kind` and `stage_labels` for now).

**Rules.** `matches` is any iterable of the tournament's matches. Each label depends
only on the tournament's whole match list, so load it once inside the function.

| Match | Label |
|---|---|
| hybrid, `group != ""` | `Group {group}` |
| league (round robin or double round robin) | `Round {round_number}` |
| `bracket_type == "winners"`, knockout / hybrid / consolation | By the number of winners-bracket matches in its round, byes included: 1 = `Final`, 2 = `Semi-final`, 4 = `Quarter-final`, anything else = `Round of {2 × count}` |
| `bracket_type == "winners"`, double elimination | `Winners bracket ` + the same rule in lower case: `Winners bracket final`, `Winners bracket semi-final`, `Winners bracket round of 16` |
| `bracket_type == "losers"` | `Losers bracket round {i}`, where `i` is the 1-based position of its `round_number` among the losers bracket's distinct rounds; the last one is `Losers bracket final` |
| `bracket_type == "grand_final"` | `round_number == 1` = `Grand final`, `2` = `Grand final decider` |
| `bracket_type == "consolation"` | `Consolation ` + the winners rule, applied to the consolation matches, in lower case: `Consolation final`, `Consolation semi-final` |
| `bracket_type == "third_place"` | `Third-place match` |

**Tests first** (`StageLabelTests`, using the ST-0 builders):
- hybrid: a group match is "Group A"; the two semi-finals are "Semi-final"; the last
  match is "Final". A 3rd-place match (`third_place=True`) is "Third-place match";
- an 8-team knockout: "Quarter-final", "Semi-final", "Final";
- a 16-team knockout: round 1 is "Round of 16";
- a 6-team knockout, which is padded to 8 with byes: round 1 is still
  "Quarter-final";
- double elimination: "Winners bracket semi-final", "Losers bracket round 1",
  "Losers bracket final", "Grand final", "Grand final decider";
- consolation: "Consolation final";
- round robin: "Round 2".

Hybrid knockout rounds continue the group-round numbering (the probe showed round 4
for a semi-final); the label must not depend on `round_number`'s value.

**Mutation check:** label by `round_number` instead of by match count. The hybrid
test fails.

---

## ST-4: `structure.build_structure`: team states, phase, placings

**File:** `core/structure.py`.

**Statuses** (`TEAM_STATUSES`) and their text (`STATUS_TEXT` in
`core/ai/structure_facts.py`, see §1.2):

| status | used for | text (`detail` is filled in) |
|---|---|---|
| `in_league` | league, running | "in the league" |
| `placed` | league, finished, 4th and below | "finished {detail}" |
| `in_contention` | hybrid group stage | "still in the race to go through" |
| `through` | hybrid group stage | "through to the knockouts" |
| `out_in_groups` | hybrid | "out in the group stage" |
| `alive` | any bracket, main draw | "through to the {detail}" (the next stage, lower case) |
| `out` | any bracket | "out in the {detail}" (the stage they lost in, lower case) |
| `playing_for_third` | a semi-final loser with a third-place match to come | "playing for third place" |
| `unbeaten` | double elimination | "unbeaten, in the winners bracket" |
| `one_life_left` | double elimination | "one loss, in the losers bracket: one more and they're out" |
| `in_consolation` | consolation | "in the consolation bracket" |
| `consolation_winner` | consolation | "won the consolation bracket" |
| `champion` | any, finished | "champion" |
| `runner_up` | any, finished | "runner-up" |
| `third` | any, finished | "third" |
| `withdrawn` | any | "withdrew" |

**Rules:**

1. **Phase.**
   - `completed` status: `finished`.
   - League: `league`.
   - Hybrid: `knockout` once any `group=""` winners-bracket match has a team;
     otherwise `group_stage`.
   - Bracket: `knockout`.
2. **Withdrawn.** A team whose participation is `withdrawn` gets status `withdrawn`,
   `withdrawn=True`, and nothing else. It never takes a placing.
3. **League.**
   - While running: `in_league`.
   - When finished: the first three *non-withdrawn* rows are `champion`, `runner_up`
     and `third`; the rest are `placed` with `detail` = ordinal of rank ("5th").
4. **Hybrid, group stage.** For each group `g` with table `T` (`calculate_standings(t,
   group=g)`), and `N = min(advance, number of non-withdrawn teams in g)`:
   - `P(x)` = points; `R(x)` = x's group matches not yet final (status `upcoming`,
     `in_progress`, `pending_confirmation` or `disputed`);
     `M = max(points_per_win, points_per_draw, points_per_loss)`;
     `Max(x) = P(x) + R(x)·M`.
   - `through` if fewer than `N` other non-withdrawn teams in `g` have
     `Max(y) >= P(x)`. Ties count against x, because we don't predict tiebreakers.
   - `out_in_groups` if at least `N` other non-withdrawn teams have `P(y) > Max(x)`.
   - Otherwise `in_contention`.
   - When every group match in `g` is final: the top `N` non-withdrawn rows are
     `through`, the rest `out_in_groups`. This matches ST-2's seeding.
5. **Hybrid, knockout.** Teams not in any knockout match are `out_in_groups`. Seeded
   teams follow rule 6.
6. **Knockout main draw** (knockout, hybrid knockout, and the consolation format's
   `winners` bracket):
   - A team that lost a non-bye `winners` match is `out`, with `detail` = that match's
     stage.
   - Exception: a semi-final loser, while an undecided third-place match includes
     them, is `playing_for_third`.
   - Once the third-place match is decided, its winner is `third` and its loser is
     `out` with `detail="Third-place match"`.
   - Otherwise `alive`, with `detail` = the stage of their next winners match. If that
     match's opponent isn't known yet, still use its stage.
   - When the final is decided: its winner (or `tournament.champion`, if set) is
     `champion`, its loser `runner_up`.
7. **Double elimination.**
   - `losses` = confirmed or forfeited non-bye matches in `winners`, `losers` or
     `grand_final` that the team didn't win.
   - 0 losses: `unbeaten`. 1: `one_life_left`. 2 or more: `out`, with `detail` = the
     stage of the second loss.
   - Also `out`: the loser of the grand final when `enable_bracket_reset` is off.
   - When `tournament.champion` is set: that team is `champion`, the loser of the last
     decided grand-final match is `runner_up`, and the loser of the losers-bracket
     final is `third`.
8. **Consolation bracket.**
   - A round-1 main-draw loser, while the `consolation` bracket exists and they haven't
     lost a consolation match: `in_consolation`, with `detail` = their next
     consolation stage.
   - Lost a consolation match: `out`, with `detail` = that stage.
   - Won the consolation final: `consolation_winner`.
9. **Placings** (`Structure.placings`):
   - `champion`, `runner_up` and `third` as labels, from the statuses above;
   - `semi_finalists`: both semi-final losers, when a knockout has no third-place
     match.
   - Only keys that are known are included.
10. **Tiebreakers:** map `game_diff` to "game difference", `games_won` to "games won",
    and `head_to_head` to "head-to-head", in order.

**Tests first** (`BuildStructureTests`). Pin the probe's facts:
- **Hybrid, after the group stage:**
  - phase `knockout`;
  - Red Rovers, Green Giants, Golden Boots and Blue Jays are `alive` with detail
    "Semi-final";
  - Silver Hawks, Black Bears, Purple Pumas and Orange Owls are `out_in_groups`.
- **Hybrid, mid-group stage:** play only round 1 of the groups.
  - With 2 rounds left and M = 3, nobody is `through` yet.
  - Build a case where a team can't reach 2nd: after round 2, a team on 0 with 1
    match left, facing two rivals on 6. Assert `out_in_groups`.
  - Build a case where a team is `through` before its last match.
- **Hybrid, after one semi-final:**
  - Blue Jays (lost) are `out` / "Semi-final";
  - with `third_place=True`, they're `playing_for_third` instead.
- **Hybrid, finished:** Red Rovers `champion`, Green Giants `runner_up`,
  `placings == {"champion": "Red Rovers", "runner_up": "Green Giants", ...}`.
- **Knockout, 8 teams, after round 1:** the round-1 losers are `out` /
  "Quarter-final"; the winners are `alive` / "Semi-final".
- **Double elimination:**
  - after winners-bracket round 1, the losers are `one_life_left` and the winners
    `unbeaten`;
  - a team with 2 losses is `out`;
  - the reset case: the grand-final loser coming from the winners bracket is
    `one_life_left` when reset is on, and `out` when it's off.
- **Consolation:** round-1 losers are `in_consolation`; the consolation final's
  winner is `consolation_winner`.
- **Withdrawn:**
  - the ST-2 case: Red Rovers are `withdrawn` and appear in no placing;
  - a finished league where the withdrawn team has the most points: it isn't
    `champion`.
- **Query budget:** `build_structure` on the finished 8-team hybrid runs at most
  `groups + 4` queries. Pin the exact number you measure and add a comment saying
  why.

**Mutation checks:**
- Make ties count *for* x in the `through` rule. The "not yet through" test fails.
- Drop the withdrawn override. The withdrawn-champion test fails.

---

## ST-5: Tie explanation (T-1)

**File:** `core/structure.py`: `separated_by(tournament, rows, group=None)`.

**Rule.** For each row whose points equal the row above:
- compare the scalar tiebreakers in the tournament's order (`game_diff`, `games_won`);
  the first one that differs names the reason;
- if they're all equal and `head_to_head` is in the order, compute
  `_head_to_head_points` over the whole run of tied teams (the same data
  `rank_standings` uses). If the two teams' values differ, the reason is
  "head-to-head";
- otherwise "registration order". That's the final `-team.id` key in `rank_standings`;
  say it that way, never "team id".

Set `row["separated_by"]` to the plain words ("games won") on the lower row only.
`build_structure` calls this for `table` and every group table.

**Tests first:**
- In the ST-0 league, two teams level on 6 points and on game difference, separated by
  games won: "games won".
- Level on everything but head-to-head: "head-to-head".
- Level on everything: "registration order".
- No `separated_by` on rows that aren't tied.

**Mutation check:** always return the first tiebreaker in the order. The games-won
test fails.

---

## ST-6: Routed answers and what-ifs by group (G-1, G-8)

**Where:** `core/ai/facts.py` `build_facts` and `_fit`; `core/ai/router.py`
(`SYSTEM_PROMPT`, `build_schema`, `validate`); `core/ai/explain.py` `SYSTEM_PROMPT`.

**Build:**
1. `build_facts` builds the structure once and replaces
   `standings_top` / `results_top` with `standings_facts(structure, ...)`. Keep the
   other sections (head_to_head, form, next_match, what_if) as they are, but:
   - add `"stage"` to `next_match` (from `structure.stages`);
   - add each named team's `status` text (`team_a_status` / `team_status`).
2. **Router group** (groups kind only):
   - The schema gains `"group"`: an enum of the tournament's group letters plus
     `"none"`. Other kinds don't get the field, so their schema is unchanged.
   - `Route` gains `group: str = ""`, validated against the tournament's groups.
   - With `intent == "standings"` and a group, the facts hold only that group's table,
     with its `advance` count and statuses.
   - Add two lines to the prompt examples:
     `"who leads group b" -> {"intent":"standings", ..., "group":"B"}` and
     `"who is through to the knockouts" -> {"intent":"standings", ..., "group":"none"}`.
3. **What-if in a group:**
   - When `route.match.group` is set, re-rank only that group:
     `analytics.simulate(tournament, calculate_standings(t, group=g), [match], picks)`.
   - Add the qualification status before and after, per team, to
     `facts["what_if"]["status_changes"]` (e.g. `{"team": ..., "before":
     "still in the race to go through", "after": "through to the knockouts"}`),
     computed by applying ST-4 rule 4 to the simulated table with `R(x)` reduced by
     one for both teams.
   - A knockout what-if keeps no table. It says who goes through to which stage.
4. Update `_fit`:
   - trim each group table from the bottom, but never below `advance + 1` rows;
   - never trim the group of a team the question names;
   - bracket `knocked_out` is trimmed oldest first.
5. Add the §1.3 rule to `explain.SYSTEM_PROMPT`.

**Tests first** (`FactsBuilderTests`, `RouterTests`):
- Hybrid, standings intent, no group: the facts have `groups` with two entries of 4,
  `advance: 2`, and no top-level `standings_top`. Today they have one 8-row
  `standings_top`; watch that fail.
- Standings with `group="B"`: only group B's rows.
- Route validation: a group letter the tournament doesn't have becomes
  `unknown`, like an unknown team key.
- A what-if on a group-B match leaves group A's rows out entirely, and reports
  `status_changes` for the two teams.
- Knockout: the facts have `bracket.next_round == "Semi-final"`, with no `rank` or
  `points` keys anywhere.
- `build_schema` for a league has no `group` property (the schema didn't change).

**Mutation check:** build the group table from `calculate_standings(t)`. The group-only
test fails.

---

## ST-7: Conversation snapshot (G-1, G-3, G-4, K-2, K-6, W-2, S-1, S-3)

**Where:** `core/ai/snapshot.py` `build_snapshot`; `core/ai/conversation.py`
`SYSTEM_PROMPT`.

**Build:**
1. **League:** keep `table`, with each row's `status`, `separated_by` and
   `withdrawn` (D-2: withdrawn rows stay at their page rank).
   - `points_ahead_of_next` compares with the next row shown.
   - `points_behind_leader` compares with the first non-withdrawn row.
2. **Groups:** replace `table` with `groups`. Each has `group`, `advance` and `table`,
   and each row keeps the snapshot's per-team extras.
   - `points_behind_group_leader` (instead of `points_behind_leader`);
   - `points_behind_last_place_through`: the gap to row `N` of that group, or 0 if the
     team is inside the top `N`;
   - `group_matches_left` and `max_possible_group_points` (group matches only).
   - During the knockout phase, also add a `bracket` fragment (as in ST-6).
3. **Bracket:** `teams` rows gain `status`; drop `win_rate_pct` ranking language from
   the prompt.
4. **Every result and fixture:** add `"stage"`. Keep `round` only for leagues.
5. **Fixtures:**
   - Leave out matches whose teams are both unknown. Count them in
     `"later_matches_to_be_decided"`, grouped by stage:
     `[{"stage": "Final", "matches": 1}]`.
   - A match with one known team stays, with the unknown side shown as
     `"winner of the other semi-final"` when possible. Otherwise
     `"to be decided"`, never "TBD".
6. **Played, not yet confirmed:** matches in `pending_confirmation` or `disputed` move
   out of `fixtures` into `"awaiting_confirmation": [{"match", "stage", "team1",
   "team2", "status"}]`, with no scores. They count neither as played nor as left.
7. **Result date:** use `recap.played_on(m)`, formatted `%Y-%m-%d`; `None` when there
   isn't one. This removes the `"not schedu"` bug.
8. **Tournament block:** add `kind`, `phase`, `tiebreakers` and `placings`.
   `matches_left` counts only matches with both teams known.
9. **Prompt:** add the §1.3 rule to `conversation.SYSTEM_PROMPT`. Describe the new
   fields in its first paragraph (groups, statuses, stages).

**Tests first** (`ConversationTests` / a new `SnapshotStructureTests`):
- Hybrid after one semi-final: two groups of 4, and no row has more than 9 points.
  Silver Hawks' `points_behind_group_leader` is measured against Red Rovers, not
  against the overall leader.
- The unplayed final isn't in `fixtures` with "TBD". It's either shown with one known
  team, or counted under `later_matches_to_be_decided`.
- The semi-final result has `stage: "Semi-final"`.
- A league with Blue Jays withdrawn: ranks 1-6 all present, Blue Jays flagged, and
  `points_behind_leader` ignores them if they lead.
- A `pending_confirmation` match is in `awaiting_confirmation`, not `fixtures`, and the
  tournament's `matches_left` doesn't count it.
- An unscheduled confirmed match's date is `None`, not `"not schedu"`.
- `_fit` still returns a document for an 8-team hybrid under `MAX_SNAPSHOT_CHARS`.
  Assert the size.

**Mutation check:** put back the `[:10]` date slice. The date test fails.

---

## ST-8: News board and recaps (G-1, G-4, G-6, G-7, K-1, K-2, K-5, W-1, W-3)

**Where:** `core/ai/recap.py`: `build_recap_facts`, `final_placings`, the prompt parts,
and `write_recap`.

**Build:**
1. **Facts:** replace `standings_top` / `results_top` with
   `standings_facts(structure, rows=TOP_ROWS)`. Add `phase`.
2. Every `new_results` and `coming_up` row gets `"stage"`.
3. **Position changes (G-7):**
   - Store each team's `(group, rank)` in the recap's facts.
   - Report changes only within the same group, and only while `phase` is `league` or
     `group_stage`.
   - During the knockout phase, report `"status_changes"` instead: teams whose status
     text changed since the previous recap. For example, "through to the final" from
     "through to the semi-final", or "out in the semi-final" from "through to the
     semi-final".
   - Keep the previous recap's statuses in its `facts` so there is something to
     compare against. An old recap without them means "no changes known".
4. **Placings (G-6, K-5):** delete `recap.final_placings`, and use
   `structure.placings` in both `recap` and `team_news`. Update the import in
   `team_news.py`.
5. **Withdrawals (W-1, W-3):**
   - Add `"withdrawals": [{"team", "date"}]` for participations withdrawn since the
     previous recap. With no previous recap, include all of them.
   - Forfeit results whose loser is withdrawn get `"walkover_after_withdrawal": true`.
     Derive it from the participation, never from `match.notes`.
6. **Prompt parts per kind (K-1).** Make `RECAP_PARTS` a dict keyed by kind:
   - league: today's `"table"` text;
   - groups: `"groups"`: 1 to 3 sentences on each group's leaders and who is through
     or out, from the statuses. During the knockout phase, `"knockouts"` replaces it:
     who is still in, and the next stage;
   - bracket: `"bracket"`: who is still in and the next round. Never say "table",
     "top" or "points".

   Add the new part names to `STORY_PARTS` (in display order),
   `MAX_STORY_PART_CHARS`, and the templates that render story parts
   (`news_board_body.html`, `news_story.html` and `news_team_take_panel.html`; grep
   `story.` in `templates/core/partials/` to be sure). A story from before this
   change has only the old parts and must still render.
7. Add the §1.3 rule and: "Report a withdrawal plainly: name the team and say they
   withdrew; no puns about walkover wins."

**Tests first** (`RecapTests`, `NewsBoardTests`):
- Hybrid during the group stage: the facts have `groups` and no `standings_top`.
  `build_schema` asks for the `groups` part, not `table`.
- Hybrid, a group-B result between recaps: no group-A team appears in
  `position_changes_since_last_recap`.
- Hybrid, knockout phase: the semi-final result row has `stage: "Semi-final"`, and
  `status_changes` names Blue Jays "out in the semi-final".
- Hybrid finale: `runner_up == "Green Giants"`, and nothing in the facts ranks Golden
  Boots above Green Giants.
- Knockout: the schema has a `bracket` part and no `table` part. The prompt sent to
  `FakeOllama` has no "top of the table".
- Withdrawal: `withdrawals` names the team, and its forfeits are marked.
- An old recap job whose facts lack the new keys doesn't break the next recap.
  Build one by hand with today's shape.

**Mutation check:** compare positions across groups. The group-A test fails.

---

## ST-9: "My team's take" (G-1, G-5, K-3, K-4, K-7)

**Where:** `core/ai/team_news.py`: `build_team_facts`, `PROMPT`, `RUNNING_PARTS`,
`FINALE_PARTS`.

**Build:**
1. Add `team_facts(structure, team.pk)`: `your_status`, and for groups
   `your_group` and `your_group_table`.
2. `your_standing`, `teams_around_you` and `leader` come from the team's **group** table
   for hybrids, and from the league table for leagues. Leave withdrawn rows out of
   `teams_around_you`. Brackets get none of these.
   - `teams_in_table` becomes `teams_in_group` for hybrids, and it counts non-withdrawn
     teams.
3. `your_results` and `your_next_matches` rows get `stage`. A next match's
   `opponent_rank` is only set when the opponent is in the same group or league table.
4. **Byes (K-7):** a `bye` match involving the team is a result row
   `{"played": ..., "result": "advanced with a bye", "stage": ...}`.
5. **Parts per kind**, the same pattern as ST-8. The `table` part becomes:
   - `group` (hybrid, group stage);
   - `run` (any bracket, and a hybrid in the knockout phase): "where your run stands:
     your_status, and the next stage".

   When `your_status` is out, the running prompt says: "Their run is over (your_status):
   look back on it warmly, no hype about next matches."

**Tests first** (`TeamNewsTests`):
- Blue Jays in the hybrid group stage: `teams_around_you` holds only group-B teams, and
  `your_group == "B"`.
- Blue Jays after losing the semi-final: `your_status == "out in the semi-final"`, and
  the schema asks for `run`, not `table`.
- Knockout, Black Bears after round 1: `your_status == "out in the quarter-final"`.
- Double elimination, a round-1 winners-bracket loser: `your_status` mentions the
  losers bracket and one life.
- A team with a round-1 bye has a result row saying so.
- A league where the team below you has withdrawn: they aren't in
  `teams_around_you`.

**Mutation check:** build `teams_around_you` from `calculate_standings(t)`. The group-B
test fails.

---

## ST-10: Corrected results (S-2)

**Where:** `core/ai/recap.py`: `write_recap`, `new_results`, `news_board`.

**Build:**
1. `write_recap` stores `route["covered_scores"]`: `{match_id: [status, score1,
   score2, winner_id]}` for every covered match.
2. `new_results(tournament, previous)` also returns covered matches whose current
   `[status, score1, score2, winner_id]` differs from the stored one. Those rows get
   `"corrected": true` in the facts, and the prompt says: "A result marked corrected
   replaces an earlier score; say it was corrected."
3. `news_board` hides a headline whose stored score no longer matches. It compares the
   match against the `covered_scores` of the update the headline came from.
4. **Old recaps** without `covered_scores`: treat their matches as unchanged. Don't
   hide their headlines.

**Tests first:**
- In a round-robin league (the override view only allows league and group matches),
  publish a recap covering Aces 3-0 Bolts. Override the score to 1-3 through the real
  `override_match_result` view, as the organizer.
  - The board no longer shows the old headline.
  - The next recap includes the match with `corrected: true`.
- A recap written before this change (no `covered_scores`) still shows its headlines.

**Mutation check:** skip the score comparison in `news_board`. The first test fails.

---

## ST-11: Sport and participant wording (T-2, T-3)

**Where:** `core/ai/structure_facts.py`; the tournament blocks in all four builders;
the prompts; the "My team's take" panel templates
(`news_team_take*.html`, `news_flip.html`).

**Build:**
1. Add `SCORE_UNITS` = `{"badminton": "games", "tennis": "sets", "table_tennis":
   "games", "volleyball": "sets", "soccer": "goals", "basketball": "points",
   "cricket": "runs", "other": "points"}`. Put `score_unit` in every tournament block.
2. Rename `game_diff` in the facts only (not in the code) to `score_difference`, and
   say in each prompt: "score_difference is in tournament.score_unit".
3. Add `participant` (`tournament.participant_label.lower()`: "player" or "team") to
   every tournament block. Change the prompts to say "competitor (tournament.participant)"
   wherever they say "team" in general. `YOUR_TEAM` becomes `YOUR_SIDE`.
4. In the panel templates, "My team's take" becomes
   `My {{ tournament.participant_label|lower }}'s take` ("My player's take" reads
   oddly, so for individual mode use "My take").

**Tests first:**
- Badminton facts have `score_unit: "games"` and no `game_diff` key.
- An individual-mode tournament: the panel says "My take", and the facts'
  `participant == "player"`.

---

## ST-12: Format-matrix test, eval cases, docs (guard for all gaps)

**1. Format-matrix test** (`core/tests_ai.py`, `StructureInvariantTests`).

For each ST-0 builder (league, hybrid in the group stage, hybrid in the knockout phase,
hybrid finished, knockout, double elimination, consolation, and a league with a
withdrawal), build all four facts documents and assert:
- **Groups:** no list anywhere in the document has rows with a `rank` for teams from
  two different groups.
- **Bracket:** no `rank`, `points`, `table` or `standings_top` keys anywhere.
- **Results and fixtures:** every result, fixture and next-match row has a non-empty
  `stage`, and no team value is `"TBD"`.
- **Withdrawn teams:** never in `placings`, never in `teams_around_you` or `leader`,
  and every row about them is flagged.
- **Size:** every document fits its size limit.

Write it as one helper, `walk(facts)`, plus one loop over the scenarios. It must fail on
`de71909`: check by stashing the ST-6 to ST-9 changes, or by reading the assertion
failures before those tasks land. Note what you did in the commit message.

**2. Eval cases** (`core/ai/eval/questions.json` and `core/ai/evaluation.py`):
- `data["groups"]` is optional, e.g. `{"A": ["T1", "T2", "T3"], "B": [...]}`. When it's
  present, `evaluate` passes the group letters to `build_schema`.
- Add routing questions for group standings and "who's through".
- Explanation cases gain optional `must_mention_any: [...]` and
  `must_not_contain: [...]`, checked case-insensitively. Report them as
  `WORDING` lines in `summary_lines`, and under `wording_failed` in `as_dict`.
- Add at least four cases:
  - a hybrid group table where the question is "who's top?". `must_mention_any`: both
    group leaders; `must_not_contain`: "nail-biter".
  - a knockout after the quarter-finals. `must_not_contain`: "top of the table",
    "points".
  - double elimination with a one-loss team. `must_not_contain`: "knocked out",
    "eliminated".
  - a withdrawn team: `must_not_contain` their name next to "runner-up".
- Tests go in `EvaluationTests`, using `FakeOllama`.

**3. Docs:**
- In `AI_ANALYTICS_GAPS.md`, add a line to each gap: `**Status:** fixed in ST-n
  (<commit>)`, or `deferred (reason)`.
- In the README's "AI analytics" section: one short paragraph on how the AI handles
  groups, brackets and withdrawals, and the ST-1 behaviour change for hybrid points.
- In `AI_ANALYTICS_PLAN.md`: link to this plan from "Done when".

**4. Owner checks (on the server, not CI):** ask the owner to:
- run `python manage.py ai_eval --json eval.json` and send the WORDING lines;
- read one news board, one "My team's take" and one conversational answer on a real
  hybrid tournament.

---

## ST-13 (optional, D-5): Analytics page by group

The analytics page for a hybrid:
- **Points Overview:** one bar block per group, from
  `calculate_standings(t, group=g)`.
- **What-if simulator:** re-ranks the picked match's group only, and shows a status
  column (through / out / in contention) from `structure`.
- **Knockout phase:** a line "Knockout: next round Semi-final", with the teams still
  in.

Keep the A-13 query-count test green. The number of groups is fixed per tournament,
so per-group queries don't break "flat as the tournament grows". Test it with a hybrid
of 8 and of 16 teams.

---

## Done when

- Every gap in `AI_ANALYTICS_GAPS.md` has a status line: fixed, or deferred with a
  reason.
- X-1 and X-2 are fixed.
- The suite is green on SQLite and PostgreSQL CI; `ruff check .` is clean.
- Every task's mutation check was run, and its commit message says so.
- The format-matrix test (ST-12) passes, and it fails when any builder goes back to
  `calculate_standings(tournament)` for a hybrid.
- The owner has run `ai_eval` on the server, and the results are recorded in §2 below
  the decisions.
