# Analytics Plan

Fixes and improvements for the analytics page (`/analytics/`), from a review
of `core/views/reporting.py` (`analytics_view`) and
`templates/core/analytics.html`.

Every bug listed here was **reproduced** against a test database before this
plan was written, not inferred from reading. The probe that did it is
described under each task so it can be turned straight into the regression
test.

---

## 0. Ground rules

Same as `FOLLOWUP_PLAN.md` §0, which in turn defers to `REMEDIATION_PLAN.md`
§0. The ones that matter most:

1. **One task, one commit.** `<type>(<area>): <summary> [<TASK-ID>]`.
2. **Regression test first.** Write it, watch it fail against the current
   code, then fix. A test that passes before the fix proves nothing — the
   dispute-throttle test in F-4 is the cautionary example.
3. **Every task ends green** on both backends:
   `python manage.py test` (SQLite) and the same with the `DJANGO_DB_*`
   PostgreSQL env vars. Baseline: **391 tests, 0 failures** (3 skipped on
   SQLite, 0 on PostgreSQL). `ruff check .` clean.
4. **Push, then confirm on real CI** (`workflow_dispatch` on the branch —
   CI doesn't run on feature-branch pushes).
5. **Stop at `DECISION REQUIRED`.** Ask the owner; do the rest meanwhile.
6. **Line numbers are as of `b8c0b96`.** Locate code by the quoted text.

Branch: `claude/code-docs-review-26pjjx`.

### Execution order

A-1 → A-2 → A-3 → A-4 → A-5 → A-6 → A-9 → A-11 → A-7 → A-8 → A-10 → A-12 → A-13

Bugs first (A-1 to A-6: one security, three wrong-output, two rendering),
then the cheap cleanups, then the refactors. A-12 (HTMX widgets) rewrites
the template's forms, so it goes after everything else that touches
`analytics.html` to avoid resolving the same conflicts repeatedly.

### Test file

New `core/tests_analytics.py` for everything below unless a task names
another file. Existing coverage to keep passing:
`core/tests/test_standings_analytics.py` (2 tests),
`core/tests_authorization.py` (analytics/audit-log access, 3 tests).

---

## A-1 — Other organizers can read a tournament's analytics and audit log

**Severity:** high (data exposure) · **Files:** `core/views/reporting.py`

**Problem.** `analytics_view` gates on `_is_organizer(request.user)`
(`reporting.py:148`) and shows "Recent Activity" to any organizer (`:224`).
`_get_tournament` lets any organizer select any tournament with
`?tournament=<id>`. `_can_manage_tournament`'s own docstring says organizers
"are independent parties, not a mutually trusted pool", and the rest of the
app already enforces ownership for writes — but reads here don't.

**Reproduced:** organizer B opened `/analytics/?tournament=<A's pk>` and
the page contained the text of an audit entry organizer A had created.

`audit_log_view` (`reporting.py:486`) has the identical gap: `_is_organizer`
only, then filters by whichever tournament `_get_tournament` returns.

**Fix.**

1. `analytics_view`: replace the access check with
   ```python
   can_manage = _can_manage_tournament(request.user, tournament)
   if not can_manage and not _is_user_enrolled_in_tournament(request.user, tournament):
       messages.error(request, "You do not have access to that tournament.")
       return redirect("dashboard")
   ```
   and gate `recent_logs` on `can_manage` instead of `_is_organizer`.
   Pass `can_manage` into the context and use it in the template's
   "Recent Activity" `{% if %}` (currently `user_is_organizer`, a global
   flag from the context processor that says nothing about *this*
   tournament).
2. `audit_log_view`: require `_can_manage_tournament(request.user, tournament)`
   for tournament-scoped rows.

**DECISION REQUIRED — global audit rows.** `audit_log_view` also shows rows
with `tournament IS NULL` (logins, account registrations, user management)
to every organizer. Those are site-wide events about *other people's*
accounts. Recommended: site admins only (`_is_site_admin`). Alternative:
leave as-is. Ask before changing this part; do step 1 and the
tournament-scoped half of step 2 regardless.

**Tests.**
- Non-owner organizer → analytics for another's tournament redirects, and
  the audit detail text is not in any response.
- Owner organizer → sees Recent Activity.
- Site admin → sees Recent Activity for any tournament.
- Enrolled participant → sees analytics, no Recent Activity (already covered
  by `tests_authorization.py`; keep it passing).
- Non-owner organizer who is *also enrolled* as a player → sees analytics,
  no Recent Activity.
- `audit_log_view` equivalents.
- Legacy tournament with `created_by=None` → any verified organizer still
  manages it (existing `_can_manage_tournament` rule; pin it).

**Done when:** no response to a non-manager contains another tournament's
audit data, and the decision above is recorded in this file.

---

## A-2 — Team Performance counts draws as losses and ranks by win rate alone

**Severity:** medium (wrong numbers) · **File:** `core/views/reporting.py`
(`team_stats` block, `:175-202`), `templates/core/analytics.html`

**Problem.**
- `"losses": played - wins` (`:198`) — every draw becomes a loss.
- Sorted by `win_rate` only (`:202`) — a 1-0 team outranks a 9-1 team.
- Wins are re-derived from scores in a loop that duplicates, and subtly
  differs from, `calculate_standings`.

**Reproduced:** a team whose only match was a 2–2 draw showed
`P1 W0 L1 0.0%`; a team at 9W-1D-1L showed `L2`.

**Fix.** Build `team_stats` from `calculate_standings(tournament)` rows,
which already count `played / wins / draws / losses` correctly for scored
confirmed matches and forfeits, then filter to active participations:

```python
rows = [r for r in calculate_standings(tournament) if r["team"].pk in active_ids]
team_stats = [{
    "team": r["team"],
    "display_label": _team_display_label(tournament, r["team"]),
    "played": r["played"], "wins": r["wins"],
    "draws": r["draws"], "losses": r["losses"],
    "win_rate": round(r["wins"] / r["played"] * 100, 1) if r["played"] else 0,
} for r in rows]
team_stats.sort(key=lambda s: (-s["wins"], -s["win_rate"], s["losses"], s["display_label"].lower()))
```

For round-robin-style formats the standings order is the better default —
use it (i.e. skip the re-sort) when `tournament.format in ("round_robin",
"double_round_robin", "hybrid")`. The explicit sort is for bracket formats,
where points are meaningless.

Add a **Draws** column to the template; hide it when every row has
`draws == 0`.

`calculate_standings` includes withdrawn teams; the current table shows
active only. Keep active-only.

**Tests.** A draw is a draw (W0 D1 L0); a 9-1 team ranks above a 1-0 team
in a knockout tournament; round-robin order matches `calculate_standings`.

---

## A-3 — Internal team names leak in individual-registration tournaments

**Severity:** medium (wrong/confusing output, internal identifiers
exposed) · **Files:** `core/views/reporting.py`, template

**Problem.** `context["standings"] = calculate_standings(tournament)`
(`:233`) — those rows have no `display_label`, so the Points Overview's
`{{ s.display_label|default:s.team.name }}` shows the shadow team's name.

**Reproduced:** an individual-mode tournament with players "Player A" and
"Player B" rendered `__tm_shadow_x` and `__tm_shadow_y` on the page.

**Fix.** Set `row["display_label"] = _team_display_label(tournament,
row["team"])` on every standings row before it enters the context (the
dashboard already does this; see `auth.py` near the "Team Analytics"
comment). Combine with A-11's single `calculate_standings` call.

Also: the Team Performance link goes to `team_detail` for the shadow team,
which non-organizers can't open (`teams.py`: `if team.is_internal and not
_is_organizer`). Render the label without a link when `team.is_internal`.

**Tests.** Individual-mode tournament: no `__tm_shadow_` substring anywhere
in the response (`assertNotContains`), display names present, and no
`team_detail` link for an internal team.

---

## A-4 — The what-if simulator applies knockout matches to group standings

**Severity:** medium (wrong projection) · **File:** `core/views/reporting.py`
(simulator block, `:409-460`), template

**Problem.** The simulator takes any `status="upcoming"` match with both
teams set (`:415`) and offers **Draw** on every one. In a hybrid tournament
that includes knockout-stage matches: they award group points, and a draw —
impossible in a knockout match — gives both teams `points_per_draw`.

**Reproduced:** a hybrid knockout match (`group=""`) appeared in the
simulator, and picking "draw" gave both teams +1.

**Fix.**
- For `hybrid`, restrict the candidate matches to the group stage:
  `.exclude(group="")`. (Group-stage matches carry a group letter; knockout
  matches have `group=""` — the same rule the dashboard's bracket-final
  query already relies on.)
- Only offer **Draw** where a draw is legal: the match is in a group /
  round-robin stage. After the filter above, that's every simulator match,
  but make it explicit per match (`m.draw_allowed`) so A-8 can't regress it.
- Server-side, ignore a submitted `draw` for a match where
  `draw_allowed` is false, rather than trusting the dropdown.

**Tests.** Hybrid with one group match and one knockout match: only the
group match is offered; a forged `sim_<knockout pk>=draw` changes nothing.

---

## A-5 — The page's JavaScript is emitted twice

**Severity:** low (double work, double event handlers) ·
**File:** `templates/core/analytics.html`

**Problem.** `{% block extra_js %}` is nested inside `{% block content %}`
(template lines 369–447). Django renders a nested block both where it sits
*and* in the parent's `{% block extra_js %}` slot (`base.html:176`).

**Reproduced:** `var data = ` occurs twice in the rendered page; the scroll
listeners are attached twice and the density chart is drawn twice.

**Fix.** Close `{% block content %}` before opening `{% block extra_js %}`
so the two are siblings. Check no other template has the same nesting:
`grep -n "block extra_js" templates -r` and inspect each.

**Test.** `self.assertEqual(response.content.decode().count("var data = "), 1)`
(or, after A-11, count the `json_script` element id).

---

## A-6 — Dark theme: hard-coded light colours

**Severity:** low (visual) · **Files:** `templates/core/analytics.html`,
`static/css/style.css`

**Problem.** Five inline colours: bar tracks `#e2e8f0` (×3 bars plus the
JS density chart) and W/L/D form pills (`#d1fae5/#065f46`,
`#fecaca/#991b1b`, `#e2e8f0/#475569`). `#e2e8f0` is exactly the light
theme's `--border`; dark sets `--border: #334155`, so the tracks stay pale
on a dark card.

