# Follow-up Plan

Successor to `REMEDIATION_PLAN.md`, which is complete (all 34 tasks ticked,
merged to `main` at `65dbb9a`, CI green on both SQLite and PostgreSQL).

That plan fixed defects found in a code review. This one covers what the review
never looked at: the absence of tooling, one monolith the views split left
behind, and a security gap that was explicitly deferred as out of scope.

Like its predecessor, this document is written to be executed by an AI coding
agent, task by task. Every task states what to change, how to prove it worked,
and what "done" means.

---

## 0. Ground rules

The ground rules in `REMEDIATION_PLAN.md` §0 still apply in full. Read them.
The ones that matter most here:

1. **One task, one commit.** Message format: `<type>(<area>): <summary> [<TASK-ID>]`.
2. **Every task ends green.** Run `python manage.py test` before committing.
   The baseline is **371 tests, 0 failures** (3 skipped on SQLite, 0 skipped on
   PostgreSQL). A task that reduces the passing count is not done.
3. **Do not refactor opportunistically.** Several tasks below are deliberately
   narrow because a wide diff would bury the part that matters.
4. **Do not invent line numbers.** References here were accurate at `65dbb9a`.
   Locate code by searching for the quoted text.
5. **Stop at `DECISION REQUIRED`.** Those need the owner's call.

Work on branch `claude/code-docs-review-26pjjx`. Do not push to `main`.

### Reporting format after each task

```
[TASK-ID] <DONE | BLOCKED | SKIPPED>
Files changed: <list>
Test added: <test path::name>
Verification: <command> -> <result>
```

### Suggested execution order

F-1 → **F-7** → F-2 → F-3 → F-4 → F-5 → F-6.

F-7 is pulled forward because it edits `.github/workflows/ci.yml`, the same file
F-1 touches; doing them back to back avoids a conflict. F-2 (coverage) is worth
having before F-3 and F-4 because it tells you which parts of the code the
existing 371 tests never execute.

### Baseline measurements

All figures below were measured at `65dbb9a` and should be re-measured, not
trusted, if significant time has passed.

| Measure | Value |
|---|---|
| Tests | 371 (138 of them in `core/tests.py`) |
| Suite runtime | ~7.6s SQLite, ~20.7s PostgreSQL |
| Routes in `core/urls.py` | 103 |
| `POST` handlers in `core/views/` | 77 across 7 modules |
| Throttled endpoints | 1 (`login_view`) |
| `ruff check` findings (default rules) | 266, of which 166 come from one repeated mistake — see F-1 |
| Lines over 88 columns | 850 |
| Tab-indented lines | 3,527 (all but 10 of them in `core/tests.py`) |
| Largest file | `core/tests.py`, 4,161 lines |

---

## F-1 — A lint gate in CI

**Severity:** medium · **Files:** `pyproject.toml` (new),
`requirements-dev.txt` (new), `.github/workflows/ci.yml`, plus the fixes

**Problem.** Nothing enforces anything. CI runs Django's system checks and the
test suite; neither notices an import that is never used or a variable assigned
and thrown away. Measured at `65dbb9a` with ruff's default rule set:

| Rule | Meaning | Count |
|---|---|---:|
| E402 | Module-level import not at top of file | **166** |
| F401 | Imported but unused | 49 |
| F541 | f-string with no placeholders | 25 |
| F841 | Local assigned but never used | 22 |
| F811 | Redefinition of an unused name | 3 |
| E401 | Multiple imports on one line | 1 |
| — | **Undefined names (F821)** | **0** |

Zero undefined names is the important number: nothing is broken. This is debt,
not breakage.

**The 166 E402s are one mistake, repeated nine times.** The views split left a
stray second docstring in every module it created:

```python
"""Sign-in, registration, profile and the dashboard."""
"""Core views for tournament management."""      # <- copied from the old views.py
from django.contrib import messages
```

The second string literal is not a docstring — it is an expression statement,
which makes every import below it "not at top of file". All nine modules under
`core/views/` carry it (`__init__.py` does not; in `helpers.py` it sits at line
7, below a multi-line docstring, not line 2). Deleting those nine lines takes
the total from **266 to 129**, verified.

Of the 29 E402s that remain, **27 are legitimate**: the scripts in `scripts/`
must call `django.setup()` before importing any model, so their imports cannot
be at the top. Give them a per-file ignore rather than contorting the code. The
other two are in `core/tests.py` (lines 3538–3539) and should be moved up.

