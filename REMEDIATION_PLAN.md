# Remediation Plan

Companion to `CODE_REVIEW_FINDINGS.md`. This document is written to be executed
by an AI coding agent, task by task, from top to bottom.

Every task is self-contained and states: what to change, the exact code, how to
prove it worked, and what "done" means. Do not batch tasks together. Do not
reorder phases — later tasks assume earlier ones have landed.

---

## 0. Ground rules for the agent

Read this section before starting. It is not optional context.

1. **One task, one commit.** Commit message format:
   `fix(<area>): <summary> [<TASK-ID>]`. Never combine two task IDs in one
   commit.
2. **Every task ends green.** Run the task's verification command *and*
   `python manage.py test` before committing. If the full suite regresses
   relative to the baseline recorded in §1.3, stop and fix it — do not proceed.
3. **Write the regression test before the fix.** Each task specifies a test.
   Confirm it fails against the current code, then make it pass. A task with no
   failing-then-passing test is not complete.
4. **Never delete or skip an existing test to get green.** If an existing test
   genuinely encodes wrong behaviour, say so explicitly in the commit body and
   change the assertion with a comment explaining why.
5. **Do not refactor opportunistically.** Change only what the task names.
   Unrelated cleanup belongs in its own task and is out of scope here.
6. **Stop at every `DECISION REQUIRED` marker.** Those need a human product
   call. Do the parts you can, leave the rest, and report clearly what is
   blocked and what the options are.
7. **Do not invent line numbers.** Line references in this document were
   accurate at commit `342b032`. Locate code by searching for the quoted text,
   not by jumping to a line number.
8. **If a task's premise no longer matches the code**, stop and report. Do not
   guess at an equivalent change.

### Reporting format after each task

```
[TASK-ID] <status: DONE | BLOCKED | SKIPPED>
Files changed: <list>
Test added: <test path::name>
Verification: <command> -> <result>
Full suite: <N tests, F failures, E errors>
Notes: <anything unexpected>
```

---

## 1. Environment and baseline

### 1.1 Setup

```bash
cd /path/to/TournamentManagerV2
python -m venv .venv && source .venv/bin/activate   # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

The review ran against Django 5.2.17 on Python 3.11. `requirements.txt` pins
only `django>=4.2`; T-6.4 addresses that.

### 1.2 Never run destructive commands against a real database

Several tasks touch `restore_backup`, which deletes rows. Verify **only**
through `manage.py test` (which uses a throwaway database) or a scratch copy of
`db.sqlite3`. Never run `restore_backup()` or `manage.py flush` against the
working database.

### 1.3 Record the baseline

```bash
python manage.py test 2>&1 | tail -5
```

Expected at commit `342b032`:

```
Ran 139 tests in ~260s
FAILED (failures=1, errors=1)
```

- error: `test_delete_fix.py` breaks test discovery (fixed by T-0.2)
- failure: `test_add_court_availability_supports_additional_start_times`
  (fixed by T-0.1)

After Phase 0 the suite must be **OK** with 138+ tests. That green state is the
gate for every later task.

---

## Phase 0 — Make the test suite trustworthy

Nothing else can be verified until the suite is green. Do this first.

---

### T-0.1 — Remove the expired hardcoded date from the availability test

**Severity:** blocker · **Finding:** §5.1, §3.10 · **File:** `core/tests.py`

**Problem.** `test_add_court_availability_supports_additional_start_times` posts
an availability window for `2026-05-04` only. The tournament is created without
an explicit `start_date`, so `Tournament.start_date` defaults to
`timezone.localdate()` (today). `_build_slots` clamps
`range_start = max(base_date, availability.start_date)`, so once today is past
2026-05-04 the day loop never runs and `count_available_slots` returns 0. The
test passed when written and has failed on every run since 2026-05-05.

**Fix.** Make the test date relative to the tournament's own start date instead
of absolute, so it cannot expire again.

Add this helper to `UXAndLogicRegressionTests` (next to `_create_team`):

```python
	def _next_weekday_on_or_after(self, start_date, weekday):
		"""Return the first date >= start_date falling on `weekday` (0=Monday)."""
		offset = (weekday - start_date.weekday()) % 7
		return start_date + timedelta(days=offset)
```

Ensure `core/tests.py` imports `timedelta` (`from datetime import timedelta` —
check the existing imports; add only if missing).

Then rewrite the body of the test:

```python
	def test_add_court_availability_supports_additional_start_times(self):
		tournament = self._create_tournament(name="Explicit Times")
		court = Court.objects.create(tournament=tournament, name="Court A", is_available=True)
		self.client.force_login(self.organizer)

		# Pick a Monday on or after the tournament start date so the availability
		# window is never in the past relative to tournament.start_date.
		target_day = self._next_weekday_on_or_after(tournament.start_date, 0)

		response = self.client.post(
			reverse("add_court_availability", kwargs={"pk": tournament.pk}),
			{
				"courts": [str(court.pk)],
				"weekdays": ["0"],
				"start_time": "10:00",
				"additional_start_times": "13:00",
				"start_date": target_day.isoformat(),
				"end_date": target_day.isoformat(),
				"matches_per_court_per_day": "2",
				"is_active": "on",
			},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		availability = CourtAvailability.objects.get(court=court)
		self.assertEqual(availability.additional_start_times, "13:00")
		self.assertEqual(availability.matches_per_court_per_day, 2)
		self.assertEqual(count_available_slots(tournament), 2)
```

**Note on indentation:** `core/tests.py` is indented with **tabs**. Match the
surrounding style exactly or the file will mix tabs and spaces.

**Verify:**

```bash
python manage.py test core.tests.UXAndLogicRegressionTests.test_add_court_availability_supports_additional_start_times
```

**Done when:** that test passes, and it still passes if you change the system
clock by a year (conceptually — no absolute dates remain in the test body).

**Do not** "fix" this by changing `_build_slots`. The clamp is separately
addressed by T-4.9, which adds an organizer-facing warning rather than changing
scheduling behaviour.

---

### T-0.2 — Stop a root-level script from breaking test discovery

**Severity:** blocker · **Finding:** §5.2 · **Files:** `test_delete_fix.py` (move)

**Problem.** Django's test runner discovers `test*.py` from the project root.
`test_delete_fix.py` is an ad-hoc script that runs ORM queries at import time
against the development database, so discovery raises:

```
File "test_delete_fix.py", line 17, in <module>
    tournament = Tournament.objects.first()
django.db.utils.OperationalError: no such table: core_tournament
```

This is counted as an error on every run.

**Fix.**

```bash
mkdir -p scripts
git mv test_delete_fix.py scripts/check_delete_fix.py
```

Add `scripts/README.md`:

```markdown
# Ad-hoc scripts

One-off diagnostic and seeding scripts. These are **not** tests and are not run
by `manage.py test`. Several hardcode primary keys or usernames from a
particular developer database and will not work unmodified elsewhere.

Run with: `python scripts/<name>.py` from the project root.
```

While here, move the other root-level scripts into `scripts/` as well (T-6.3
covers the full list; if you prefer, do only `test_delete_fix.py` now and the
rest later — but the `test*.py` one must move now).

**Verify:**

```bash
python manage.py test 2>&1 | tail -3
```

**Done when:** the run reports `OK` with no collection error, and the
`Ran N tests` count is 138 (one fewer than 139 — the phantom error entry is
gone).

---

### T-0.3 — Record the green baseline

After T-0.1 and T-0.2:

```bash
python manage.py test 2>&1 | tail -3
```

**Done when:** output is `OK` and you have recorded the exact test count. Every
subsequent task must keep this green and must not reduce the count.

---

## Phase 1 — Secrets and data loss

These two tasks protect credentials and prevent irreversible data destruction.
Do them before any feature work.

---

### T-1.1 — Purge committed database backups and stop tracking them

**Severity:** critical · **Finding:** §1.1

**⚠ DECISION REQUIRED — this task rewrites git history and requires human
action the agent cannot perform.**

**Problem.** `backups/` is tracked by git — 11 JSON files, 1.8 MB, containing a
full `auth.user` serialization:

```
users: 51
sample fields: {'password': 'pbkdf2_sha256$1200000$lQK...', 'is_superuser': True,
                'username': 'admin', 'email': '<owner email>', ...}
```

Every account's password hash, including the superuser's, plus the owner's
personal email address, are in the repository and in its history.
`settings.BACKUP_DIR` points at the same directory, so new backups land there
too.

**Agent-executable part:**

1. Add to `.gitignore`, under the `# Django / local app data` block:

   ```gitignore
   # Database backups — may contain password hashes, never commit
   backups/
   ```

2. Stop tracking the files without deleting them from disk:

   ```bash
   git rm -r --cached backups/
   git commit -m "fix(security): stop tracking database backups [T-1.1]"
   ```

3. Move the backup directory out of the working tree. In
   `tournament_manager/settings.py` replace:

   ```python
   BACKUP_DIR = BASE_DIR / "backups"
   ```

   with:

   ```python
   # Default to a sibling directory so backups (which contain password hashes)
   # are never inside the git working tree. Override with DJANGO_BACKUP_DIR.
   BACKUP_DIR = Path(
       os.environ.get("DJANGO_BACKUP_DIR", BASE_DIR.parent / "tournament_manager_backups")
   )
   ```

4. Add a test asserting the directory is not tracked, in a new
   `core/tests_security.py`:

   ```python
   import subprocess
   from pathlib import Path
   from django.test import SimpleTestCase


   class RepositoryHygieneTests(SimpleTestCase):
       def test_backups_directory_is_not_tracked_by_git(self):
           repo_root = Path(__file__).resolve().parent.parent
           tracked = subprocess.run(
               ["git", "ls-files", "backups"],
               cwd=repo_root, capture_output=True, text=True,
           ).stdout.strip()
           self.assertEqual(
               tracked, "",
               "backups/ must not be tracked by git — it contains password hashes.",
           )
   ```

**Human-only part — report these, do not attempt them:**

- Purge `backups/` from git history with `git filter-repo --path backups/
  --invert-paths` or BFG, then force-push. Every collaborator must re-clone.
- **Rotate every account password**, starting with the superuser. The hashes are
  `pbkdf2_sha256` with 1.2M iterations so they are not trivially crackable, but
  they must be treated as compromised.
- If the repository was ever public, treat the owner email as exposed.

**Verify:** `python manage.py test core.tests_security`

**Done when:** `git ls-files backups` prints nothing, the new test passes, and
the history-purge and password-rotation steps are clearly reported as
outstanding human actions.

**Related (fold into this commit):** `teams.txt` at the repo root contains
plaintext seed passwords (`Alpha Squad,team9,pass123,...`). `username.txt` and
`teamnames.txt` are loose participant dumps. Move all three to
`scripts/fixtures/` and add a note to `scripts/README.md` that they contain
sample credentials and must never be used for real accounts.

---

### T-1.2 — Fix backup creation and make restore non-destructive

**Severity:** critical · **Finding:** §1.2 · **File:** `core/backup.py`

**Problem, part 1 — backup crashes.** `create_backup()` reads a field that the
global-team migration removed:

```python
    # Include M2M relationships
    m2m_data = {}
    for team in Team.objects.all():
        m2m_data[team.id] = list(team.preferred_courts.values_list("id", flat=True))
    data["_m2m_team_preferred_courts"] = m2m_data
```

Reproduced: `AttributeError: 'Team' object has no attribute 'preferred_courts'`.
Court preferences now live in `TeamTournamentCourtPreference`. The crash is
masked when there are zero `Team` rows, which is why it went unnoticed.

**Problem, part 2 — restore destroys unbacked data.** `BACKUP_MODELS` lists 11
models; the app has 25. `restore_backup()` runs
`model.objects.all().delete()` over `Team`, `Tournament` and `User`, which
cascades into 13 tables that were never serialized:
`TeamMembership`, `TeamTournamentParticipation`,
`TournamentIndividualRegistration`, `TeamTournamentCourtPreference`, `Player`,
`Notification`, `TeamInvite`, `OrganizerProfile`, `OrganizerApplication`,
`UserTeamAssignment`, `NoShowReport`, `TeamRegistration`,
`IndividualRegistration`. A restore therefore wipes every roster, registration
and notification, and deletes the account performing the restore.

**Fix.**

Replace the imports and `BACKUP_MODELS` at the top of `core/backup.py`:

```python
"""Backup and restore functionality."""
import json
import os
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core import serializers
from django.db import connection, transaction

from .models import (
    Tournament, Court, TimeSlot, Team, Match, Player,
    RescheduleRequest, OpenSlot, AuditLog, BackupRecord, CourtAvailability,
    TeamMembership, TeamTournamentParticipation, TeamTournamentCourtPreference,
    TournamentIndividualRegistration, UserTeamAssignment, TeamInvite,
    Notification, OrganizerProfile, OrganizerApplication,
    TeamRegistration, IndividualRegistration, NoShowReport,
)

# Order matters for restore: parents before children. Self-referential and
# circular FKs (Match.next_match, Tournament.champion) are handled by disabling
# constraint checks during the restore transaction.
BACKUP_MODELS = [
    User,
    Team,
    Tournament,
    Court,
    CourtAvailability,
    TimeSlot,
    Player,
    OrganizerProfile,
    OrganizerApplication,
    TeamMembership,
    UserTeamAssignment,
    TeamInvite,
    TeamTournamentParticipation,
    TeamTournamentCourtPreference,
    TournamentIndividualRegistration,
    TeamRegistration,
    IndividualRegistration,
    Match,
    RescheduleRequest,
    NoShowReport,
    OpenSlot,
    Notification,
    AuditLog,
    BackupRecord,
]

BACKUP_FORMAT_VERSION = 2
```

Delete the `shutil` import if it is unused after this change.

In `create_backup()`, replace the M2M block with a format stamp:

```python
    data["_meta"] = {
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": datetime.now().isoformat(),
        "models": [f"{m._meta.app_label}.{m._meta.model_name}" for m in BACKUP_MODELS],
    }
```

Replace `validate_backup()` so it rejects the legacy format that this restore
path cannot safely handle:

```python
def validate_backup(filepath):
    """Validate a backup file before restore."""
    try:
        with open(filepath, "r") as f:
            data = json.load(f)

        if "_m2m_team_preferred_courts" in data and "_meta" not in data:
            return False, (
                "This backup predates the global-team schema change and cannot be "
                "restored safely: it contains no roster, membership or registration "
                "data, so restoring it would delete all of yours."
            )

        meta = data.get("_meta") or {}
        version = meta.get("format_version")
        if version != BACKUP_FORMAT_VERSION:
            return False, (
                f"Unsupported backup format version {version!r} "
                f"(this server writes and reads version {BACKUP_FORMAT_VERSION})."
            )

        expected = {f"{m._meta.app_label}.{m._meta.model_name}" for m in BACKUP_MODELS}
        missing = sorted(expected - set(data))
        if missing:
            return False, "Backup is missing required data: " + ", ".join(missing)

        return True, "Backup is valid"
    except json.JSONDecodeError:
        return False, "Invalid JSON file"
    except Exception as e:
        return False, str(e)
```

Replace `restore_backup()` so it is atomic and validates first:

```python
def restore_backup(filepath):
    """Restore data from a backup file.

    Runs inside a single transaction with FK constraint checks deferred, so a
    partial failure leaves the database untouched rather than half-wiped.
    """
    valid, message = validate_backup(filepath)
    if not valid:
        raise ValueError(f"Refusing to restore an invalid backup: {message}")

    with open(filepath, "r") as f:
        data = json.load(f)

    with transaction.atomic():
        with connection.constraint_checks_disabled():
            for model in reversed(BACKUP_MODELS):
                model.objects.all().delete()

            for model in BACKUP_MODELS:
                model_name = f"{model._meta.app_label}.{model._meta.model_name}"
                if model_name not in data:
                    continue
                for obj in serializers.deserialize("json", json.dumps(data[model_name])):
                    obj.save()

        # Re-assert every constraint now that all rows are present.
        for model in BACKUP_MODELS:
            connection.check_constraints(table_names=[model._meta.db_table])

    return True
```

**Regression test.** Add to `core/tests_backup.py`:

```python
import tempfile
from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from core.backup import create_backup, restore_backup, validate_backup
from core.models import (
    Tournament, Team, TeamMembership, TeamTournamentParticipation,
    Court, TeamTournamentCourtPreference, Notification,
)


class BackupRoundTripTests(TestCase):
    def _populate(self):
        t = Tournament.objects.create(name="RT", format="round_robin", players_per_team=2)
        court = Court.objects.create(tournament=t, name="C1")
        team = Team.objects.create(name="Alpha")
        part = TeamTournamentParticipation.objects.create(
            team=team, tournament=t, status="active"
        )
        TeamTournamentCourtPreference.objects.create(participation=part, court=court)
        user = User.objects.create_user(username="alice", password="s3cret-pass")
        TeamMembership.objects.create(team=team, user=user, role="captain")
        Notification.objects.create(
            user=user, notification_type="general", message="hello", tournament=t
        )

    def test_create_backup_succeeds_with_teams_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(BACKUP_DIR=Path(tmp)):
                self._populate()
                record = create_backup(notes="round-trip")
                self.assertTrue((Path(tmp) / record.filename).exists())

    def test_restore_preserves_memberships_and_registrations(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(BACKUP_DIR=Path(tmp)):
                self._populate()
                record = create_backup(notes="round-trip")
                path = Path(tmp) / record.filename

                Notification.objects.all().delete()
                TeamMembership.objects.all().delete()

                restore_backup(path)

                self.assertEqual(TeamMembership.objects.count(), 1)
                self.assertEqual(TeamTournamentParticipation.objects.count(), 1)
                self.assertEqual(TeamTournamentCourtPreference.objects.count(), 1)
                self.assertEqual(Notification.objects.count(), 1)
                self.assertTrue(User.objects.filter(username="alice").exists())

    def test_legacy_backup_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "backup_manual_legacy.json"
            legacy.write_text(
                '{"auth.user": [], "core.tournament": [], '
                '"_m2m_team_preferred_courts": {}}'
            )
            valid, message = validate_backup(legacy)
            self.assertFalse(valid)
            self.assertIn("predates", message)
```

**Verify:**

```bash
python manage.py test core.tests_backup
```

**Done when:** all three tests pass, `create_backup` succeeds with teams
present, a restore preserves memberships/participations/preferences/
notifications, and the 11 legacy files in `backups/` are rejected with a clear
message instead of silently destroying data.

**Also update** `backup_view`'s template copy to warn that restore replaces all
data, and note in `README.md` that pre-v2 backups cannot be restored.

---

## Phase 2 — Crashing and broken endpoints

---

### T-2.1 — Rewrite `organizer_remove_team` against the current schema

**Severity:** critical · **Finding:** §1.3 · **Files:** `core/views.py`,
`templates/core/partials/team_detail_content.html`, `templates/core/teams.html`

**Problem.** The view uses three attributes that no longer exist:

```python
    tournament = team.tournament          # Team has no tournament FK
    captain_user = team.user              # Team has no user FK
    if not captain_user.captained_teams.exists():   # no such related name
```

Reproduced: `AttributeError: 'Team' object has no attribute 'tournament'` — a
500 on every click. The route is live (`core/urls.py`, name
`organizer_remove_team`) and reachable from two templates. It has zero test
coverage.

**Design decision.** The old view deleted the team *and* its captain's user
account. Under the global-team schema a team can compete in several tournaments
and members are real user accounts, so cascading a user delete from a
per-tournament removal is wrong. The correct behaviour is to remove the team's
*participation* in the selected tournament and leave the team and all accounts
intact — which is exactly what `remove_team_from_tournament` already does.

**Fix — preferred: delete the view and redirect the templates.**

1. Delete the `organizer_remove_team` function from `core/views.py`.
2. Delete its `path(...)` line from `core/urls.py`.
3. In both templates, replace the form action with the working equivalent.
   `remove_team_from_tournament` is routed as
   `remove_team_from_tournament` taking `pk` (tournament) and `participation_pk`.

   In `templates/core/teams.html` and
   `templates/core/partials/team_detail_content.html`, the loop must expose the
   participation. Where the template currently renders teams, switch the context
   to supply `participation.pk`. In `teams_view` (`core/views.py`), annotate each
   team with its participation id — the view already does
   `participation = team.participations.filter(tournament=tournament).first()`
   to set `team.group`, so add on the next line:

   ```python
                   team.participation_pk = participation.pk if participation else None
   ```

   Then in the template:

   ```html
   {% if t.participation_pk %}
   <form method="post"
         action="{% url 'remove_team_from_tournament' pk=tournament.pk participation_pk=t.participation_pk %}"
         style="display:inline;"
         onsubmit="return confirm('Remove {{ t.name }} from this tournament? The team and its member accounts are kept.')">
     {% csrf_token %}
     <button type="submit" class="btn btn-danger btn-sm">Remove</button>
   </form>
   {% endif %}
   ```

   Apply the same treatment in `team_detail_content.html`, using the
   participation for the currently selected tournament. `team_detail` must
   supply it; add to that view's context:

   ```python
       team_participation = (
           TeamTournamentParticipation.objects.filter(team=team, tournament=tournament).first()
           if tournament else None
       )
   ```

   and pass `"team_participation": team_participation`.

4. Update the confirm text — the old copy said "This will delete the team and
   its captain account", which will no longer be true.

**Fix — alternative if you must keep the URL** (e.g. external bookmarks): keep
the function name but reimplement it as a thin, correct wrapper:

```python
@login_required
@require_POST
def organizer_remove_team(request, pk):
    """Deprecated alias: remove a team's participation in the selected tournament."""
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can remove teams.")
        return redirect("team_detail", pk=pk)
    team = get_object_or_404(Team, pk=pk)
    tournament = _get_tournament(request)
    participation = (
        TeamTournamentParticipation.objects.filter(team=team, tournament=tournament).first()
        if tournament else None
    )
    if not participation:
        messages.error(request, "Select the tournament first, then remove the team from it.")
        return redirect("team_detail", pk=pk)
    return remove_team_from_tournament(
        request, pk=tournament.pk, participation_pk=participation.pk
    )
```

Note this alias must be defined *after* `remove_team_from_tournament` in the
module, and it inherits that view's authorization (including T-3.1's ownership
check once that lands).