**Fix.** Add to `style.css`:
```css
.bar-track { background: var(--border); border-radius: 4px; }
.form-pill { width:28px; height:28px; border-radius:50%; display:inline-flex;
             align-items:center; justify-content:center; font-size:.72rem; font-weight:700; }
.form-pill.is-win  { background:#d1fae5; color:#065f46; }
.form-pill.is-loss { background:#fecaca; color:#991b1b; }
.form-pill.is-draw { background:var(--border); color:var(--text-muted); }
html[data-theme="dark"] .form-pill.is-win  { background:#064e3b; color:#a7f3d0; }
html[data-theme="dark"] .form-pill.is-loss { background:#7f1d1d; color:#fecaca; }
```
and use the classes in the template and the density-chart JS. Also give
each pill an accessible name (`aria-label="Win"` etc. — colour alone
currently carries the meaning; the letter helps but the `title` is the
only full description).

Out of scope, noted: the `.badge-*` classes have the same light-only
problem app-wide. That's a separate, larger theming task.

**Verify.** Render once in each theme (`run` skill / browser) and
eyeball it; there's no meaningful unit test for colour. Assert in a test
that the strings `#e2e8f0`, `#d1fae5`, `#fecaca` no longer appear in the
analytics response.

---

## A-7 — One definition of a match result across the page