The remaining ~100 findings are an artefact of the same split — one import block
copied into nine modules. `core/views/admin_tools.py` imports `OrganizerProfile`
at module level and then imports it again inside two separate functions;
`core/views/helpers.py` does it once more.

**Do this.**

1. Add `requirements-dev.txt`:

   ```
   # Developer tooling. Not needed to run the app.
   #
   #     pip install -r requirements.txt -r requirements-dev.txt
   ruff>=0.6,<1
   coverage>=7,<8
   ```

2. Add `pyproject.toml`. Start with ruff's **default** rule set and nothing
   more:

   ```toml
   [tool.ruff]
   target-version = "py311"
   extend-exclude = ["core/migrations", "staticfiles"]

   [tool.ruff.lint]
   # Ruff's default: pycodestyle errors that matter, plus all of pyflakes.
   # Deliberately excludes E501 (line length) — see the note below.
   select = ["E4", "E7", "E9", "F"]

   [tool.ruff.lint.per-file-ignores]
   # The views package re-exports its submodules' public names so that
   # `from core.views import x` keeps working after the split. The star
   # imports are the mechanism, not an accident.
   "core/views/__init__.py" = ["F403", "F405"]
   # Every script calls django.setup() before it can import a model, so its
   # imports genuinely cannot sit at the top of the file.
   "scripts/*.py" = ["E402"]
   ```

   Verified with ruff 0.15.8: this config parses and selects the rules above.

   **Do not enable `E501`.** 850 lines exceed 88 columns; enforcing length
   would produce a reformatting diff far larger than every real fix combined,
   and would bury them. Same reasoning for `I` (import sorting) and for
   `ruff format` — both are reasonable later, each as its own commit, once this
   gate is green and quiet.

   **Do not enable `W191` (tab indentation)** either. `core/tests.py` is
   tab-indented throughout; that file is converted in F-3, and enabling the rule
   before then makes F-1 unreviewable.

3. Delete the nine stray `"""Core views for tournament management."""`
   lines first, as their own hunk — that is 166 of the 266 findings and it is
   the one fix that is pure deletion. The command below lists exactly the nine:

   ```bash
   grep -ln '^"""Core views for tournament management\."""$' core/views/*.py
   ```

   Do not delete the *first* docstring in any module; those are the real ones
   the split wrote.

4. Fix the remaining ~129. They fall into three mechanical groups:
   - **Unused imports** — delete. Check first that the name is not re-exported
     via a module's `__all__`; `core/views/helpers.py` declares one explicitly.
   - **Unused locals** — usually a result that was computed and then ignored.
     Read each one. A few may be a genuine bug (something was meant to be
     asserted or returned); if so, say which in the commit body.
   - **f-strings with no placeholders** — drop the `f` prefix. All 25 are
     cosmetic; the majority are `print(f'...')` in `scripts/`. If F-6 deletes
     the script, the finding goes with it — do F-1 first anyway so the count is
     honest.

5. Add a CI step to the `test` job, **before** the Django system checks so a
   lint failure reports fast:

   ```yaml
   - name: Install dependencies
     run: |
       python -m pip install --upgrade pip
       pip install -r requirements.txt -r requirements-dev.txt

   - name: Lint
     run: ruff check .
   ```

   Update that job's `cache-dependency-path` to include `requirements-dev.txt`.

**Verify.**

```bash
ruff check .                  # must exit 0
python manage.py test         # must still be 371 passing
```

**Done when:** `ruff check .` is clean, CI runs it, and the suite is unchanged
at 371 passing. Removing an unused import must not change behaviour — if the
test count moves in either direction, something was load-bearing.

**Expected checkpoint:** 266 findings before any change; 129 after the nine
docstring deletions; 0 when the task is done.

---

## F-2 — Measure coverage

**Severity:** medium · **Files:** `pyproject.toml`, `.github/workflows/ci.yml`

**Problem.** 371 tests pass and nobody knows what they miss. `.coverage` and
`htmlcov/` are already in `.gitignore`, so the intent existed; the measurement
never happened. This task is diagnostic — its output decides how urgent F-3 and
F-4 really are.

**Do this.**