**Regression test** — add to `core/tests_views_teams.py`:

```python
from django.contrib.auth.models import User
from django.test import TestCase

from core.models import (
    Tournament, Team, TeamMembership, TeamTournamentParticipation,
)


class OrganizerTeamRemovalTests(TestCase):
    def setUp(self):
        self.org = User.objects.create_user(
            username="org", password="pass12345", is_staff=True
        )
        self.t = Tournament.objects.create(
            name="RM", format="round_robin", status="registration_open", players_per_team=1
        )
        self.team = Team.objects.create(name="Alpha")
        self.part = TeamTournamentParticipation.objects.create(
            team=self.team, tournament=self.t, status="active"
        )
        self.member = User.objects.create_user(username="cap", password="pass12345")
        TeamMembership.objects.create(team=self.team, user=self.member, role="captain")

    def test_removal_does_not_raise_and_keeps_accounts(self):
        self.client.force_login(self.org)
        session = self.client.session
        session["selected_tournament_id"] = self.t.pk
        session.save()

        response = self.client.post(
            f"/tournament/{self.t.pk}/remove-team/{self.part.pk}/", follow=True
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            TeamTournamentParticipation.objects.filter(pk=self.part.pk).exists()
        )
        # The team and the captain's account survive.
        self.assertTrue(Team.objects.filter(pk=self.team.pk).exists())
        self.assertTrue(User.objects.filter(username="cap").exists())
```

**Verify:**

```bash
python manage.py test core.tests_views_teams
grep -rn "organizer_remove_team" core/ templates/   # expect: nothing, or only the alias
```

**Done when:** no template references a route that 500s, removing a team from a
tournament works end to end, and no user account is deleted as a side effect.

---

## Phase 3 — Authorization

T-3.1 is the largest task in this plan and is a prerequisite for a correct
multi-organizer deployment. T-3.2 through T-3.5 are independent and small; do
them first if you want quick wins.

---

### T-3.2 — Restrict analytics and the audit log

**Severity:** high · **Finding:** §2.2 · **File:** `core/views.py`

**Problem.** `analytics_view` and `audit_log_view` carry only `@login_required`.
Neither checks `_is_organizer` nor enrollment, and `_get_tournament()` falls
through to `return _get_available_tournaments().first()` for a user enrolled in
nothing — so an unrelated account is handed an arbitrary tournament. Reproduced:
both return **200** for a user with no teams and no registrations.
`audit_log_view` additionally includes global rows
(`Q(tournament__isnull=True)`), exposing login events,
`member_password_reset`, `user_deleted`, `impersonation_started`, and client IPs.

**Fix — audit log: organizers only.**

Immediately after the `def audit_log_view(request):` line, before
`tournament = _get_tournament(request)`:

```python
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can view the audit log.")
        return redirect("dashboard")
```

**Fix — analytics: organizers, or participants of that tournament.**

After `tournament = _get_tournament(request)` and the existing
`if not tournament:` early return, insert:

```python
    if not _is_organizer(request.user) and not _is_user_enrolled_in_tournament(
        request.user, tournament
    ):
        messages.error(request, "You are not enrolled in that tournament.")
        return redirect("dashboard")
```

Then scope the audit trail inside analytics to organizers only — find:

```python
    recent_logs = AuditLog.objects.filter(tournament=tournament).order_by("-timestamp")[:20]
```

and replace with:

```python
    recent_logs = (
        AuditLog.objects.filter(tournament=tournament).order_by("-timestamp")[:20]
        if _is_organizer(request.user)
        else AuditLog.objects.none()
    )
```

Guard the corresponding block in `templates/core/analytics.html` with
`{% if is_organizer %}` and add `"is_organizer": _is_organizer(request.user)` to
the analytics context (check whether `_tournament_context` already supplies an
equivalent flag — `user_is_organizer` is injected globally by the
`user_organizer_status` context processor, so `{% if user_is_organizer %}` may
be enough and no context change is needed).

**Regression test** — `core/tests_authorization.py`:

```python
from django.contrib.auth.models import User
from django.test import TestCase

from core.models import Tournament, Team, TeamMembership, TeamTournamentParticipation


class AnalyticsAndAuditAccessTests(TestCase):
    def setUp(self):
        self.t = Tournament.objects.create(
            name="A", format="round_robin", status="active", players_per_team=1
        )
        self.team = Team.objects.create(name="Alpha")
        TeamTournamentParticipation.objects.create(
            team=self.team, tournament=self.t, status="active"
        )
        self.player = User.objects.create_user(username="p", password="pass12345")
        TeamMembership.objects.create(team=self.team, user=self.player, role="captain")
        self.outsider = User.objects.create_user(username="nobody", password="pass12345")
        self.org = User.objects.create_user(
            username="org", password="pass12345", is_staff=True
        )

    def test_outsider_cannot_read_analytics(self):
        self.client.force_login(self.outsider)
        response = self.client.get("/analytics/")
        self.assertEqual(response.status_code, 302)

    def test_outsider_cannot_read_audit_log(self):
        self.client.force_login(self.outsider)
        response = self.client.get("/audit-log/")
        self.assertEqual(response.status_code, 302)

    def test_participant_can_read_analytics(self):
        self.client.force_login(self.player)
        response = self.client.get("/analytics/")
        self.assertEqual(response.status_code, 200)

    def test_participant_cannot_read_audit_log(self):
        self.client.force_login(self.player)
        response = self.client.get("/audit-log/")
        self.assertEqual(response.status_code, 302)

    def test_organizer_can_read_both(self):
        self.client.force_login(self.org)
        self.assertEqual(self.client.get("/analytics/").status_code, 200)
        self.assertEqual(self.client.get("/audit-log/").status_code, 200)
```