**Severity:** low · **Files:** `core/standings.py`, `core/views/reporting.py`

**Problem.** After A-2, Team Performance uses the standings rules (a
confirmed match counts only when it has scores; forfeits by `winner`).
Head-to-head, Rolling Form and the Prep Sheet use `winner_id` on any
`confirmed`/`forfeited` match. They diverge on confirmed matches with no
scores.

**First, check whether that case exists.** Find every code path that sets
`status="confirmed"` (score submission, organizer override, no-show
finalization — `_finalize_no_show_match`) and whether each also sets
scores. **If no path can produce a confirmed match without scores, stop,
record that here, and close this task** — the definitions are equivalent
in practice and a refactor buys nothing.

**If it can:** add `match_result_for(team, match) -> "W" | "L" | "D" | None`
to `core/standings.py`, implementing exactly the rule `calculate_standings`
uses (and have `calculate_standings` use it too), then use it in the three
widgets.

**Tests.** A confirmed-without-scores match is treated identically by the
standings table and the form widget.

---

## A-8 — The simulator ignores the tournament's tiebreakers

**Severity:** low · **Files:** `core/standings.py`, `core/views/reporting.py`

**Problem.** Simulated standings sort by
`(points, game_diff, games_won, wins)` (`:455`). The real table uses
`tournament.get_tiebreaker_order()` plus the head-to-head pass added in
T-4.5. A simulated table can therefore rank two tied teams in a different
order than the real table would with the same points.

**Fix.** Extract the sorting tail of `calculate_standings` (from
`tiebreakers = tournament.get_tiebreaker_order()` through the rank
assignment) into `rank_standings(tournament, rows, group=None)`; call it
from both `calculate_standings` and the simulator after applying the
projected points. Head-to-head can only use real results (a projection has
no scores) — state that in the function's docstring.

Also: the simulator silently caps at 8 matches (`[:8]`). Show "Showing the
next 8 of N upcoming matches" when truncated.

**Tests.** Two teams tied on projected points order the same way
`calculate_standings` would order them given those points; existing
`tests_standings.py` must still pass unchanged (the extraction must not
alter real standings).

---

## A-9 — "Court Utilization" measures completion, not utilization

**Severity:** low (misleading label) · **Files:** view, template