1. Add coverage config to `pyproject.toml`:

   ```toml
   [tool.coverage.run]
   source = ["core", "tournament_manager"]
   omit = [
       "*/migrations/*",
       "*/tests*.py",
       "core/tests/*",
       "manage.py",
       "*/wsgi.py",
       "*/asgi.py",
   ]
   branch = true

   [tool.coverage.report]
   show_missing = true
   skip_covered = true
   ```

2. Measure, on both backends — the PostgreSQL run executes three concurrency
   tests that SQLite skips:

   ```bash
   coverage run manage.py test && coverage report
   ```

3. **Record the result in this file**, as a table of the ten least-covered
   modules with their percentages, under a new heading `### F-2 baseline`.
   That table is the actual deliverable. A number in a CI log that nobody reads
   is not.

4. Add a CI step that reports but **does not fail**:

   ```yaml
   - name: Coverage
     run: |
       coverage run manage.py test
       coverage report
   ```

   Once the baseline is known and recorded, a second commit may add
   `--fail-under=<baseline minus 2>`. Do not pick a round aspirational number —
   a threshold above where the code actually sits is a broken build on day one.

**Verify.** `coverage report` produces a table; CI shows it; the suite still
passes.

**Done when:** the baseline table is committed in this document and CI prints
coverage on every run.

### F-2 baseline

Measured at `9dfa11d` with `coverage run manage.py test`, combined across both
backends (`coverage run` on SQLite, then `coverage run -a` on PostgreSQL with
the same env vars CI uses) so the 3 concurrency tests PostgreSQL alone runs are
included. **Combining made no difference to the numbers below** — those tests
exercise `_claim_participant_slot`, which the non-concurrency claim tests
(`test_a_free_slot_is_claimable`, `test_a_full_tournament_is_not_claimable`)
already reach identically; the branch coverage they'd add is inside a real
race, not reachable from either backend's tests in a straight line. Both runs:
371 tests, 0 failures.

**Overall: 67%** (6,676 statements, 1,920 missed; branch coverage included).

The ten least-covered modules:

| Module | Coverage | Note |
|---|---:|---|
| `core/management/commands/backfill_organizer_and_team_assignment.py` | 0% | One-time data backfill, run once against production data and never again — not exercised by the request-response suite. Same for the row below. |
| `core/management/commands/normalize_individual_registrations.py` | 0% | One-time legacy-data normalization command. |
| `core/views/teams.py` | 45% | Largest views module (616 statements) and the worst-covered live code path. |
| `core/views/tournaments.py` | 57% | Second-largest module (688 statements); setup/lifecycle transitions. |
| `core/management/commands/audit_participant_integrity.py` | 58% | Diagnostic command; exercised only at the entry points the existing tests happen to hit. |
| `core/views/test_maker.py` | 58% | Development-only data generator, disabled outside `DEBUG` — lower priority than user-facing code. |
| `core/views/reporting.py` | 61% | Standings, analytics, backups, notifications, search, public pages — the broadest single module. |
| `core/views/matches.py` | 63% | Fixtures, scores, disputes, reschedules, no-shows. |
| `core/views/admin_tools.py` | 67% | Site-admin settings, user management, impersonation. |
| `core/views/registration.py` | 68% | Joining tournaments, registration review, participant seeding. |