**Verify:** `python manage.py test core.tests_authorization`

**Done when:** all five tests pass. Check that no existing test asserted a 200
for an outsider on these pages; if one did, it encoded the bug and must be
updated with a comment.

---

### T-3.3 — Add the missing participant check to `dispute_score`

**Severity:** high · **Finding:** §2.3 · **File:** `core/views.py`

**Problem.** `dispute_score` checks only that the user has a team and did not
submit the score:

```python
    team = _get_team(request.user, match.tournament)
    if not team or match.submitted_by == request.user:
        messages.error(request, "Cannot dispute your own submission.")
        return _redirect_to_match_detail(request, pk)
```

It never checks that the team is in the match — unlike `confirm_score`, which
does. Reproduced: a third team in the same tournament disputed a match it was
not in (`status: disputed, disputed_by: u2`). That freezes any pending result
and blocks bracket progression until an organizer intervenes.

**Fix.** Insert immediately after the `if not team or match.submitted_by ...`
block, before the `match.status != "pending_confirmation"` check:

```python
    if match.team1 != team and match.team2 != team:
        messages.error(request, "You are not a participant in this match.")
        return _redirect_to_match_detail(request, pk)
```

Place it first if you prefer — order does not matter functionally, but keeping
it adjacent to the other participation checks is clearer.

**Regression test** — append to `core/tests_authorization.py`:

```python
from datetime import timedelta
from django.utils import timezone
from core.models import Court, Match


class DisputeAuthorizationTests(TestCase):
    def setUp(self):
        self.t = Tournament.objects.create(
            name="D", format="round_robin", status="active",
            players_per_team=1, default_match_duration=30,
        )
        self.court = Court.objects.create(tournament=self.t, name="C1")
        self.users, self.teams = [], []
        for i in range(3):
            team = Team.objects.create(name=f"T{i}")
            TeamTournamentParticipation.objects.create(
                team=team, tournament=self.t, status="active"
            )
            user = User.objects.create_user(username=f"u{i}", password="pass12345")
            TeamMembership.objects.create(team=team, user=user, role="captain")
            self.teams.append(team)
            self.users.append(user)
        self.match = Match.objects.create(
            tournament=self.t, match_number=1,
            team1=self.teams[0], team2=self.teams[1], court=self.court,
            scheduled_time=timezone.now() + timedelta(days=1),
            scheduled_end_time=timezone.now() + timedelta(days=1, minutes=30),
            status="upcoming",
        )

    def _submit(self):
        self.client.force_login(self.users[0])
        self.client.post(
            f"/match/{self.match.pk}/submit-score/",
            {"score_team1": 3, "score_team2": 1, "notes": ""},
        )
        self.client.logout()
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, "pending_confirmation")

    def test_non_participant_cannot_dispute(self):
        self._submit()
        self.client.force_login(self.users[2])          # in the tournament, not the match
        self.client.post(f"/match/{self.match.pk}/dispute-score/", {"dispute_notes": "x"})
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, "pending_confirmation")
        self.assertIsNone(self.match.disputed_by)

    def test_opponent_can_still_dispute(self):
        self._submit()
        self.client.force_login(self.users[1])          # the opponent
        self.client.post(f"/match/{self.match.pk}/dispute-score/", {"dispute_notes": "x"})
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, "disputed")
        self.assertEqual(self.match.disputed_by, self.users[1])
```

**Verify:** `python manage.py test core.tests_authorization.DisputeAuthorizationTests`

**Done when:** both tests pass — the outsider is blocked and the legitimate
opponent is not.

---

### T-3.4 — Close the open redirect in `select_tournament`

**Severity:** high · **Finding:** §2.4 · **File:** `core/views.py`

**Problem.**

```python
    next_url = request.POST.get("next") or "dashboard"
    ...
    return redirect(next_url)
```

`django.shortcuts.resolve_url` returns any string containing `/` or `.`
unchanged, so a POSTed `next` of `https://evil.example/pwn` produces
`302 https://evil.example/pwn`. Reproduced.

**Fix.** Add to the imports at the top of `core/views.py`:

```python
from django.utils.http import url_has_allowed_host_and_scheme
```

Add a module-level helper next to `_safe_page_param`:

```python
def _safe_next_url(request, default="dashboard"):
    """Return a POSTed/GET 'next' target only when it is local to this site."""
    candidate = (request.POST.get("next") or request.GET.get("next") or "").strip()
    if not candidate:
        return default
    if url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return default
```

In `select_tournament`, replace:

```python
    next_url = request.POST.get("next") or "dashboard"
```

with:

```python
    next_url = _safe_next_url(request)
```

Leave the three `redirect(next_url)` call sites unchanged.

**Audit the rest of the codebase for the same pattern** before finishing:

```bash
grep -n "redirect(.*request\.\(POST\|GET\)\.get" core/views.py
grep -n "HX-Redirect" core/views.py | grep -v reverse
```

`mark_notification_read` does `HX-Redirect: notif.link`. `Notification.link`
values are all set server-side today, so this is not currently exploitable —
but note it in the commit body and consider validating it with the same helper
if notification links ever become user-supplied.

**Regression test** — append to `core/tests_authorization.py`:

```python
class OpenRedirectTests(TestCase):
    def setUp(self):
        self.t = Tournament.objects.create(
            name="R", format="round_robin", status="active", players_per_team=1
        )
        self.team = Team.objects.create(name="Alpha")
        TeamTournamentParticipation.objects.create(
            team=self.team, tournament=self.t, status="active"
        )
        self.user = User.objects.create_user(username="u", password="pass12345")
        TeamMembership.objects.create(team=self.team, user=self.user, role="captain")

    def test_external_next_is_rejected(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/tournament/select/",
            {"tournament_id": self.t.pk, "next": "https://evil.example/pwn"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("evil.example", response.headers["Location"])

    def test_protocol_relative_next_is_rejected(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/tournament/select/",
            {"tournament_id": self.t.pk, "next": "//evil.example/pwn"},
        )
        self.assertNotIn("evil.example", response.headers["Location"])

    def test_local_next_is_honoured(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/tournament/select/", {"tournament_id": self.t.pk, "next": "/fixtures/"}
        )
        self.assertEqual(response.headers["Location"], "/fixtures/")
```

**Verify:** `python manage.py test core.tests_authorization.OpenRedirectTests`

**Done when:** all three pass, including the protocol-relative case.

---

### T-3.5 — Make `_can_manage_reschedule` safe to call in isolation

**Severity:** medium · **Finding:** §2.7 · **File:** `core/views.py`

**Problem.**

```python
    if tournament.registration_mode == "individual":
        return True
```

The helper short-circuits to `True` for any authenticated user in an
individual-mode tournament, with no check that the user *is* the competitor.
The two POST call sites happen to check participation first, so this is not
currently exploitable — but `match_detail` feeds the result straight into
`can_reschedule`, and the helper is a trap for any future caller.

**Fix.** Make the helper verify the relationship itself:

```python
def _can_manage_reschedule(user, tournament, team):
    """Return True when user can create/respond to reschedules for the given competitor."""
    if _is_organizer(user):
        return True
    if not user.is_authenticated or not tournament or not team:
        return False
    if tournament.registration_mode == "individual":
        # The competitor is a shadow team; the user must own that registration.
        return TournamentIndividualRegistration.objects.filter(
            tournament=tournament, shadow_team=team, user=user, status="active"
        ).exists()
    return _is_captain(user, team)
```

**Regression test** — append to `core/tests_authorization.py`: build an
individual-mode tournament with two registrations, and assert
`_can_manage_reschedule(user_a, tournament, shadow_team_of_b)` is `False` while
`_can_manage_reschedule(user_a, tournament, shadow_team_of_a)` is `True`.

**Verify:** run the full suite — this helper is used by `match_detail`,
`request_reschedule` and `respond_reschedule`, so a regression will show up
in existing individual-mode tests.

**Done when:** the new assertions pass and no existing test regresses.

---

### T-3.1 — Give tournaments an owner and scope organizer powers to it

**Severity:** high · **Finding:** §2.1, §2.5 · **Files:** `core/models.py`,
new migration, `core/views.py`, `core/tests*.py`

**⚠ DECISION REQUIRED** — see the policy question below before writing code.

**Problem.** `Tournament` has no owner or creator field. Every organizer-gated
view authorizes with `_is_organizer(request.user)` and then
`get_object_or_404(Tournament, pk=pk)` with no ownership check. Any verified
organizer can configure, start, pause, cancel, duplicate, **delete**, or
disqualify teams from any other organizer's tournament, and can reset passwords
for any team's members. `organizer_public_page` already reverse-engineers
authorship from `AuditLog` rows because the link does not exist.

Separately (§2.5), `set_user_organizer`, `delete_user_account`,
`toggle_user_suspension` and `review_organizer_application` gate on
`_is_organizer` only, so any organizer can promote users to organizer, approve
organizer applications, and suspend or delete other organizers. Those are
admin-level powers behind an organizer-level check.

**DECISION REQUIRED — pick one policy and record it in the commit body:**

- **(a) Strict ownership** — an organizer manages only tournaments they created.
  Superusers manage everything. *Recommended.*
- **(b) Shared pool** — any organizer manages any tournament (status quo), but
  the destructive actions (delete, cancel, disqualify) require ownership or
  superuser.
- **(c) Explicit co-organizers** — add a `co_organizers` M2M. Most flexible,
  most work.

The steps below implement **(a)**. Adapt `_can_manage_tournament` for (b) or (c).

**Step 1 — model field.** In `core/models.py`, add to `Tournament` after
`created_at`:

```python
    created_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_tournaments",
        help_text="Organizer who created this tournament.",
    )
```

**Step 2 — migration with a data backfill.**

```bash
python manage.py makemigrations core -n tournament_created_by
```

Then edit the generated migration to backfill from the audit log, which is the
only existing record of authorship:

```python
def backfill_created_by(apps, schema_editor):
    Tournament = apps.get_model("core", "Tournament")
    AuditLog = apps.get_model("core", "AuditLog")
    for tournament in Tournament.objects.filter(created_by__isnull=True):
        entry = (
            AuditLog.objects.filter(
                tournament_id=tournament.pk, action="tournament_created"
            )
            .exclude(user__isnull=True)
            .order_by("timestamp")
            .first()
        )
        if entry:
            tournament.created_by_id = entry.user_id
            tournament.save(update_fields=["created_by"])


def noop_reverse(apps, schema_editor):
    pass
```

Add `migrations.RunPython(backfill_created_by, noop_reverse)` after the
`AddField` operation.

**Step 3 — set the owner on creation.** In `tournament_setup`, after
`t = form.save(commit=False)` and before `t.save()`:

```python
            t.created_by = request.user
```

In `duplicate_tournament`, add `created_by=request.user,` to the
`Tournament.objects.create(...)` call.

**Step 4 — the authorization helper.** Add next to `_is_organizer`:

```python
def _can_manage_tournament(user, tournament):
    """Return True when `user` may administer `tournament`.

    Superusers and staff manage everything. A verified organizer manages the
    tournaments they created. Legacy tournaments with no recorded creator
    (created before `created_by` existed and not attributable from the audit
    log) fall back to any verified organizer so they do not become orphaned.
    """
    if not _is_organizer(user):
        return False
    if user.is_superuser or user.is_staff:
        return True
    if tournament is None:
        return False
    if tournament.created_by_id is None:
        return True  # legacy, unattributable — see docstring
    return tournament.created_by_id == user.pk
```

The legacy fallback is deliberate: making unattributable tournaments
superuser-only would strand any that predate the audit log. Once the backfill
has run and you have confirmed every row has a `created_by`, tighten the
`created_by_id is None` branch to `return False` and add a migration that makes
the field non-nullable.

**Step 5 — apply the helper.** Every view below currently does
`if not _is_organizer(request.user)` and then fetches the tournament. For each,
fetch the tournament first, then check `_can_manage_tournament`:

```python
    tournament = get_object_or_404(Tournament, pk=pk)
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")
```

Apply to: `tournament_config`, `add_court`, `add_court_availability`,
`estimate_court_availability_end_date`, `delete_court_availability`,
`add_timeslot`, `add_teams_bulk`, `remove_team_from_tournament`,
`open_registration`, `close_registration`, `generate_schedule`,
`start_tournament`, `complete_tournament`, `proceed_to_knockout_view`,
`estimate_tournament_end_date`, `delete_tournament`, `cancel_tournament`,
`pause_tournament`, `resume_tournament`, `duplicate_tournament`,
`registration_review_view`, `approve_registration`, `reject_registration`,
`disqualify_team`, `organizer_announce_view`, `compute_end_date_view`,
`seed_participants_view`, `tournament_team_sub_view`.

For `settings_view` the tournament comes from `_get_tournament(request)` rather
than a URL pk — guard the POST branch:

```python
        if not _can_manage_tournament(request.user, tournament):
            messages.error(request, "You do not manage that tournament.")
            return redirect("dashboard")
```

For the match-level organizer actions (`resolve_dispute`,
`override_match_result`, `mark_no_show`, and the organizer branch of
`submit_score`), check against `match.tournament`.

`_get_available_tournaments()` and `_tournament_context` should also be scoped
so an organizer's switcher lists only their own tournaments plus legacy ones.

**Step 6 — separate admin powers from organizer powers (§2.5).** Change the
gate in `set_user_organizer`, `delete_user_account`, `toggle_user_suspension`
and `review_organizer_application` from `_is_organizer(request.user)` to:

```python
    if not (request.user.is_superuser or request.user.is_staff):
        messages.error(request, "Only site administrators can manage user accounts.")
        return redirect("settings")
```

`impersonate_user` already requires `is_superuser` — leave it.

Then hide the user-management block in `templates/core/settings.html` (and its
partial) behind `{% if request.user.is_superuser or request.user.is_staff %}`,
and stop sending the full user list to non-admin organizers: in `settings_view`,
make the `"users"` context entry conditional on the same check.

**Regression test** — `core/tests_tournament_ownership.py`:

```python
from django.contrib.auth.models import User
from django.test import TestCase

from core.models import OrganizerProfile, Tournament


class TournamentOwnershipTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="pass12345")
        OrganizerProfile.objects.create(user=self.owner, verified=True)
        self.other = User.objects.create_user(username="other", password="pass12345")
        OrganizerProfile.objects.create(user=self.other, verified=True)
        self.admin = User.objects.create_superuser(
            username="root", password="pass12345", email=""
        )
        self.t = Tournament.objects.create(
            name="Owned", format="round_robin", players_per_team=1,
            created_by=self.owner,
        )

    def test_owner_can_open_config(self):
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.get(f"/tournament/{self.t.pk}/config/").status_code, 200
        )

    def test_other_organizer_cannot_open_config(self):
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.get(f"/tournament/{self.t.pk}/config/").status_code, 302
        )

    def test_other_organizer_cannot_delete(self):
        self.client.force_login(self.other)
        self.client.post(
            f"/tournament/{self.t.pk}/delete/", {"confirm_delete": "DELETE"}
        )
        self.assertTrue(Tournament.objects.filter(pk=self.t.pk).exists())

    def test_superuser_can_delete(self):
        self.client.force_login(self.admin)
        self.client.post(
            f"/tournament/{self.t.pk}/delete/", {"confirm_delete": "DELETE"}
        )
        self.assertFalse(Tournament.objects.filter(pk=self.t.pk).exists())

    def test_non_admin_organizer_cannot_promote_users(self):
        self.client.force_login(self.owner)
        target = User.objects.create_user(username="victim", password="pass12345")
        self.client.post(
            f"/settings/users/{target.pk}/organizer/", {"is_organizer": "1"}
        )
        self.assertFalse(
            OrganizerProfile.objects.filter(user=target, verified=True).exists()
        )

    def test_legacy_tournament_without_owner_is_manageable(self):
        legacy = Tournament.objects.create(
            name="Legacy", format="round_robin", players_per_team=1, created_by=None
        )
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.get(f"/tournament/{legacy.pk}/config/").status_code, 200
        )
```

**Existing tests will break.** Most create organizers with `is_staff=True`,
which `_can_manage_tournament` treats as an admin, so they should keep passing.
Any that use a verified `OrganizerProfile` against someone else's tournament
will now be denied — those encoded the bug. Update them to create the
tournament with the right `created_by`, and note each change in the commit body.

**Verify:**

```bash
python manage.py makemigrations --check --dry-run   # expect: no changes missing
python manage.py test
```

**Done when:** the ownership tests pass, the full suite is green, the backfill
migration runs cleanly on a copy of the real database, and the chosen policy
(a/b/c) is recorded in the commit body.

---

## Phase 4 — Correctness

These are independent of each other and of Phase 3. Each can be done and
shipped on its own.

---

### T-4.1 — Persist court preferences chosen at team creation

**Severity:** high · **Finding:** §3.1 · **File:** `core/views.py`

**Problem.** `CreateTeamForm` declares `preferred_courts` and makes it
**required** whenever the tournament has available courts, with a `clean()` that
rejects an empty selection. `create_team_view` reads `team_name`, `department`
and `participant_name` and never touches `preferred_courts`. Reproduced:

```
PROBE stored court preferences: 0
PROBE readiness errors mentioning preferences: ['These teams still need court preferences: Alpha.']
```

The captain is forced to choose courts, the choice is dropped, and the
tournament then refuses to start on exactly that missing data.

**Fix.** In `create_team_view`, in the team-mode branch, find:

```python
                TeamTournamentParticipation.objects.create(team=team, tournament=tournament, status=initial_status)
                TeamMembership.objects.create(team=team, user=request.user, role="captain")
```

and replace with:

```python
                participation = TeamTournamentParticipation.objects.create(
                    team=team, tournament=tournament, status=initial_status
                )
                TeamMembership.objects.create(team=team, user=request.user, role="captain")
                preferred_courts = form.cleaned_data.get("preferred_courts") or []
                if preferred_courts:
                    TeamTournamentCourtPreference.objects.bulk_create([
                        TeamTournamentCourtPreference(participation=participation, court=court)
                        for court in preferred_courts
                    ])
```

`TeamTournamentCourtPreference` is already imported at the top of `core/views.py`
— confirm before adding a duplicate import.

**Regression test** — `core/tests_registration.py`:

```python
from django.contrib.auth.models import User
from django.test import TestCase

from core.models import (
    Tournament, Team, Court, TeamTournamentCourtPreference,
)
from core.views import _validate_tournament_ready


class CreateTeamCourtPreferenceTests(TestCase):
    def test_preferred_courts_are_saved(self):
        t = Tournament.objects.create(
            name="CT", format="round_robin", status="registration_open", players_per_team=1
        )
        court = Court.objects.create(tournament=t, name="C1")
        user = User.objects.create_user(username="cap", password="pass12345")
        self.client.force_login(user)

        self.client.post(
            f"/tournament/{t.pk}/create-team/",
            {"team_name": "Alpha", "department": "", "preferred_courts": [str(court.pk)]},
            follow=True,
        )

        team = Team.objects.get(name="Alpha")
        self.assertEqual(
            TeamTournamentCourtPreference.objects.filter(
                participation__team=team, participation__tournament=t
            ).count(),
            1,
        )
        self.assertFalse(
            [e for e in _validate_tournament_ready(t) if "preference" in e],
            "readiness check should not demand preferences that were just supplied",
        )
```

**Verify:** `python manage.py test core.tests_registration`

**Done when:** preferences are stored and the readiness check no longer blocks
on a team that supplied them at creation.

---

### T-4.2 — Enforce roster rules on the invite path

**Severity:** high · **Finding:** §3.2 · **File:** `core/views.py`

**Problem.** `accept_team_invite` checks only whether the user is already on
*that one team*. `join_team_view` checks team fullness **and**
`_is_user_enrolled_in_tournament`; the invite path checks neither. Reproduced:

```
PROBE m1 teams in tournament after accepting 2nd invite: ['B', 'A']
PROBE team A size vs players_per_team=2 -> 4
```

A player ends up on two competing teams in the same tournament, and a team
exceeds `players_per_team`. `close_registration` then blocks with "Mismatched
teams" and there is no way to trim the roster except manual removal. README
states the rule as "a team can only be entered when its member count is exactly
equal to the tournament players-per-team value".

**Fix.** In `accept_team_invite`, after the existing "already a member" check
and before `TeamMembership.objects.create(...)`, insert:

```python
    # The team may compete in several tournaments; the invite must not break the
    # roster cap or the one-team-per-tournament rule in any of them.
    live_statuses = ("setup", "registration_open", "ready", "scheduled", "active", "paused")
    blocking = []
    participations = TeamTournamentParticipation.objects.filter(
        team=team, status__in=["pending", "active", "waitlisted"]
    ).select_related("tournament")
    current_size = team.memberships.count()
    for participation in participations:
        tournament = participation.tournament
        if tournament.status not in live_statuses:
            continue
        capacity = max(1, tournament.players_per_team or 1)
        if current_size >= capacity:
            blocking.append(
                f"'{team.name}' already has {current_size} of {capacity} players "
                f"for '{tournament.name}'."
            )
            continue
        if _is_user_enrolled_in_tournament(request.user, tournament):
            blocking.append(
                f"You are already registered for '{tournament.name}' with another team."
            )

    if blocking:
        for reason in blocking:
            messages.error(request, reason)
        return redirect("my_invites")
```

Leave the invite `pending` so the captain can free a slot and the player can
retry.

**Also fix the same gap in `manage_team_members`.** The `add_existing` branch
checks the *selected* tournament only
(`if tournament and existing_user.memberships.filter(...)`), and the
account-creation branch checks nothing. The view does have a fullness guard,
but it runs against `_get_tournament(request)` and returns early. Extract the
loop above into a helper and reuse it:

```python
def _roster_conflicts_for_joining(user, team):
    """Return human-readable reasons `user` cannot join `team` right now."""
    ...
```

Call it from both `accept_team_invite` and `manage_team_members`.

**Regression test** — append to `core/tests_registration.py`:

```python
class TeamInviteRosterRulesTests(TestCase):
    def setUp(self):
        self.t = Tournament.objects.create(
            name="IV", format="round_robin", status="registration_open", players_per_team=2
        )
        self.a = Team.objects.create(name="A")
        self.b = Team.objects.create(name="B")
        for team in (self.a, self.b):
            TeamTournamentParticipation.objects.create(
                team=team, tournament=self.t, status="active"
            )
        self.cap_a = User.objects.create_user(username="capa", password="pass12345")
        self.cap_b = User.objects.create_user(username="capb", password="pass12345")
        self.member = User.objects.create_user(username="m1", password="pass12345")
        TeamMembership.objects.create(team=self.a, user=self.cap_a, role="captain")
        TeamMembership.objects.create(team=self.b, user=self.cap_b, role="captain")
        TeamMembership.objects.create(team=self.b, user=self.member, role="member")

    def test_cannot_join_second_team_in_same_tournament(self):
        invite = TeamInvite.objects.create(
            team=self.a, invited_user=self.member, invited_by=self.cap_a
        )
        self.client.force_login(self.member)
        self.client.post(f"/team-invite/{invite.pk}/accept/")
        teams = set(
            TeamMembership.objects.filter(user=self.member).values_list(
                "team__name", flat=True
            )
        )
        self.assertEqual(teams, {"B"})

    def test_cannot_exceed_players_per_team(self):
        filler = User.objects.create_user(username="m2", password="pass12345")
        TeamMembership.objects.create(team=self.a, user=filler, role="member")  # A now 2/2
        newcomer = User.objects.create_user(username="m3", password="pass12345")
        invite = TeamInvite.objects.create(
            team=self.a, invited_user=newcomer, invited_by=self.cap_a
        )
        self.client.force_login(newcomer)
        self.client.post(f"/team-invite/{invite.pk}/accept/")
        self.assertEqual(TeamMembership.objects.filter(team=self.a).count(), 2)
```

Import `TeamInvite`, `TeamMembership` and `TeamTournamentParticipation` at the
top of the test module.

**Verify:** `python manage.py test core.tests_registration`

**Done when:** both tests pass and the invite flow enforces the same rules as
`join_team_view`.

---

### T-4.3 — Reject responses to reschedule requests that are already answered

**Severity:** high · **Finding:** §3.3 · **File:** `core/views.py`

**Problem.** `respond_reschedule` never checks `rr.status`. Reproduced:

```
PROBE reschedule re-respond -> status after approve+reject: rejected | match time unchanged: True
```

An approved request flips to `rejected` while the match keeps the rescheduled
time — the audit trail and the schedule disagree. Separately, conflict detection
runs only at *request* time, so two pending requests created against the same
free slot can both be approved and double-book a court.

**Fix, part 1 — idempotence.** In `respond_reschedule`, after
`action = request.POST.get("action")` and before the `if action == "approve":`
branch:

```python
    if rr.status != "pending":
        messages.info(
            request,
            f"That reschedule request was already {rr.get_status_display().lower()}.",
        )
        return _redirect_to_match_detail(request, match.pk)
```

**Fix, part 2 — re-check conflicts at approval time.** Inside the
`if action == "approve":` branch, before mutating `rr` or `match`:

```python
        duration = timedelta(minutes=match.tournament.default_match_duration)
        end_dt = rr.new_time + duration
        target_court = rr.new_court or match.court
        active_match_statuses = ["upcoming", "in_progress", "pending_confirmation", "disputed"]

        court_conflict = Match.objects.filter(
            tournament=match.tournament,
            court=target_court,
            scheduled_time__lt=end_dt,
            scheduled_end_time__gt=rr.new_time,
            status__in=active_match_statuses,
        ).exclude(pk=match.pk).exists()

        team_conflict = Match.objects.filter(
            tournament=match.tournament,
            scheduled_time__lt=end_dt,
            scheduled_end_time__gt=rr.new_time,
            status__in=active_match_statuses,
        ).filter(
            Q(team1=match.team1) | Q(team2=match.team1)
            | Q(team1=match.team2) | Q(team2=match.team2)
        ).exclude(pk=match.pk).exists()

        if court_conflict or team_conflict:
            rr.status = "cancelled"
            rr.responded_at = timezone.now()
            rr.save(update_fields=["status", "responded_at"])
            messages.error(
                request,
                "That slot is no longer free — the request has been cancelled. "
                "Please submit a new one.",
            )
            return _redirect_to_match_detail(request, match.pk)
```