`utilization = confirmed / total` (`:173`) is the share of a court's
scheduled matches that have been played. Rename the card to **Court
Progress**, the column to **Completed**, and the context key from
`utilization` to `completion_pct`. No behaviour change; update any test
that reads the key.

---

## A-10 — Schedule density grows without bound

**Severity:** low · **Files:** view, template

One bar per calendar day. A months-long league renders hundreds of rows.

**Fix.** If the scheduled span exceeds 45 days, aggregate by ISO week
(label `Week of Mon DD`) instead of by day, and say so in the card header.
Keep per-day otherwise.

**Tests.** 10 days → 10 daily buckets; 90 days → weekly buckets.

---

## A-11 — Cleanups

**Severity:** trivial · **Files:** view, template. One commit.

- `calculate_standings` runs twice per request (`:233` and `:423`). Compute
  once, reuse (A-2, A-3 and A-8 all read it).
- Dead template code: `{% widthratio s.points standings.0.points 100 as pct %}`
  (line 306) assigns `pct` and never uses it.
- Points Overview divides by `standings.0.points`; guard the all-zero case
  so bars render at 0 rather than relying on `widthratio`'s behaviour.
- `var data = {{ schedule_density|safe }};` → pass the dict to
  `{{ schedule_density|json_script:"schedule-density-data" }}` and
  `JSON.parse(document.getElementById(...).textContent)`. The current data
  is only dates and ints, so this isn't exploitable today; it removes the
  `|safe` so it can't become so.
- `teams.filter(participations__tournament=..., participations__status="active")`
  is built three times; build `active_teams` once.

---

## A-12 — Widget forms reset each other; use HTMX partials

**Severity:** medium (UX) · **Files:** view, template, new partials

**Problem.** Head-to-head, Rolling Form, Prep Sheet and Simulator are four
separate `<form method="get">`s. Submitting one drops the others' query
parameters, so choosing a head-to-head pair resets your form-trend team
and every simulator pick. The ~60-line scroll-restore script exists only to
paper over the full-page reload.

**Fix, in two commits.**

1. **Preserve state (small, independently useful).** Each widget form
   carries the *other* widgets' current values as hidden inputs, so a
   submit round-trips everything. Test: submit the H2H form with
   `form_team`/`sim_*` already in the URL; they survive.
2. **HTMX per card.** Split each widget into
   `templates/core/partials/analytics_<widget>.html`. Forms get
   `hx-get="{% url 'analytics' %}" hx-target="#analytics-<widget>"
   hx-swap="outerHTML" hx-push-url="true" hx-include="#analytics-state"`
   (a hidden block holding every widget's current params, so the pushed URL
   stays complete and shareable). The view returns just the requested
   card's partial when the request is HTMX and carries a `widget=<name>`
   param — follow the existing `_render_refreshable_page` / `partial=1`
   convention in `helpers.py` rather than inventing a new one. Then delete
   the scroll-restore script.

   Non-JS clients must still work: the forms keep `method="get"` and
   `action`, so without HTMX they do a normal full-page submit (with the
   hidden-state inputs from step 1).

**Tests.** HTMX request for `widget=h2h` returns only that card's markup
(`assertTemplateUsed` the partial, `assertNotContains` another card's
header); non-HTMX request returns the full page; pushed URL includes all
widget params.

---

## A-13 — Per-team query fan-out

**Severity:** low — measured, not urgent · **File:** view

**Measured:** 45 queries at 4 teams / 6 matches, 53 at 8 / 28, 69 at 16 /
120 — roughly two extra queries per team and per court, flat in match
count. Fine at current sizes.

After A-2 and A-11, most per-team queries are gone (Team Performance comes
from one `calculate_standings`). What's left: per-court counts (`:168-174`)
and per-withdrawn-team lookups (`:213-221`). Replace with one
`.values("court").annotate(total=Count("id"), done=Count("id",
filter=Q(status="confirmed")))` and a `prefetch_related` on withdrawn
participations.

**Test.** `assertNumQueries` with a ceiling that doesn't grow between an
8-team and a 16-team tournament (assert equality of the two counts, not a
magic number).

---

## Done when

- A-1 to A-13 are each committed separately (A-12 as two), with their tests.
- The A-1 decision is recorded below.
- 391 + new tests pass on both backends; ruff clean; CI green on the final
  push.
- Coverage for `core/views/reporting.py` re-measured and recorded below
  (baseline 61% at F-2).

### Decisions

| Task | Question | Answer | Date |
|---|---|---|---|
| A-1 | Global (`tournament IS NULL`) audit rows: who sees them? | _pending_ | |

### Results

| Measure | Before | After |
|---|---|---|
| Tests | 391 | |
| `reporting.py` coverage | 61% | |
| Analytics queries, 16 teams | 69 | |