**Reading this table.** The `core/views/` split (F-1's predecessor task)
produced nine modules of very different sizes and very different coverage;
the three biggest — `teams.py`, `tournaments.py`, `reporting.py` — are also
among the worst-covered, which is the opposite of what you'd want. That's
the strongest argument in this plan for F-3 (split `core/tests.py` so gaps
like this are easier to see and assign) over doing F-4/F-5/F-6 first.

The two 0%-covered management commands are not a coverage gap in the usual
sense — they're one-shot scripts, not code a user request ever reaches — so
raising their percentage isn't useful work. If a `--fail-under` threshold is
added later (see the CI step above), omit them via `pyproject.toml`'s
`[tool.coverage.report] exclude_also` or accept the file-level average they
drag down; don't write tests for a backfill script just to move a number.

---

## F-3 — Split `core/tests.py`

**Severity:** low (maintainability) · **File:** `core/tests.py` → `core/tests/`

**Problem.** At 4,161 lines it is now the largest file in the repository —
larger per-module than anything the views split produced. One class holds more
than half of it:

| Class | Lines | Span | Tests |
|---|---:|---|---:|
| `UXAndLogicRegressionTests` | **2,117** | 46–2162 | **80** |
| `WithdrawalPolicyTests` | 464 | 2362–2825 | 15 |
| `TournamentCompletionTests` | 370 | 3641–4010 | 14 |
| `TournamentLifecycleTests` | 320 | 3222–3541 | 4 |
| `EnrollmentRefactorRegressionTests` | 300 | 2826–3125 | 8 |
| `DoubleEliminationBracketTests` | 199 | 2163–2361 | 5 |
| `ScoreDisputeWindowTests` | 152 | 4011–4162 | 4 |
| `AdditionalFormatSupportTests` | 99 | 3542–3640 | 4 |
| `PendingTeamRegistrationTests` | 96 | 3126–3221 | 4 |

`UXAndLogicRegressionTests` is a grab-bag by name and by content. It is where
tests went when nobody decided where they belonged.

**Do this in two commits.** The order is the whole point.

**Commit 1 — whitespace only.** `core/tests.py` is tab-indented (3,527 lines);
every other file in the project uses spaces. Convert it, and nothing else:

```bash
expand -i -t 4 core/tests.py > /tmp/tests.py && mv /tmp/tests.py core/tests.py
```

`-i` converts leading whitespace only, so a tab inside a string literal is left
alone. Prove the conversion changed nothing but whitespace by comparing the
parse trees before and after:

```bash
git show HEAD:core/tests.py > /tmp/before.py
python - <<'EOF'
import ast
before = ast.dump(ast.parse(open("/tmp/before.py").read()))
after = ast.dump(ast.parse(open("core/tests.py").read()))
print("identical AST" if before == after else "DIFFERENT - stop and investigate")
EOF
```

This was verified at `65dbb9a`: the ASTs match exactly.

Commit as `style(tests): convert core/tests.py from tabs to spaces`. Reviewers
can skip this diff entirely, which is why it must not contain anything else.

**Commit 2 — the split.** Convert to a package, mirroring what was done for
`core/views/`:

```
core/tests/
    __init__.py          # empty; Django's discovery finds test*.py beneath it
    test_ux_regressions.py
    test_withdrawals.py
    test_completion.py
    test_lifecycle.py
    test_enrollment.py
    test_double_elimination.py
    test_disputes.py
    test_formats.py
    test_pending_registration.py
```

Take `UXAndLogicRegressionTests` apart properly rather than moving 2,117 lines
into one new file — that would relocate the problem, not fix it. Group its 80
tests by the area under test and give each group a class whose name says what it
covers. Expect four to six classes.

Leave the existing top-level `core/tests_*.py` modules alone. They are
already scoped and named; merging them into the new package is a separate
argument, not part of this task.

**Watch for:** shared `setUp` and helper methods (`_create_tournament` and
friends) used across classes that end up in different modules. Put genuinely
shared fixtures in `core/tests/helpers.py` and import them; do not copy-paste.

**Verify.**

```bash
python manage.py test 2>&1 | tail -3     # exactly 371, still 0 failures
```

**Done when:** `core/tests.py` no longer exists, the package replaces it, the
count is exactly 371, and no class exceeds ~600 lines.

---

## F-4 — Throttling beyond the login form

**Severity:** high (step 1), medium (steps 2–3) · **Files:**
`core/views/auth.py`, `core/views/helpers.py`, `core/tests_throttling.py` (new)

This was listed as deliberately out of scope in `REMEDIATION_PLAN.md`
("Rate limiting anything other than login"). Planning it surfaced a bug in the
one endpoint that *is* throttled, so step 1 is a fix, not a feature.

### Step 1 — login throttling is keyed on the wrong IP

`core/views/auth.py` reads the client address directly:

```python
ip = request.META.get("REMOTE_ADDR", "unknown")
```

`core/audit.py` has a helper that exists precisely because that value is wrong
behind a proxy:

```python
def _client_ip(request):
    """Return the client IP, trusting X-Forwarded-For only behind known proxies."""
```

`_client_ip` is called in exactly one place — `log_action`. The login throttle
does not use it.

**Why this matters.** The README's own Deployment section tells operators to run
behind a reverse proxy and to set `DJANGO_TRUSTED_PROXY_COUNT`. Follow that
advice and the audit log records real client IPs while the throttle still sees
the proxy's address for every request. `LOGIN_ATTEMPTS_PER_IP = 10` then stops
being per-IP: it becomes a global budget of 10 failed logins per 5 minutes for
the entire site. One person fat-fingering their password ten times locks
everyone out, and an attacker gets no per-source limiting at all. The per-account
counter still works, which is the only reason this is not worse.

**Fix.** Import `_client_ip` and use it. Add a regression test that sets
`TRUSTED_PROXY_COUNT = 1` and `HTTP_X_FORWARDED_FOR`, then asserts two different
forwarded clients get independent counters — and that with the setting at `0`
the header is ignored, so this does not become a spoofing hole.

### Step 2 — generalise the throttle

The three helpers in `core/views/helpers.py` (`_throttle_get`, `_throttle_bump`,
`_throttle_clear`) are sound and already fail open on cache errors, which is the
correct choice and is documented. Wrap them in a decorator rather than
open-coding the pattern 77 more times:

```python
def throttled(scope, limit, window=LOGIN_ATTEMPT_WINDOW_SECONDS):
    """Limit POSTs to `limit` per `window` seconds per client IP.

    Fails open: a cache outage degrades throttling rather than taking the
    endpoint down. Same trade-off as the login throttle, for the same reason.
    """
```

Preserve the fixed-window semantics — the existing comment explains that
`incr()` keeps the original TTL so the window expires rather than sliding.

### Step 3 — apply it

**The unauthenticated write path first.** `account_register_view`
(`register/`) has no `@login_required` and no limit: account creation is
unbounded. That is the one genuinely open write endpoint.

Then the authenticated endpoints where repetition is abuse rather than use —
score submission, dispute raising, team invites. Do **not** blanket-apply to all
77 handlers. An organizer legitimately clicks through many actions in a session;
a limit that fires during normal use is worse than no limit, because the fix
will be to remove it.

**DECISION REQUIRED:** the limits themselves. Login uses 10/IP and 5/account per
5 minutes. Proposed starting points — confirm with the owner:

| Endpoint | Proposed limit |
|---|---|
| `account_register_view` | 5 per IP per hour |
| `submit_score` / `dispute_score` | 30 per IP per hour |
| `team_invite_view` | 20 per IP per hour |

**Verify.** New tests in `core/tests_throttling.py`: the limit fires, the window
expires, a cache failure fails open, and the proxy-aware keying from step 1
holds.

**Done when:** login is keyed on `_client_ip`, registration is limited, and
each behaviour has a test. Step 1 is worth landing on its own if steps 2–3
stall on the decision above.

---

## F-5 — Pin dependencies

**Severity:** low · **Files:** `requirements.txt`, `requirements-postgres.txt`,
`constraints.txt` (new)

**Problem.** The requirements are ranges:

```
django>=5.2,<5.3            # resolves to 5.2.17 today
psycopg2-binary>=2.9,<3     # resolves to 2.9.13 today
```

CI installs whatever the index serves that day. A patch release lands and the
first thing to run against it is `main`. The range is a deliberate, well-argued
choice — the comment in `requirements.txt` explains that the upper bound keeps a
future 6.x out — but "not 6.x" is a much weaker guarantee than "the version we
tested".

**Do this.** Keep the ranges as the declared compatibility window and add a
`constraints.txt` holding the exact resolved versions:

```
# Exact versions CI and production install. Regenerate with:
#     pip freeze --exclude-editable > constraints.txt
django==5.2.17
psycopg2-binary==2.9.13
...
```

Install with `-c constraints.txt` in both CI jobs and in the README's
Deployment section. Upgrading then becomes an explicit commit that CI validates,
which is also what makes the dependabot config from F-7 useful rather than
noisy.

**Verify.** `pip install -r requirements.txt -c constraints.txt` from a clean
virtualenv; suite passes; `pip freeze` matches the constraints file.

**Done when:** both CI jobs install through constraints and the README says how
to upgrade.

---

## F-6 — Deal with `scripts/`

**Severity:** low · **Files:** `scripts/` (13 files, 841 lines)

**Problem.** Thirteen ad-hoc scripts, honestly documented in `scripts/README.md`
as one-offs that "hardcode primary keys or usernames from one developer's
database". `diagnose_match_195.py` is named after a row. `promote_t2p1.py` is
named after a user account. Several write to the real database, not a test one.

They are not dangerous — they are inert unless someone runs them — but they are
841 lines that import from `core.views` and will keep showing up in every
future grep and refactor. The views split already required checking that their
imports still resolved (they do; `core/views/__init__.py` re-exports the names).

**DECISION REQUIRED — the owner's call, per script:**

| Script | Suggestion |
|---|---|
| `check_db.py`, `check_knockout.py`, `check_upcoming.py`, `check_perms.py` | Delete. Read-only dumps the Django admin and shell already provide. |
| `diagnose_match_195.py`, `diagnose_get_team.py`, `diagnose_knockout.py`, `promote_t2p1.py` | Delete. Bound to one developer's data; a diagnosis of a bug that was fixed. |
| `verify_dual_role_toggle.py`, `verify_role_separation.py`, `verify_completion_feature.py` | Convert to tests. They assert behaviour; that belongs in the suite, where CI runs it. |
| `seed_tt1.py`, `check_delete_fix.py` | Promote `seed_tt1.py` to `manage.py seed_demo` — seeding a dev database is a real need. Delete `check_delete_fix.py`. |

Do not act until the owner has chosen. If the answer is "keep them all", the
task is instead to fix the pyflakes findings in them (F-1 already does) and stop
there.

**If deleting:** update `scripts/README.md`, and the two references in
`README.md` (the project-layout tree around line 409, and the documentation
index around line 456).

**Done when:** `scripts/` contains only files someone can run today against a
database they have, and the docs match.

---

## F-7 — CI maintenance

**Severity:** cosmetic · **Files:** `.github/workflows/ci.yml`,
`.github/dependabot.yml` (new)

Three small things, one commit. None of these affect whether the suite passes;
they remove noise that makes a real warning harder to spot.

1. **Node 20 deprecation.** Both jobs use `actions/checkout@v4` and
   `actions/setup-python@v5`, which run on Node 20. GitHub already forces them
   onto Node 24 and warns on every run. Bump to `actions/checkout@v5` and
   `actions/setup-python@v6` — four call sites.

2. **PostgreSQL healthcheck noise.** `--health-cmd pg_isready` runs without a
   user, so `pg_isready` defaults to the OS user and the container log carries
   four `FATAL: role "root" does not exist` lines per run. Harmless, and it
   looks exactly like a real connection failure when you are scanning a log for
   one. Fix:

   ```yaml
   options: >-
     --health-cmd "pg_isready -U tm_user -d tournament_manager"
   ```

3. **Add `.github/dependabot.yml`.** There is none, so the `django>=5.2,<5.3`
   range silently drifts and the security advisories nobody is watching go
   unwatched:

   ```yaml
   version: 2
   updates:
     - package-ecosystem: pip
       directory: "/"
       schedule:
         interval: weekly
       open-pull-requests-limit: 5
     - package-ecosystem: github-actions
       directory: "/"
       schedule:
         interval: monthly
   ```

   This pairs with F-5: against pinned constraints, a dependabot PR is a
   specific version bump that CI validates. Against ranges, it has nothing to
   change.

**Note on log noise that is not a bug.** The PostgreSQL job's container log
contains, by design:

```
ERROR: duplicate key value violates unique constraint
       "core_teamtournamentparti_team_id_tournament_id_16715580_uniq"
```

Those are the database rejecting the second writer in
`core/tests_concurrency.py`. They are the guard working. Do not "fix" them, and
do not add them to a log filter — if they ever stop appearing, a test has
stopped testing.

**Verify.** Push and confirm: no Node deprecation warnings, no `role "root"`
FATALs, both jobs still green.

**Done when:** a CI run's logs contain no warning that is not about the code.

---

## Not in this plan

Stated explicitly so nobody assumes otherwise:

- **`ruff format` / import sorting.** Reasonable, but a whole-repo reformat
  should be its own decision and its own commit, after F-1 is quiet.
- **Line-length enforcement.** 850 lines would need rewrapping. Same argument.
- **Merging `core/tests_*.py` into the F-3 package.** Those modules are already
  scoped and named. Moving them is churn.
- **Typing / mypy.** Nothing in the codebase is annotated; starting now means a
  long tail of `Any`. Worth a conversation, not a task.
- **Deleting the stale remote branches.** The owner's, and tracked elsewhere.
- **Rotating the leaked test password hashes.** The owner has confirmed these
  were throwaway test accounts.