The existing body then reuses its own `duration` local; remove the now-duplicate
`duration = timedelta(...)` line further down so there is a single definition.

**Fix, part 3 — guard against races.** Wrap the approve branch's mutations in
`transaction.atomic()` and re-read the request row with
`select_for_update()`. Add `from django.db import transaction` to the imports.
On SQLite this is a no-op for concurrency but is correct under Postgres:

```python
        with transaction.atomic():
            rr = RescheduleRequest.objects.select_for_update().get(pk=rr.pk)
            if rr.status != "pending":
                messages.info(request, "That request was answered by someone else.")
                return _redirect_to_match_detail(request, match.pk)
            # ... existing approve body ...
```

**Regression test** — `core/tests_rescheduling.py`: create an approved request
and POST `reject` to it; assert `rr.status` is still `approved` and
`match.scheduled_time` is unchanged. Add a second test where two pending
requests target the same slot, approve both, and assert the second is refused
and the court is not double-booked.

**Verify:** `python manage.py test core.tests_rescheduling`

**Done when:** an answered request cannot be re-answered, and two requests
cannot both claim the same slot.

---

### T-4.4 — Double elimination: implement the losers bracket or stop advertising it

**Severity:** high · **Finding:** §3.4 · **Files:** `core/scheduling.py`,
`core/standings.py`, `README.md`

**⚠ DECISION REQUIRED.**

**Problem.**

```python
def generate_double_elimination(tournament):
    """Generate double elimination bracket (winners + losers)."""
    teams = _active_teams(tournament)
    generate_knockout(tournament, teams=teams, bracket_type="winners")
    # Losers bracket matches are created dynamically as teams are eliminated
```

Nothing in the codebase ever creates a match with `bracket_type="losers"`, and
`advance_winner` has no losing-side counterpart. The format is single
elimination in practice while README advertises "Winners and losers brackets".
`estimate_required_matches` still reserves `2n-2` slots for it, so
`_validate_tournament_ready` demands roughly double the court availability that
will actually be used. `DoubleEliminationBracketTests` only asserts
winners-bracket behaviour, so the gap is invisible to the suite.

**Option A — honest downgrade (small, recommended as an immediate step).**

1. In `estimate_required_matches`, change the double-elimination line from
   `return max(n - 1, (2 * n) - 2)` to `return n - 1` **only if** you also make
   the format behave as single elimination. Do not leave the estimate and the
   generator disagreeing either way.
2. Mark the choice in `Tournament.FORMAT_CHOICES`:
   `("double_elimination", "Double Elimination (losers bracket not yet implemented)")`.
3. Correct the README format table.
4. Add a test asserting the documented behaviour so it cannot silently drift:
   `test_double_elimination_currently_generates_single_elimination_only`, with a
   comment pointing at this task.

**Option B — implement it properly.** Specification:

- `generate_double_elimination` creates the winners bracket (as today) **and** a
  losers-bracket skeleton with `bracket_type="losers"`. For a bracket of size
  `2^k`, the losers bracket has `2(k-1)` rounds: minor rounds fed by
  winners-bracket losers, alternating with major rounds that pair losers-bracket
  survivors.
- Add `advance_loser_to_losers_bracket(match)` in `core/standings.py`, mirroring
  `advance_winner`. Each winners-bracket match needs a `next_loser_match` link;
  add a nullable self-FK `Match.next_loser_match` (with migration) rather than
  overloading `next_match`.
- Call it from every site that currently calls `advance_winner`:
  `_lock_match_score`, `_finalize_no_show_match`, `submit_score` (organizer
  branch), `handle_withdrawal`, `disqualify_team`, and the test-maker
  randomizer. Missing one leaves the bracket stuck.
- Add a grand final between the winners-bracket champion and the losers-bracket
  champion, plus the bracket reset rule if the losers champion wins it.
  **DECISION REQUIRED:** reset or single grand final — they give different
  results and different match counts.
- `_determine_champion` and `_check_and_finalize_tournament` currently treat the
  winners final as the end of the tournament. Both must be taught about the
  grand final or a double-elimination tournament will complete early.
- `get_bracket_data` filters `bracket_type="winners"`; the standings template
  needs a losers-bracket section.
- Restore `estimate_required_matches` to `2n - 2` (or `2n - 1` with a reset).

Option B is a multi-day change. Do not start it inside this remediation pass —
raise it as its own piece of work and do Option A now so the product stops
making a false claim.

**Done when:** the generator, the match estimate, the format label and the
README all agree, and a test pins whichever behaviour was chosen.

---

### T-4.5 — Implement the head-to-head tiebreaker

**Severity:** medium · **Finding:** §3.5 · **File:** `core/standings.py`

**Problem.**

```python
        elif tb == "head_to_head":
            key.append(0)  # Simplified; would need pairwise comparison
```

`Tournament.tiebreaker_order` defaults to
`["game_diff", "games_won", "head_to_head"]`, so every tournament ships with a
configured tiebreaker that does nothing. Ties beyond `games_won` resolve by
arbitrary dict ordering, which is not stable across runs.

**Fix.** Head-to-head is only meaningful *between the tied teams*, so it cannot
be a per-team scalar computed before sorting. Restructure `calculate_standings`
to sort in two passes: sort by the scalar keys, then re-order each group of
teams that are still tied using their mutual results.

Add to `core/standings.py`:

```python
def _head_to_head_points(tournament, team_ids, group=None):
    """Return {team_id: points} counting only matches among `team_ids`."""
    points = {tid: 0 for tid in team_ids}
    matches = tournament.matches.filter(
        status__in=["confirmed", "forfeited"],
        team1_id__in=team_ids,
        team2_id__in=team_ids,
    )
    if group:
        matches = matches.filter(group=group)
    for match in matches:
        if match.status == "forfeited":
            if match.winner_id in points:
                points[match.winner_id] += tournament.points_per_win
            continue
        if match.score_team1 is None or match.score_team2 is None:
            continue
        if match.score_team1 > match.score_team2:
            points[match.team1_id] += tournament.points_per_win
            points[match.team2_id] += tournament.points_per_loss
        elif match.score_team2 > match.score_team1:
            points[match.team2_id] += tournament.points_per_win
            points[match.team1_id] += tournament.points_per_loss
        else:
            points[match.team1_id] += tournament.points_per_draw
            points[match.team2_id] += tournament.points_per_draw
    return points


def _apply_head_to_head(tournament, ordered_rows, scalar_key, group=None):
    """Re-order runs of rows that tie on `scalar_key` using mutual results."""
    result = []
    index = 0
    while index < len(ordered_rows):
        end = index + 1
        while end < len(ordered_rows) and scalar_key(ordered_rows[end]) == scalar_key(
            ordered_rows[index]
        ):
            end += 1
        run = ordered_rows[index:end]
        if len(run) > 1:
            ids = [row["team"].id for row in run]
            h2h = _head_to_head_points(tournament, ids, group=group)
            run.sort(key=lambda r: (h2h.get(r["team"].id, 0), r["team"].id), reverse=True)
        result.extend(run)
        index = end
    return result
```

In `calculate_standings`, replace the sort block:

```python
    tiebreakers = tournament.get_tiebreaker_order()
    scalar_tiebreakers = [tb for tb in tiebreakers if tb != "head_to_head"]

    def scalar_key(standing):
        return _sort_key(standing, scalar_tiebreakers)

    result = sorted(standings.values(), key=scalar_key, reverse=True)
    if "head_to_head" in tiebreakers:
        result = _apply_head_to_head(tournament, result, scalar_key, group=group)

    for idx, s in enumerate(result):
        s["rank"] = idx + 1
    return result
```

Then remove the `head_to_head` branch from `_sort_key` entirely — it no longer
belongs there — and add a final `standing["team"].id` component to `_sort_key`
so ordering is deterministic when everything else ties.

**Caveat to document in the docstring:** this implementation applies
head-to-head only *after* the scalar tiebreakers, matching the configured order
semantics of "points, then game_diff, then games_won, then head_to_head". Some
competition rules apply head-to-head *before* goal difference; if that is wanted,
it is a separate product decision.

**Regression test** — `core/tests_standings.py`: build a 3-team round robin
where two teams finish level on points, game difference and games won, but one
beat the other in their direct match. Assert the winner of that match ranks
higher. Add a second test asserting `calculate_standings` returns the same order
across two consecutive calls (determinism).

**Verify:** `python manage.py test core.tests_standings`

**Done when:** both tests pass and no existing standings test regresses.

---

### T-4.6 — Keep individual registrations and shadow participations in sync

**Severity:** high · **Finding:** §3.6 · **File:** `core/views.py`

**Problem.** `TournamentIndividualRegistration` and its `shadow_team`'s
`TeamTournamentParticipation` are synchronized only by
`_ensure_shadow_team_for_registration`. Three views change one side without it:

- `approve_registration` / `reject_registration` set `reg.status`; the shadow
  participation — which is what the match engine and `_validate_tournament_ready`
  read — keeps its old value. **A rejected individual still gets scheduled.**
- `disqualify_team` does the reverse: it withdraws the participation and leaves
  the registration `active`, so the player still appears in participant lists
  and in `active_participant_count`.
- `reject_registration` also never sets `withdrawn_at`.

The existence of a `reconcile_participant_integrity` management command suggests
this drift is already known.

**Fix.** Add a single synchronization helper next to
`_ensure_shadow_team_for_registration`:

```python
def _sync_registration_status(registration):
    """Push an individual registration's status onto its shadow participation."""
    if not registration.shadow_team_id:
        _ensure_shadow_team_for_registration(registration)
        return
    TeamTournamentParticipation.objects.filter(
        team_id=registration.shadow_team_id,
        tournament_id=registration.tournament_id,
    ).update(
        status=registration.status,
        group=registration.group or "",
        seed=registration.seed,
        withdrawn_at=registration.withdrawn_at,
    )


def _sync_participation_status(participation):
    """Push a shadow participation's status back onto its individual registration."""
    if not participation.team.is_internal:
        return
    TournamentIndividualRegistration.objects.filter(
        shadow_team=participation.team, tournament=participation.tournament
    ).update(
        status=participation.status,
        withdrawn_at=participation.withdrawn_at,
    )
```

Call `_sync_registration_status(reg)` in `approve_registration` and
`reject_registration`, immediately after `reg.save(...)`, but only when `reg` is
a `TournamentIndividualRegistration` — the views fetch either type, so branch on
`isinstance(reg, TournamentIndividualRegistration)` rather than the existing
`hasattr(reg, "user")` duck-check, which is fragile.

In `reject_registration`, also set `withdrawn_at`:

```python
    reg.status = "withdrawn"
    if hasattr(reg, "withdrawn_at"):
        reg.withdrawn_at = timezone.now()
        reg.save(update_fields=["status", "withdrawn_at", "updated_at"])
    else:
        reg.save(update_fields=["status", "updated_at"])
```

Call `_sync_participation_status(participation)` in `disqualify_team` after the
participation is saved, and in `handle_withdrawal` (`core/withdrawals.py`) after
the participation is marked withdrawn.

**Regression test** — `core/tests_individual_mode.py`: create an individual-mode
tournament with a registration and shadow team; reject the registration; assert
the shadow `TeamTournamentParticipation.status == "withdrawn"` and that
`active_participant_count(tournament) == 0`. Add the mirror test: disqualify the
shadow participation and assert the registration goes `withdrawn`.

**Verify:**

```bash
python manage.py test core.tests_individual_mode
python manage.py audit_participant_integrity   # should report no drift on a seeded DB
```

**Done when:** both directions stay in sync and
`audit_participant_integrity` reports nothing after an approve/reject/disqualify
cycle.

---

### T-4.7 — Fix JSON seed submission

**Severity:** medium · **Finding:** §3.7 · **File:** `core/views.py`

**Problem.** In `seed_participants_view`, JSON input produces string keys:

```python
                data = json.loads(request.body)
                seeds = data.get("seeds", {})
```

but lookup uses an integer: `seeds.get(p.pk)`. No seed ever matches, and the
view still reports "Seeds saved." The form-POST path (`int(key[5:])`) works.

**Fix.** Normalize keys and values immediately after parsing:

```python
        if "application/json" in content_type:
            try:
                data = json.loads(request.body)
                raw_seeds = data.get("seeds", {})
            except (json.JSONDecodeError, AttributeError):
                return JsonResponse({"error": "Invalid JSON"}, status=400)
            if not isinstance(raw_seeds, dict):
                return JsonResponse({"error": "'seeds' must be an object"}, status=400)
            seeds = {}
            for key, val in raw_seeds.items():
                try:
                    seeds[int(key)] = int(val)
                except (TypeError, ValueError):
                    return JsonResponse(
                        {"error": f"Invalid seed entry: {key!r} -> {val!r}"}, status=400
                    )
```

Then drop the now-redundant `if isinstance(seeds, dict)` guard in the apply loop.

While here, make the "nothing applied" case honest:

```python
        applied = 0
        for p in participants:
            new_seed = seeds.get(p.pk)
            if new_seed is not None and new_seed != p.seed:
                p.seed = new_seed
                p.save(update_fields=["seed"])
                applied += 1

        log_action(
            request, "seeds_updated",
            f"Seeds updated for '{tournament.name}' ({applied} changed)",
            tournament=tournament,
        )
        messages.success(request, f"Seeds saved ({applied} changed).")
```

**Regression test** — `core/tests_seeding.py`: POST
`{"seeds": {"<participation_pk>": 3}}` with
`content_type="application/json"` and assert the seed persisted. Add a negative
test asserting a non-numeric value returns 400.

**Verify:** `python manage.py test core.tests_seeding`

**Done when:** JSON and form paths produce identical results and the success
message reflects what actually changed.

---

### T-4.8 — Scope substitutes to a tournament

**Severity:** medium · **Finding:** §3.8 · **Files:** `core/models.py`,
migration, `core/views.py`

**⚠ DECISION REQUIRED — this changes the data model.**

**Problem.** `tournament_team_sub_view` documents "participate in this
tournament's matches without being a permanent member", but it creates a plain
`TeamMembership` with `role="sub"`. `TeamMembership` has no tournament scope, so
the substitute joins the team in **every** tournament it competes in, counts
toward `team.memberships.count()` — used by `close_registration`,
`_validate_tournament_ready`, `_promote_team_participation_when_full` and
`_check_roster_minimum` — and `_get_team` returns that team for them, letting
them submit and confirm scores anywhere.

**Option A — scope the membership (recommended).** Add a nullable FK:

```python
    tournament = models.ForeignKey(
        "Tournament",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="scoped_memberships",
        help_text="Set for tournament-scoped roles such as substitutes; NULL for permanent members.",
    )
```

Update `unique_together` to `[["team", "user", "tournament"]]` and write the
migration. Then:

- `tournament_team_sub_view` sets `tournament=tournament` on the created
  membership and filters `current_subs` by it.
- Every roster-size count must exclude scoped/sub rows. Audit each of these and
  add `.filter(tournament__isnull=True)` or `.exclude(role="sub")` as
  appropriate:
  `close_registration`, `_validate_tournament_ready`,
  `_promote_team_participation_when_full`, `_check_roster_minimum`,
  `join_team_view`, `manage_team_members`, `enter_existing_team_view`,
  `team_detail` (`members_full`), and `join_tournament_view`'s `is_full`.
- `_get_team` must not return a team for a sub outside that sub's tournament.

**Option B — exclude subs from roster counts only (smaller, partial).** Leave
the model alone and add `.exclude(role="sub")` to the roster counts above. This
fixes the roster-arithmetic bug but leaves the cross-tournament leak: a sub
added for one tournament can still act in another.

**Regression test:** add a sub to a team competing in two tournaments, and
assert (i) the roster count for `close_registration` is unchanged, and (ii) the
sub cannot submit a score in the *other* tournament.

**Done when:** adding a substitute does not change roster arithmetic and does
not grant rights in unrelated tournaments. If you choose Option B, record the
remaining gap explicitly in the commit body.

---

### T-4.9 — Warn when court availability falls outside the tournament dates

**Severity:** medium · **Finding:** §3.10 · **File:** `core/views.py`

**Problem.** `_build_slots` clamps
`range_start = max(base_date, availability.start_date or base_date)` where
`base_date = tournament.start_date or localdate()`. An availability window that
ends before the tournament start date produces zero slots, silently. The warning
in `tournament_config` explicitly skips exactly those rows:

```python
        for availability in court_availabilities:
            if availability.end_date:
                continue
```

**Fix.** Do not change `_build_slots` — the clamp is intentional. Extend the
warning to cover date-bounded rows. Replace the `availability_date_warning`
block in `tournament_config` with:

```python
    availability_warnings = []
    base_date = tournament.start_date or timezone.localdate()

    stale_rows = [
        availability for availability in court_availabilities
        if availability.end_date and availability.end_date < base_date
    ]
    if stale_rows:
        availability_warnings.append(
            f"{len(stale_rows)} availability entr"
            f"{'y ends' if len(stale_rows) == 1 else 'ies end'} before the tournament "
            f"start date ({base_date}), so {'it contributes' if len(stale_rows) == 1 else 'they contribute'} "
            "no schedulable slots. Extend the end date or move the tournament start date earlier."
        )

    if tournament.end_date:
        conflicting_open_rows = 0
        for availability in court_availabilities:
            if availability.end_date:
                continue
            range_start = max(base_date, availability.start_date or base_date)
            if tournament.end_date < range_start:
                conflicting_open_rows += 1
        if conflicting_open_rows:
            availability_warnings.append(
                f"Tournament end date ({tournament.end_date}) is earlier than the effective "
                f"start date for {conflicting_open_rows} open-ended availability entr"
                f"{'y' if conflicting_open_rows == 1 else 'ies'}. "
                "Update the tournament end date or set explicit end dates on those entries."
            )

    availability_date_warning = " ".join(availability_warnings)
```

Keep the `availability_date_warning` context key so the template needs no
change, or switch the template to loop over `availability_warnings` — pick one
and be consistent.

**Regression test:** create a tournament starting today with an availability row
ending yesterday; GET the config page and assert the response contains "before
the tournament start date" and that `count_available_slots(tournament) == 0`.

**Done when:** an organizer whose availability is silently useless is told so on
the config page.

---

### T-4.10 — Small correctness fixes (batch)

**Severity:** low–medium · **Finding:** §3.9, §3.11

These are individually tiny. Do them as **one commit per bullet** or one batched
commit titled `fix(core): batch of small correctness fixes [T-4.10]` with a body
listing each. Each still needs a test.

1. **`_get_user_tournament_ids` can return `None`** (§3.9). A membership in a
   team with no participations yields `None` from the join. Filter it:

   ```python
       ids = {
           tid for tid in user.memberships.filter(team__is_internal=False)
           .values_list("team__participations__tournament_id", flat=True)
           .distinct()
           if tid is not None
       }
   ```

   *Test:* a user on a team with no participations plus one real registration
   must not see the multi-tournament switcher.

2. **Dead `completed` check in `submit_score`.** The
   `if match.tournament.status == "completed"` block is unreachable —
   `allowed_tournament_statuses` already rejected it. Delete it.
   *Test:* none needed; confirm coverage does not drop.

3. **`resolve_dispute` silently no-ops.** If `final_score_team1`/`2` are absent,
   the whole body is skipped and the view redirects with no message. Add an
   explicit guard at the top:

   ```python
       if score1 is None or score2 is None:
           messages.error(request, "Enter the final score for both sides to resolve this dispute.")
           return _redirect_to_match_detail(request, pk)
       ```
   *Test:* POST with no scores and assert an error message and `status ==
   "disputed"` unchanged.

4. **`override_match_result` leaves the tournament active.** It never calls
   `_check_and_finalize_tournament`. Add the call after `match.save()`, alongside
   the bracket-advance calls the other score paths make.
   *Test:* override the final outstanding round-robin match and assert the
   tournament becomes `completed`.

5. **`_lock_match_score` erases the confirmer.** It sets
   `match.confirmed_by = confirmed_by_user` unconditionally, so an auto-lock
   (`confirmed_by_user=None`) wipes a previously recorded confirmer. Change to:

   ```python
       if confirmed_by_user is not None:
           match.confirmed_by = confirmed_by_user
   ```
   *Test:* confirm a score as a user, trigger the auto-lock path, assert
   `confirmed_by` survives.

6. **`duplicate_tournament` drops fields.** It omits `enable_third_place_match`
   and `matches_per_court_per_day`. Add both to the `create(...)` call.
   *Test:* set both on a source tournament, duplicate, assert both carried over.

7. **`add_timeslot` swallows form errors.** Add the same `else:` branch
   `add_court_availability` uses:

   ```python
       else:
           for errs in form.errors.values():
               for err in errs:
                   messages.error(request, err)
   ```
   *Test:* POST an invalid time slot and assert an error message is present.

8. **`CourtAvailabilityForm.is_active` default.** `initial=True` does not apply
   to bound forms, so an unchecked box silently creates an inactive row that
   `count_available_slots` ignores. Either make the checkbox checked-by-default
   in the template **and** keep the current behaviour, or treat a missing value
   as active. Pick one — do not leave `initial=True` implying a default it does
   not provide. *Test:* POST without `is_active` and assert the documented
   outcome.

9. **Team-name race.** `create_team_view` and `create_standalone_team_view`
   check `Team.objects.filter(name__iexact=...)` then `create()` against a
   `unique` column. Wrap the create in `try/except IntegrityError` and surface
   the same form error the pre-check produces.
   *Test:* not easily raceable in a unit test; assert the `except` path renders
   the form error when forced with `mock.patch`.

10. **`user_public_profile` over-counts wins.** It counts
    `Match.objects.filter(winner_id__in=team_ids, status="confirmed")` across all
    of the user's *current* teams, crediting matches played before they joined.
    Constrain by `TeamMembership.joined_at`:
    `.filter(scheduled_time__gte=membership.joined_at)` per team, or accept the
    approximation and say so in the template ("team record", not "their record").
    *Test:* a user joining a team after a win must not be credited with it.

11. **Timezone-inconsistent bucketing in `analytics_view`.**
    `m.scheduled_time.strftime("%Y-%m-%d")` uses the raw UTC datetime; the rest
    of the codebase uses `timezone.localtime`. Change to
    `timezone.localtime(m.scheduled_time).strftime("%Y-%m-%d")`.
    *Test:* with `TIME_ZONE` set to a non-UTC zone, a late-evening match buckets
    to the local date.

---

## Phase 5 — Security hardening

---

### T-5.1 — Apply Django's password validators everywhere

**Severity:** high · **Finding:** §4.1 · **Files:** `core/forms.py`,
`tournament_manager/settings.py`, `core/views.py`

**Problem.** `AccountRegistrationForm.clean()` checks only that the two password
fields match and the username is free. It never calls
`django.contrib.auth.password_validation.validate_password`, and neither does
`TeamMemberInviteForm` or the bulk-team CSV importer. `AUTH_PASSWORD_VALIDATORS`
(`MinimumLengthValidator`, default 8) is therefore **never applied anywhere** —
a one-character password is accepted at signup. Meanwhile
`SelfPasswordChangeForm`, `reset_member_password` and `reset_captain_password`
each hand-roll an inconsistent 6-character minimum.

**Fix — step 1: strengthen the validator set.** In `settings.py`:

```python
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
```

**Warning:** existing tests create users with `password="pass123"` (7
characters, and `CommonPasswordValidator` may reject it). Validators apply to
*form* input, not to `User.objects.create_user`, so test fixtures are
unaffected — but any test that posts a registration form with a weak password
will now fail. Search for them:

```bash
grep -rn "pass123" core/tests.py | grep -i "register\|signup\|invite"
```

Update those to a compliant password such as `"Regression-Pass-1"`.

**Fix — step 2: a shared mixin.** Add near the top of `core/forms.py`:

```python
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError


def _validate_password_strength(password, user=None, field_errors=None):
    """Run Django's configured validators, returning a list of messages."""
    if not password:
        return []
    try:
        validate_password(password, user=user)
    except DjangoValidationError as exc:
        return list(exc.messages)
    return []
```

**Fix — step 3: use it in every form that sets a password.**

`AccountRegistrationForm.clean()`:

```python
    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        if password != cleaned.get("password_confirm"):
            raise forms.ValidationError("Passwords do not match.")
        if User.objects.filter(username=cleaned.get("username", "").strip()).exists():
            raise forms.ValidationError("Username already taken.")
        for message in _validate_password_strength(password):
            self.add_error("password", message)
        return cleaned
```

`TeamMemberInviteForm.clean()`: same pattern, after the match check.

`SelfPasswordChangeForm.clean()`: replace
`if len(new_password) < 6: raise ...` with the shared validator, keeping the
"must differ from current" rule.

**Fix — step 4: the two raw-POST reset views.** `reset_member_password` and
`reset_captain_password` read `request.POST` directly and enforce `len >= 6`.
Replace that check in both with:

```python
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError as DjangoValidationError
    try:
        validate_password(new_password, user=member_user)
    except DjangoValidationError as exc:
        for message in exc.messages:
            messages.error(request, message)
        return redirect("team_detail", pk=pk)
```

(Import at module level rather than inside the function; shown inline here only
for clarity.)

**Fix — step 5: the bulk importer.** `_create_teams_from_data` creates accounts
from CSV passwords with no validation. Add a check per row and skip invalid ones
with a warning message naming the row, rather than silently creating a weak
account.

**Regression test** — `core/tests_passwords.py`:

```python
from django.contrib.auth.models import User
from django.test import TestCase


class PasswordPolicyTests(TestCase):
    def test_registration_rejects_short_password(self):
        response = self.client.post(
            "/register/",
            {
                "full_name": "Ada L", "username": "ada",
                "password": "x", "password_confirm": "x",
            },
        )
        self.assertEqual(response.status_code, 200)   # re-rendered with errors
        self.assertFalse(User.objects.filter(username="ada").exists())

    def test_registration_accepts_strong_password(self):
        response = self.client.post(
            "/register/",
            {
                "full_name": "Ada L", "username": "ada",
                "password": "Regression-Pass-1", "password_confirm": "Regression-Pass-1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(username="ada").exists())
```

**Verify:** `python manage.py test core.tests_passwords && python manage.py test`

**Done when:** no code path can create an account with a password that fails
`AUTH_PASSWORD_VALIDATORS`, and the 6-character constants are gone.

---

### T-5.2 — Put Test Maker behind a real gate

**Severity:** high · **Finding:** §4.2 · **Files:** `core/views.py`,
`tournament_manager/settings.py`, `core/urls.py`

**Problem.** `test_maker_view` is gated on `_is_organizer` only and routed at
`/testing/`. It creates real `User` accounts with a default password of
`pass123`, mass-registers **existing real users** into a tournament without
consent (`register_existing_to_open_tournament`), and randomizes and confirms
live match scores. `set_dispute_window` additionally mutates module globals at
runtime:

```python
            import core.views as _self
            _self.DEFAULT_DISPUTE_WINDOW_MINUTES = minutes
            _self.CRITICAL_STAGE_DISPUTE_WINDOW_MINUTES = minutes
```

which is per-process (inconsistent across workers), lost on restart, and not
recorded as configuration anywhere.

**Fix — step 1: feature flag.** In `settings.py`, after the `DEBUG` definition:

```python
# Test Maker creates real accounts and mutates live match data. Off unless
# explicitly enabled; defaults to on only in DEBUG.
ENABLE_TEST_MAKER = os.environ.get(
    "DJANGO_ENABLE_TEST_MAKER", "True" if DEBUG else "False"
).lower() in ("true", "1", "yes")
```

**Fix — step 2: enforce it.** At the top of `test_maker_view`, before the
existing organizer check:

```python
    if not getattr(settings, "ENABLE_TEST_MAKER", False):
        raise Http404
    if not request.user.is_superuser:
        messages.error(request, "Test Maker is restricted to site administrators.")
        return redirect("dashboard")
```

Add `from django.http import Http404` to the imports (the module currently
imports `Http404` locally inside two functions — hoist it).

Hide the nav entry: wrap the Test Maker link in `templates/core/base.html` (and
any settings page link) in
`{% if user.is_superuser and test_maker_enabled %}`, and expose
`test_maker_enabled` from a context processor or the settings view.

**Fix — step 3: replace the global mutation.** Move the dispute window into the
`Tournament` model so it is per-tournament, persistent and auditable:

```python
    dispute_window_minutes = models.PositiveIntegerField(
        default=10,
        help_text="Minutes an opponent has to dispute a submitted score before it auto-locks.",
    )
```

Write the migration, then change `_dispute_window_minutes_for_match`:

```python
def _dispute_window_minutes_for_match(match):
    base = match.tournament.dispute_window_minutes or DEFAULT_DISPUTE_WINDOW_MINUTES
    if _is_critical_stage_match(match):
        return min(base, CRITICAL_STAGE_DISPUTE_WINDOW_MINUTES)
    return base
```

and make the Test Maker action write `tournament.dispute_window_minutes` instead
of the module globals. Keep the two module constants as defaults only.

**Fix — step 4: require consent for bulk-registering real users.**
`register_existing_to_open_tournament` sweeps up arbitrary existing accounts. At
minimum, restrict its candidate query to accounts created by Test Maker:

```python
                candidates = list(
                    User.objects.filter(
                        is_staff=False, is_superuser=False,
                        username__startswith=settings.TEST_MAKER_USER_PREFIX,
                    )
                    .exclude(individual_registrations__tournament=tournament)
                    .order_by("username", "id")[:existing_count]
                )
```

with `TEST_MAKER_USER_PREFIX = "tm_"` in settings, and have every Test Maker
user-creation path apply that prefix.

**Regression test** — `core/tests_test_maker.py`: with
`@override_settings(ENABLE_TEST_MAKER=False)`, assert `/testing/` returns 404
for a superuser; with it enabled, assert 302 for a non-superuser organizer and
200 for a superuser. Add a test that the dispute window persists on the
tournament rather than a module global.

**Verify:** `python manage.py test core.tests_test_maker`

**Done when:** Test Maker is unreachable in a default (non-DEBUG) deployment,
the dispute window is stored on the model, and bulk registration cannot pull in
real user accounts.

---

### T-5.3 — Stop trusting `X-Forwarded-For` unconditionally

**Severity:** medium · **Finding:** §4.3 · **Files:** `core/audit.py`,
`tournament_manager/settings.py`

**Problem.**

```python
        ip = request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR"))
```

No trusted-proxy check, so any client can set the IP recorded against their own
actions in the audit log — and the same header feeds nothing else, so the audit
trail is the only casualty, but it is the record you would rely on in a dispute.

**Fix.** Add to `settings.py`:

```python
# Number of reverse proxies in front of the app. 0 means X-Forwarded-For is
# untrusted and REMOTE_ADDR is used directly.
TRUSTED_PROXY_COUNT = int(os.environ.get("DJANGO_TRUSTED_PROXY_COUNT", "0"))
```

Rewrite `core/audit.py`:

```python
"""Audit logging utility."""
from django.conf import settings

from .models import AuditLog


def _client_ip(request):
    """Return the client IP, trusting X-Forwarded-For only behind known proxies."""
    if request is None:
        return None
    remote_addr = request.META.get("REMOTE_ADDR")
    proxy_count = getattr(settings, "TRUSTED_PROXY_COUNT", 0)
    if proxy_count <= 0:
        return remote_addr

    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if not forwarded:
        return remote_addr
    # Right-most entries are appended by our own proxies; step back past them.
    parts = [part.strip() for part in forwarded.split(",") if part.strip()]
    if len(parts) < proxy_count:
        return remote_addr
    return parts[-proxy_count] if proxy_count <= len(parts) else remote_addr


def log_action(request, action, details="", tournament=None):
    AuditLog.objects.create(
        user=request.user if request and request.user.is_authenticated else None,
        action=action,
        details=details,
        ip_address=_client_ip(request),
        tournament=tournament,
    )
```

**Regression test** — `core/tests_audit.py`: with `TRUSTED_PROXY_COUNT=0`, send
a request carrying `HTTP_X_FORWARDED_FOR="1.2.3.4"` and assert the logged IP is
`REMOTE_ADDR`, not `1.2.3.4`. With `TRUSTED_PROXY_COUNT=1`, assert the spoofed
left-most entry is not used.

**Also document** in the README deployment section that
`DJANGO_TRUSTED_PROXY_COUNT` must match the actual proxy chain — setting it too
high lets clients spoof again.

---

### T-5.4 — Harden the deployment defaults

**Severity:** high · **Finding:** §4.5 · **File:** `tournament_manager/settings.py`

**Problem.** `DEBUG` defaults to `True` and `SECRET_KEY` falls back to a
hardcoded `django-insecure-change-me-in-production-...` value committed in the
repo. A deployment that forgets `DJANGO_DEBUG=False` runs with debug pages
**and** a publicly known secret key, which makes session cookies and
password-reset tokens forgeable. `SECURE_HSTS_SECONDS`, `SECURE_SSL_REDIRECT`,
`X_FRAME_OPTIONS` and a referrer policy are unset even in the `not DEBUG`
branch.

**Fix.** Replace the top of `settings.py`:

```python
DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() in ("true", "1", "yes")

_DEV_SECRET_KEY = "django-insecure-change-me-in-production-x7k9m2p4q8r1s5t3u6v0w"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", _DEV_SECRET_KEY)

if not DEBUG and SECRET_KEY == _DEV_SECRET_KEY:
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be set to a unique value when DEBUG is False. "
        "Generate one with: python -c \"from django.core.management.utils import "
        "get_random_secret_key; print(get_random_secret_key())\""
    )
```

with `from django.core.exceptions import ImproperlyConfigured` at the top.

Extend the `if not DEBUG:` block:

```python
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    CSRF_COOKIE_SAMESITE = "Lax"
    SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SECURE_SSL_REDIRECT", "True").lower() in (
        "true", "1", "yes",
    )
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    X_FRAME_OPTIONS = "DENY"
```

**Note on `SECURE_PROXY_SSL_HEADER`:** it is only safe when a reverse proxy
always overwrites `X-Forwarded-Proto`. Add a comment saying so — it is already
there, keep it.

**Verify:**

```bash
DJANGO_DEBUG=False DJANGO_SECRET_KEY=$(python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())") \
  DJANGO_ALLOWED_HOSTS=example.com python manage.py check --deploy
```

**Done when:** `check --deploy` reports no `security.W0*` warnings other than
ones you have consciously accepted and documented, and starting with
`DJANGO_DEBUG=False` and no `DJANGO_SECRET_KEY` fails loudly rather than
starting insecurely.

**Regression test** — `core/tests_security.py`: assert that importing settings
with `DEBUG=False` and the dev key raises `ImproperlyConfigured` (use
`importlib.reload` under `mock.patch.dict(os.environ, ...)`).

---

### T-5.5 — Require POST for logout

**Severity:** low · **Finding:** §4.6 · **Files:** `core/views.py`, templates

**Problem.** `logout_view` performs the logout for `GET` as well as `POST`,
outside CSRF protection, so any third-party page can log a user out with an
`<img src="/logout/">`.

**Fix.**

```python
@require_POST
def logout_view(request):
    if request.user.is_authenticated:
        log_action(request, "logout", f"User '{request.user.username}' logged out")
        logout(request)
    return redirect("login")
```

Then convert every logout link to a POST form. Find them:

```bash
grep -rn "url 'logout'" templates/
```

Replace each `<a href="{% url 'logout' %}">` with:

```html
<form method="post" action="{% url 'logout' %}" class="logout-form">
  {% csrf_token %}
  <button type="submit" class="btn btn-link">Log out</button>
</form>
```

**This will break any test that GETs `/logout/`.** Search and update:

```bash
grep -rn "get(\"/logout" core/tests.py
```

**Regression test:** assert `GET /logout/` returns 405 and the session survives;
assert `POST /logout/` logs out.

---

### T-5.6 — Make login throttling survive more than one process

**Severity:** medium · **Finding:** §4.4 · **Files:**
`tournament_manager/settings.py`, `core/views.py`

**Problem.** `login_view` counts attempts per `REMOTE_ADDR` in the default
cache. With no `CACHES` configured this is `LocMemCache` — per-process and wiped
on restart — so the real limit is `5 × worker_count` and resets on every deploy.
The counter is also re-`set` with a fresh 300s TTL on each failure, so the
window slides rather than expiring. There is no per-account lockout, so an
attacker can spray one password across many usernames from one IP and only ever
trip the shared counter.

**Fix — step 1: a real cache backend.** In `settings.py`:

```python
CACHES = {
    "default": {
        "BACKEND": os.environ.get(
            "DJANGO_CACHE_BACKEND",
            "django.core.cache.backends.db.DatabaseCache" if not DEBUG
            else "django.core.cache.backends.locmem.LocMemCache",
        ),
        "LOCATION": os.environ.get("DJANGO_CACHE_LOCATION", "tm_cache_table"),
    }
}
```

If you use `DatabaseCache`, the table must exist:

```bash
python manage.py createcachetable
```

Document that in the README deployment section. Redis is a better choice if
available — note it as the recommended production backend.

**Fix — step 2: fixed window and per-account counter.** In `login_view`:

```python
        ip = request.META.get("REMOTE_ADDR", "unknown")
        username = request.POST.get("username", "").strip()
        ip_key = f"login_attempts_ip_{ip}"
        user_key = f"login_attempts_user_{username.lower()}"

        if django_cache.get(ip_key, 0) >= 10 or django_cache.get(user_key, 0) >= 5:
            messages.error(
                request,
                "Too many failed login attempts. Please wait 5 minutes before trying again.",
            )
            return render(request, "core/login.html")

        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user:
            django_cache.delete(ip_key)
            django_cache.delete(user_key)
            ...
        # Fixed window: only set the TTL on the first failure.
        for key, limit in ((ip_key, 10), (user_key, 5)):
            if django_cache.get(key) is None:
                django_cache.set(key, 1, timeout=300)
            else:
                try:
                    django_cache.incr(key)
                except ValueError:
                    django_cache.set(key, 1, timeout=300)
```

`cache.incr` preserves the existing TTL, which is what makes the window fixed
rather than sliding.

**Regression test** — `core/tests_login_throttle.py`: with
`@override_settings(CACHES={"default": {"BACKEND": "...locmem.LocMemCache",
"LOCATION": "throttle-test"}})`, fail five logins for one username and assert
the sixth is refused even with the *correct* password; assert a different
username from the same IP still works until the IP limit.

**Verify:** `python manage.py test core.tests_login_throttle`

**Done when:** the limit holds across processes (documented backend) and a
per-account counter exists.

---

## Phase 6 — Performance, documentation, hygiene

---

### T-6.1 — Cache or bound `_build_slots`

**Severity:** medium · **Finding:** §5.4 · **File:** `core/scheduling.py`

**Problem.** `_build_slots` materializes one tuple per court per slot per day
over a 365-day fallback horizon for open-ended availability rows.
`count_available_slots` calls it on every `tournament_config` page load and
`_validate_tournament_ready` calls it again on the same request. This is the
main reason the 139-test suite takes 260 seconds, and it is a production
page-load cost, not only a test cost.

**Fix — step 1: stop computing it twice per request.** In `tournament_config`,
compute the slot list once and pass it to both consumers. Give
`count_available_slots` and `estimate_completion_date` an optional
`slots=None` parameter and thread the precomputed list through, rather than each
rebuilding it.

**Fix — step 2: short-circuit the count.** `count_available_slots` only needs a
number, and callers only ever compare it against `required_matches`. Add an
early-exit:

```python
def count_available_slots(tournament, limit=None):
    """Return the number of schedulable slots, stopping early once `limit` is reached."""
    courts = list(tournament.courts.filter(is_available=True))
    if not courts:
        return 0
    slots = _build_slots(tournament, courts, max_slots=limit)
    return len(slots)
```

and give `_build_slots` a `max_slots=None` parameter that breaks out of the day
loop once `len(slots) >= max_slots`. Call sites that compare against
`required_matches` pass `limit=required_matches + 1`.

**Fix — step 3: shrink the fallback horizon.** `open_fallback_days = 365` is
generous for a tournament whose end date is unset. Derive it instead:

```python
    # Enough days to cover the required matches at this tournament's capacity,
    # with headroom — not a fixed year.
    open_fallback_days = int(os.environ.get("TM_OPEN_AVAILABILITY_DAYS", "120"))
```

**DECISION REQUIRED:** shortening the horizon changes `estimate_completion_date`
for tournaments with no end date and sparse availability — they may now report
"not enough availability" where they previously found slots far in the future.
Confirm 120 days is acceptable, or keep 365 and rely on steps 1–2 alone.

**Verify:**

```bash
time python manage.py test core.tests.UXAndLogicRegressionTests
time python manage.py test
```

**Done when:** the full suite runs meaningfully faster (target: under 120s) with
no behavioural test changes, or — if you take only steps 1–2 — the config page
builds the slot list once instead of twice.

---

### T-6.2 — Close the coverage gaps

**Severity:** medium · **Finding:** §5.3 · **File:** `core/tests*.py`

**Problem.** Occurrences in `core/tests.py`: `organizer_remove_team` 0,
`create_backup` 0, `restore_backup` 0, `impersonate` 0, `respond_reschedule` 0,
`accept_team_invite` 0, `seed_participants` 0, `tournament_team_sub` 0. Every
finding in §1 and §3.1–§3.3 sits in one of these untested views.

Most of these gaps are closed by the tests written in earlier tasks. What
remains:

1. **Impersonation** (`impersonate_user` / `stop_impersonating`) — no coverage
   at all, and it manipulates Django's session auth keys by hand. Add
   `core/tests_impersonation.py`:
   - a non-superuser organizer gets redirected, not a switched session;
   - a superuser impersonating a user sees that user's dashboard;
   - `stop_impersonating` restores the original session even if the target's
     password changed mid-session (the view stores
     `impersonating_original_hash` precisely for this — assert it works);
   - `stop_impersonating` with no impersonation in progress is a safe no-op;
   - **check:** `stop_impersonating` has no `@require_POST` and no
     `@login_required`. Assert it cannot be used to escalate from an
     un-impersonated session, and consider adding `@require_POST` as part of
     this task.

2. **A smoke test over every URL.** Add `core/tests_smoke.py` that walks
   `core.urls.urlpatterns`, and for each GET-able route with no required kwargs
   (or with kwargs filled from fixtures), asserts the response is not a 5xx, for
   three personas: anonymous, plain user, organizer. This would have caught
   T-2.1 immediately.

   ```python
   from django.test import TestCase
   from django.urls import reverse, NoReverseMatch
   from core import urls as core_urls


   class UrlSmokeTests(TestCase):
       def test_no_get_route_raises_500(self):
           for pattern in core_urls.urlpatterns:
               name = getattr(pattern, "name", None)
               if not name:
                   continue
               try:
                   path = reverse(name)
               except NoReverseMatch:
                   continue          # needs args — covered by targeted tests
               with self.subTest(route=name):
                   response = self.client.get(path)
                   self.assertLess(
                       response.status_code, 500,
                       f"{name} returned {response.status_code}",
                   )
   ```

   Extend it with an authenticated persona and with routes that take a `pk`,
   supplying fixtures.

**Done when:** every view named above has at least one test, and the smoke test
covers all no-argument GET routes for all three personas.

---

### T-6.3 — Repository hygiene

**Severity:** low · **Finding:** §6.4

Move or remove the following, all committed to the repo root and unreferenced by
the application:

| File | Action |
|---|---|
| `Document A.txt` | Move to `docs/reference-workflows.txt` if it is the spec the app is built against; otherwise delete. |
| `Document B.txt` | Delete — it is LLM prompt scaffolding still containing `[PASTE REFERENCE DOCUMENT HERE]` placeholders. |
| `eam_A,Team_B,Score_A,Score_B,Notes.txt` | Delete — the filename is a truncated shell accident. Inspect the contents first in case it holds real data. |
| `teams.txt`, `username.txt`, `teamnames.txt` | Move to `scripts/fixtures/`. `teams.txt` holds plaintext passwords — note that in `scripts/README.md`. |
| `check_db.py`, `check_knockout.py`, `check_perms.py`, `check_upcoming.py`, `diagnose_get_team.py`, `diagnose_knockout.py`, `diagnose_match_195.py`, `promote_t2p1.py`, `seed_tt1.py`, `verify_completion_feature.py`, `verify_dual_role_toggle.py`, `verify_role_separation.py` | Move to `scripts/`. Several hardcode primary keys (`Tournament.objects.get(pk=12)`) or usernames (`t2p1`) from one developer's database — say so in `scripts/README.md`. |
| `.vscode/settings.json` | `.gitignore` already lists `.vscode/` but this file is tracked. Run `git rm --cached .vscode/settings.json`. It auto-approves `django-admin` and `&` for an editor agent, which should be a personal setting, not a repo one. |

Use `git mv` so history is preserved. Verify nothing imports the moved modules:

```bash
grep -rn "import check_db\|import seed_tt1\|import promote_t2p1" . --include=*.py
python manage.py test 2>&1 | tail -3
```

**Done when:** the repo root contains only `manage.py`, `requirements.txt`,
`README.md`, the documentation files, and the two Django packages.

---

### T-6.4 — Pin dependencies

**Severity:** low · **Finding:** §6.4

`requirements.txt` contains only `django>=4.2` with no upper bound. The review
ran against Django 5.2.17; a future 6.x will install silently and may break.

```
django>=5.2,<5.3
```

Add a `requirements-dev.txt` for anything the test suite needs beyond Django,
and note the tested Python version (3.11) in the README.

**Done when:** a fresh `pip install -r requirements.txt` produces the version
the suite was verified against.

---

### T-6.5 — Rewrite `DUAL_ROLE_TOGGLE_FEATURE.md`

**Severity:** low · **Finding:** §6.1

The document contradicts the code in three places:

1. It states dual-role detection is `is_staff=True`. The code is
   `_has_dual_roles(user)` → `_is_organizer(user) and user.memberships.exists()`,
   where `_is_organizer` is
   `is_superuser or is_staff or organizer_profile.verified`. The "Edge Cases
   Handled" section repeats the `is_staff` claim twice.
2. It states "Dashboard detects view_mode='organizer' and redirects to
   tournament_setup", with an ASCII flow diagram to match. `dashboard_view` does
   no such redirect — it computes `effective_view` and passes it to the
   template, which renders the appropriate blocks in one page.
3. The documented verification path (`python promote_t2p1.py`) sets `is_staff`
   rather than creating a verified `OrganizerProfile`, i.e. it exercises a code
   path that is no longer the primary organizer signal.

**Fix.** Rewrite the Detection, View Routing and Backend Logic Flow sections
against the current `_has_dual_roles`, `_is_organizer` and `dashboard_view`.
Replace the flow diagram with:

```
_is_organizer(user) and user.memberships.exists() -> has_dual_roles = True
                    |
                    v
dashboard_view computes effective_view:
    has_dual_roles  -> session["view_mode"]  ('team' | 'organizer', default 'team')
    organizer only  -> 'organizer'
    otherwise       -> 'team'
                    |
                    v
dashboard.html renders the matching blocks (no redirect)
```

Update the verification instructions to promote via the Settings page or by
creating `OrganizerProfile(user=..., verified=True)`, and point at
`scripts/promote_t2p1.py` (new path after T-6.3) as a legacy helper.

**Done when:** every claim in the document is checkable against current code.

---

### T-6.6 — Correct and extend the README

**Severity:** low · **Finding:** §6.2, §6.3

**Corrections** (§6.2) — three claims are currently wrong:

1. Format table: "Double Elimination — Winners and losers brackets." Update to
   whatever T-4.4 settles on.
2. "Organizer tools: analytics, backups, audit log" — accurate only once T-3.2
   and T-1.2 land. Re-check the wording afterwards.
3. "a team can only be entered when its member count is exactly equal to the
   tournament players-per-team value" — accurate only once T-4.2 lands.

**Additions** (§6.3) — the README is missing:

1. **Configuration reference.** `settings.py` reads `DJANGO_SECRET_KEY`,
   `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, and
   (after this plan) `DJANGO_BACKUP_DIR`, `DJANGO_ENABLE_TEST_MAKER`,
   `DJANGO_TRUSTED_PROXY_COUNT`, `DJANGO_CACHE_BACKEND`,
   `DJANGO_SECURE_SSL_REDIRECT`, `DJANGO_HSTS_SECONDS`. None are documented.
   Add a table: variable, default, what it does.

2. **A deployment section.** The quick start ends at
   `python manage.py runserver 0.0.0.0:8000`, which is a development server and
   should not face a network. Document: generate a secret key, set
   `DJANGO_DEBUG=False`, set `ALLOWED_HOSTS`, run `collectstatic`, run
   `createcachetable`, serve through a WSGI server behind a reverse proxy, and
   run `manage.py check --deploy`.

3. **Undocumented features.** Notifications, organizer applications and
   approval, impersonation, participant seeding, substitutes, Test Maker, and
   the public tournament list/detail pages all exist and none are mentioned.

4. **Project layout.** The block lists `core/audit.py` but omits
   `core/services/`, `core/templatetags/`, `core/management/commands/` and
   `core/context_processors.py`. It also omits `core/models.py`'s siblings
   `core/admin_config.py` and `core/admin.py`.

5. **Backup compatibility note.** After T-1.2, pre-v2 backups cannot be
   restored. Say so.

**Done when:** every claim in the README is true of the code at that commit, and
a new operator can deploy from the README alone.

---

## 7. Final verification

Run this after the last task. All of it must pass before calling the work done.

```bash
# 1. Full suite green, no reduction in test count
python manage.py test 2>&1 | tail -3

# 2. No missing migrations
python manage.py makemigrations --check --dry-run

# 3. Deployment checks clean
DJANGO_DEBUG=False \
DJANGO_SECRET_KEY=$(python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())") \
DJANGO_ALLOWED_HOSTS=example.com \
  python manage.py check --deploy

# 4. No secrets tracked
git ls-files backups | wc -l          # expect 0
git ls-files | grep -iE "\.env|password|secret"   # expect nothing unexpected

# 5. No references to removed schema
grep -rn "preferred_courts\|captained_teams\|team\.tournament\b\|team\.user\b" core/ templates/

# 6. Test discovery is clean — no stray test*.py in the root
ls test*.py 2>/dev/null                # expect: no matches

# 7. Data integrity commands report no drift
python manage.py audit_participant_integrity
```

### Acceptance checklist

| Task | Title | Status |
|---|---|---|
| T-0.1 | Expired date removed from availability test | ☐ |
| T-0.2 | Root-level `test*.py` moved out of discovery | ☐ |
| T-0.3 | Green baseline recorded | ☐ |
| T-1.1 | Backups untracked, ignored, moved out of tree | ☐ |
| T-1.2 | Backup/restore fixed and non-destructive | ☐ |
| T-2.1 | `organizer_remove_team` no longer 500s | ☐ |
| T-3.1 | Tournament ownership + admin/organizer split | ☐ |
| T-3.2 | Analytics and audit log restricted | ☐ |
| T-3.3 | `dispute_score` participant check | ☐ |
| T-3.4 | Open redirect closed | ☐ |
| T-3.5 | `_can_manage_reschedule` verifies the competitor | ☐ |
| T-4.1 | Court preferences persisted at team creation | ☐ |
| T-4.2 | Invite path enforces roster rules | ☐ |
| T-4.3 | Reschedule responses idempotent + re-checked | ☐ |
| T-4.4 | Double elimination honest or implemented | ☐ |
| T-4.5 | Head-to-head tiebreaker implemented | ☐ |
| T-4.6 | Individual registration sync | ☐ |
| T-4.7 | JSON seeding fixed | ☐ |
| T-4.8 | Substitutes scoped | ☐ |
| T-4.9 | Availability date warning extended | ☐ |
| T-4.10 | Small correctness batch (11 items) | ☐ |
| T-5.1 | Password validators applied everywhere | ☐ |
| T-5.2 | Test Maker gated | ☐ |
| T-5.3 | `X-Forwarded-For` trust bounded | ☐ |
| T-5.4 | Deployment defaults hardened | ☐ |
| T-5.5 | Logout requires POST | ☐ |
| T-5.6 | Login throttling durable | ☐ |
| T-6.1 | `_build_slots` cost reduced | ☐ |
| T-6.2 | Coverage gaps closed | ☐ |
| T-6.3 | Repository hygiene | ☐ |
| T-6.4 | Dependencies pinned | ☐ |
| T-6.5 | Dual-role doc rewritten | ☐ |
| T-6.6 | README corrected and extended | ☐ |

### Decision log

Record each `DECISION REQUIRED` outcome here as it is made, so the reasoning
survives the branch:

| Task | Question | Decision | Decided by | Date |
|---|---|---|---|---|
| T-1.1 | History purge + password rotation | | | |
| T-3.1 | Ownership policy (a/b/c) | | | |
| T-4.4 | Double elimination: downgrade or implement | | | |
| T-4.4 | Grand final: bracket reset or single match | | | |
| T-4.8 | Substitutes: scope the model or counts only | | | |
| T-6.1 | Open-availability horizon: 120 or 365 days | | | |

### What is deliberately out of scope

State these explicitly so nobody assumes they were handled:

- Rotating the leaked password hashes (human action — T-1.1).
- Purging git history (human action — T-1.1).
- A full double-elimination implementation, unless Option B was chosen (T-4.4).
- Migrating off SQLite. `DatabaseCache` and `select_for_update` behave
  differently on SQLite; several concurrency fixes here are correct but inert
  until the database is Postgres.
- Rate limiting anything other than login.
- Any change to the HTMX front-end beyond the template edits named in the tasks.
