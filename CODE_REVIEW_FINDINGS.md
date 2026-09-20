# Code & Documentation Review — Findings

Review date: 2026-09-19
Scope: `core/` (models, views, forms, scheduling, standings, withdrawals, backup, urls),
`tournament_manager/settings.py`, repository root, `README.md`,
`DUAL_ROLE_TOGGLE_FEATURE.md`.

Findings marked **[verified]** were reproduced by running code against a test
database (Django 5.2, `manage.py test`). Findings marked *[by inspection]* were
identified by reading the code and are not covered by an executed reproduction.

Baseline: `python manage.py test` → **139 tests, 1 failure, 1 error** (260s).

---

## 1. Critical

### 1.1 Committed database backups leak password hashes and a real email address **[verified]**

`backups/` is tracked by git (11 files, 1.8 MB) and `.gitignore` covers
`db.sqlite3` but not `backups/`. Each file is a full serialization of
`auth.user`:

```
users: 51
sample fields: {'password': 'pbkdf2_sha256$1200000$lQK...', 'is_superuser': True,
                'username': 'admin', 'email': 'simarsamra@gmail.com', ...}
```

Every account's password hash — including the superuser's — plus the owner's
personal email address are in the repository history, and `settings.BACKUP_DIR`
points at this same directory so new backups land there too.

**Fix:** add `backups/` to `.gitignore`, remove the files from history
(`git filter-repo` / BFG), rotate every account password, and move
`BACKUP_DIR` outside the working tree.

Related: `teams.txt` at the repo root contains plaintext seed passwords
(`Alpha Squad,team9,pass123,...`), and `username.txt` / `teamnames.txt` are
loose data dumps of real-looking tournament participants.

### 1.2 Backup creation is broken and restore destroys data that is not backed up **[verified]**

`core/backup.py:39-41`:

```python
for team in Team.objects.all():
    m2m_data[team.id] = list(team.preferred_courts.values_list("id", flat=True))
```

`Team.preferred_courts` was removed by the global-team schema migration; court
preferences now live in `TeamTournamentCourtPreference`. Reproduced:

```
create_backup FAILED: AttributeError 'Team' object has no attribute 'preferred_courts'
```

(With zero `Team` rows the loop body never runs, which is why this has gone
unnoticed.) This breaks `create_backup_view` *and* `restore_backup_view`,
which calls `create_backup(is_auto=True)` for its pre-restore safety copy.

Worse, `BACKUP_MODELS` (`core/backup.py:19-22`) lists 11 models but the app has
25. Missing: `TeamMembership`, `TeamTournamentParticipation`,
`TournamentIndividualRegistration`, `TeamTournamentCourtPreference`, `Player`,
`Notification`, `TeamInvite`, `OrganizerProfile`, `OrganizerApplication`,
`UserTeamAssignment`, `NoShowReport`, `TeamRegistration`,
`IndividualRegistration`. `restore_backup()` runs
`model.objects.all().delete()` over `Team`, `Tournament` and `User`, which
cascades into every one of those unbacked tables. **A restore permanently
destroys all rosters, registrations and notifications**, and deletes every user
account including the one performing the restore. The committed backups confirm
this: they carry `_m2m_team_preferred_courts` and no membership data at all.

**Fix:** drop the `preferred_courts` block, add every model to `BACKUP_MODELS`
in dependency order, and add a test that round-trips a populated database.

### 1.0 Verified organizer grants are revoked on the organizer's next login **[verified]**

*Found after the initial review, while fixing §1.2. `core/apps.py` was not read
in the first pass.*

`core/apps.py` registered a `post_save` handler on `User` that re-synced
`OrganizerProfile.verified` to `instance.is_staff` on every non-creation save:

```python
        elif not created:
            org_profile = OrganizerProfile.objects.get(user=instance)
            if org_profile.verified != instance.is_staff:
                org_profile.verified = instance.is_staff
                org_profile.save(update_fields=["verified"])
```

Reproduced:

```
PROBE after grant   -> verified: True  _is_organizer: True
PROBE after login   -> verified: False _is_organizer: False
PROBE after profile save -> verified: False
PROBE save with no profile -> raised DoesNotExist
```

Django's `update_last_login` does `user.save(update_fields=["last_login"])` on
every login, which fires `post_save` with `created=False` and resets `verified`
to `False` for any organizer who is not `is_staff`. `_is_organizer` then returns
`False`.

This means the entire organizer approval flow — `organizer_apply_view` →
`review_organizer_application` → `OrganizerProfile.verified = True`, and
`set_user_organizer` — has only ever worked for `is_staff` accounts. A user
promoted through the UI loses the grant the moment they log in. The same happens
on any profile update or password change. It also explains why
`DUAL_ROLE_TOGGLE_FEATURE.md` and `promote_t2p1.py` both talk in terms of
`is_staff` (§6.1).

Third defect in the same handler: `OrganizerProfile.objects.get(user=instance)`
raises `DoesNotExist` — an unhandled 500 on `user.save()` — for any user whose
profile row is missing.

**Fixed** in `[T-2.2]`: handlers moved to `core/signals.py`, the `elif` branch
removed (`verified` is seeded at creation and then left alone; `_is_organizer`
already treats `is_staff` as sufficient on its own).

### 1.3 `organizer_remove_team` raises `AttributeError` on every call **[verified]**

`core/views.py:4457-4489` uses three attributes that no longer exist:

```python
tournament = team.tournament          # Team has no tournament FK
captain_user = team.user              # Team has no user FK
if not captain_user.captained_teams.exists():   # no such related name
```

```
PROBE organizer_remove_team -> raised AttributeError 'Team' object has no attribute 'tournament'
```

The route is live (`core/urls.py:78`) and reachable from two templates
(`templates/core/partials/team_detail_content.html:40`,
`templates/core/teams.html:75`), so any organizer clicking "Remove team" gets a
500. The view has zero test coverage. `remove_team_from_tournament` is the
working equivalent.

---

## 2. Authorization and access control

### 2.1 Any tournament can be managed by any organizer

`Tournament` has no owner/creator field. Every organizer-gated view authorizes
with `_is_organizer(request.user)` alone and then `get_object_or_404(Tournament,
pk=pk)` — no ownership check anywhere. Any verified organizer can configure,
start, pause, cancel, duplicate, **delete**, or disqualify teams from any other
organizer's tournament, and can reset passwords for members of any team.
`organizer_public_page` has to reverse-engineer authorship from `AuditLog`
rows (`core/views.py:6626-6640`) precisely because the ownership link does not
exist.

**Fix:** add `Tournament.created_by` (FK to `User`) and a
`_can_manage_tournament(user, tournament)` helper used by all organizer views.

### 2.2 Analytics and the full audit log are readable by any logged-in user **[verified]**

`analytics_view` (`core/views.py:4666`) and `audit_log_view`
(`core/views.py:5089`) carry only `@login_required`. Neither checks
`_is_organizer` nor enrollment. `_get_tournament()` falls through to
`return _get_available_tournaments().first()` for a user enrolled in nothing,
so an unrelated account gets handed an arbitrary tournament:

```
PROBE non-participant analytics: 200 audit-log: 200
```

`audit_log_view` additionally includes global rows (`Q(tournament__isnull=True)`),
exposing login events, `member_password_reset`, `user_deleted`,
`impersonation_started`, and client IP addresses. README lists both pages under
"Organizer tools".

### 2.3 Any tournament participant can dispute a match they are not in **[verified]**

`dispute_score` (`core/views.py:3767-3776`) checks only that the user has a team
and did not submit the score. Unlike `confirm_score`, it never checks that the
user's team is `match.team1` or `match.team2`:

```
PROBE dispute-by-outsider -> status: disputed disputed_by: u2
```

A rival can freeze any pending result in `disputed` state, forcing organizer
intervention and blocking bracket progression.

**Fix:** add the same `if match.team1 != team and match.team2 != team` guard
`confirm_score` uses.

### 2.4 Open redirect in `select_tournament` **[verified]**

`core/views.py:2778-2795`:

```python
next_url = request.POST.get("next") or "dashboard"
...
return redirect(next_url)
```

`django.shortcuts.resolve_url` passes any string containing `/` or `.` through
unchanged:

```
PROBE select_tournament redirect -> 302 https://evil.example/pwn
```

**Fix:** validate with `django.utils.http.url_has_allowed_host_and_scheme`, or
restrict `next` to a whitelist of view names.

### 2.5 Organizers can escalate and destroy each other

`set_user_organizer`, `delete_user_account`, `toggle_user_suspension` and
`review_organizer_application` all gate on `_is_organizer` only. Any verified
organizer can promote arbitrary users to organizer, approve pending organizer
applications, suspend or permanently delete any non-superuser account
(including other organizers), and `settings_view` hands every organizer the
full `User` list. The only backstop is "at least one organizer must remain".
These are admin-level powers behind an organizer-level check.

### 2.6 Captains can take over member accounts

`reset_member_password` (`core/views.py:4302`) lets a captain set any
non-captain teammate's password to an arbitrary value with no notification to
the account holder. Because a user who self-joins via `join_team_view` or
accepts an invite becomes an ordinary `member`, a captain can lock that real
user out and log in as them. `reset_captain_password` gives the same power to
every organizer, for every team. The minimum length enforced here is 6
characters, bypassing `AUTH_PASSWORD_VALIDATORS`.

### 2.7 `_can_manage_reschedule` returns `True` for any authenticated user in individual mode

`core/views.py:391-401` short-circuits to `True` whenever
`tournament.registration_mode == "individual"`, with no check that the user is
the competitor. The two call sites in `request_reschedule` / `respond_reschedule`
happen to check participation first, but `match_detail` uses the result directly
for `can_reschedule`, and the helper is unsafe to reuse as written.

---

## 3. Correctness bugs

### 3.1 Court preferences chosen at team creation are silently discarded **[verified]**

`CreateTeamForm` declares `preferred_courts` and makes it **required** when the
tournament has courts (`core/forms.py:296-299`, plus a `clean()` that rejects an
empty selection). `create_team_view` reads `team_name`, `department` and
`participant_name` and never touches `preferred_courts`.

```
PROBE create_team status: 200 | team created: True
PROBE stored court preferences: 0
PROBE readiness errors mentioning preferences: ['These teams still need court preferences: Alpha.']
```

The captain is forced to pick courts, the selection is dropped, and the
tournament then refuses to start on exactly that missing data. Every team must
re-enter preferences through `team_preferences`.

### 3.2 Team invites bypass the roster cap and the one-team-per-tournament rule **[verified]**

`accept_team_invite` (`core/views.py:5817`) checks only for an existing
membership on that one team. `join_team_view` checks team fullness *and*
`_is_user_enrolled_in_tournament`; the invite path checks neither.

```
PROBE m1 teams in tournament after accepting 2nd invite: ['B', 'A']
PROBE team A size vs players_per_team=2 -> 4
```

A player ends up on two competing teams in the same tournament, and a team can
exceed `players_per_team`. README states the rule as "a team can only be entered
when its member count is exactly equal to the tournament players-per-team
value". `close_registration` then blocks with "Mismatched teams", with no way to
trim the roster except manual removal.

### 3.3 Reschedule requests can be re-answered after they are applied **[verified]**

`respond_reschedule` (`core/views.py:4000`) never checks `rr.status == "pending"`:

```
PROBE reschedule re-respond -> status after approve+reject: rejected | match time unchanged: True
```

The request flips to `rejected` while the match keeps the rescheduled time — the
audit trail and the schedule disagree. Related: conflict detection runs at
*request* time only (`request_reschedule`), never at approval time, so two
requests created against the same free slot can both be approved and
double-book a court.

### 3.4 Double elimination has no losers bracket

`generate_double_elimination` (`core/scheduling.py:641-647`) generates a winners
bracket and comments "Losers bracket matches are created dynamically as teams
are eliminated" — nothing in the codebase ever creates a match with
`bracket_type="losers"`. `advance_winner` has no losing-side counterpart. The
format is single elimination in practice, while README advertises "Double
Elimination: Winners and losers brackets."

`estimate_required_matches` nonetheless reserves `2n-2` slots for the format
(`core/scheduling.py:725-726`), so `_validate_tournament_ready` demands roughly
double the court availability that will actually be used.
`DoubleEliminationBracketTests` only asserts winners-bracket behaviour.

### 3.5 Head-to-head tiebreaker is a no-op

`_sort_key` (`core/standings.py:104-116`):

```python
elif tb == "head_to_head":
    key.append(0)  # Simplified; would need pairwise comparison
```

`Tournament.tiebreaker_order` defaults to
`["game_diff", "games_won", "head_to_head"]`, so every tournament ships with a
configured tiebreaker that does nothing. Ties past `games_won` resolve by
arbitrary dict ordering.

### 3.6 Individual registration status desyncs from the shadow-team participation

`TournamentIndividualRegistration` and its `shadow_team`'s
`TeamTournamentParticipation` are kept in sync only by
`_ensure_shadow_team_for_registration`. Three views change one side without it:

- `approve_registration` / `reject_registration` (`core/views.py:6096`, `6143`)
  set `reg.status`; the shadow participation — which is what the match engine
  and `_validate_tournament_ready` read — keeps its old value. A rejected
  individual still gets scheduled.
- `disqualify_team` (`core/views.py:6302`) does the reverse: it withdraws the
  participation and leaves the registration `active`, so the player still shows
  up in participant lists and `active_participant_count`.
- `reject_registration` also never sets `withdrawn_at`.

A `reconcile_participant_integrity` management command exists to clean this up
after the fact, which suggests the drift is known.

### 3.7 JSON seed submission silently does nothing

`seed_participants_view` (`core/views.py:6821-6827`) parses JSON into
`seeds = data.get("seeds", {})` — JSON object keys are strings — then looks them
up with an integer key: `seeds.get(p.pk)`. No seed is ever matched. The form-POST
path (`int(key[5:])`) works. The view reports "Seeds saved." either way.

### 3.8 Substitutes are global team members, not per-tournament

`tournament_team_sub_view` documents "participate in this tournament's matches
without being a permanent member", but it creates a plain `TeamMembership` with
`role="sub"` — `TeamMembership` has no tournament scope. The sub joins the team
in *every* tournament it competes in, counts toward `team.memberships.count()`
(used for the exact-roster check in `close_registration`,
`_validate_tournament_ready`, `_promote_team_participation_when_full` and
`_check_roster_minimum`), and `_get_team` will return that team for them, so
they can submit and confirm scores anywhere.

### 3.9 `_get_user_tournament_ids` can return `None`

`core/views.py:218-232` builds IDs from
`values_list("team__participations__tournament_id")`. A membership in a team
with no participations yields `None`, which lands in the set. `_tournament_context`
then triggers the multi-tournament switcher on `len(...) > 1` for a user
enrolled in a single tournament, and `Tournament.objects.filter(pk__in=ids)`
receives a `NULL`.

### 3.10 Availability entries dated before the tournament start silently yield zero slots

`_build_slots` clamps `range_start = max(base_date, availability.start_date or
base_date)` where `base_date = tournament.start_date or localdate()`. If an
availability window ends before the tournament start date, the day loop never
executes and the entry contributes nothing — with no warning. The
`availability_date_warning` built in `tournament_config`
(`core/views.py:1986-2004`) explicitly `continue`s past any entry that *has* an
`end_date`, i.e. exactly the entries that can fail this way.

This is the cause of the currently failing test (see §5.1).

### 3.11 Smaller correctness issues

- `submit_score` (`core/views.py:3641-3643`): the `status == "completed"` check
  is dead code — `allowed_tournament_statuses` has already rejected it.
- `resolve_dispute` (`core/views.py:3812`): if `final_score_team1`/`2` are
  absent the entire body is skipped and the view redirects with no message and
  no state change — a dispute cannot be resolved without restating the score.
- `override_match_result` never calls `_check_and_finalize_tournament`, so an
  override that completes the last match leaves the tournament `active`.
- `_lock_match_score` sets `match.confirmed_by = confirmed_by_user`
  unconditionally, so an auto-lock (`confirmed_by_user=None`) erases a
  previously recorded confirmer.
- `duplicate_tournament` (`core/views.py:6256`) omits `enable_third_place_match`
  and `matches_per_court_per_day` from the copied fields, and copies no courts
  or availability.
- `add_timeslot` (`core/views.py:2369`) discards form errors entirely — an
  invalid submission redirects with no feedback.
- ~~`CourtAvailabilityForm.is_active` uses `initial=True` on a
  `BooleanField(required=False)`; `initial` does not apply to bound forms, so an
  unchecked box creates an inactive availability row.~~ **Retracted — not a
  bug.** Verified while fixing T-4.10: `tournament_config` builds the form
  unbound, so the checkbox renders `checked` and the default really is active.
  Unchecking it is a deliberate choice, and a bound form only occurs on
  re-render after a validation error, where the submitted value is correct.
- `create_team_view` / `create_standalone_team_view` check
  `Team.objects.filter(name__iexact=...)` then `create()` against a `unique`
  column — the race raises an unhandled `IntegrityError`.
- `user_public_profile` counts wins as
  `Match.objects.filter(winner_id__in=team_ids, status="confirmed")` across all
  of the user's *current* teams, crediting matches played before they joined.
- `analytics_view` buckets `schedule_density` with
  `m.scheduled_time.strftime(...)` on the raw UTC datetime instead of
  `timezone.localtime`, unlike the rest of the codebase.

---

## 4. Security hardening

### 4.1 Registration accepts passwords of any length

`AccountRegistrationForm.clean()` (`core/forms.py:212-219`) checks only that the
two fields match and the username is free. It never calls
`django.contrib.auth.password_validation.validate_password`, and neither does
`TeamMemberInviteForm` or the bulk-team CSV importer. `AUTH_PASSWORD_VALIDATORS`
in settings (`MinimumLengthValidator`, default 8) is therefore never applied
anywhere in the app — a one-character password is accepted at signup, while
`SelfPasswordChangeForm` and the captain reset paths enforce an inconsistent
hand-rolled minimum of 6.

### 4.2 Test Maker is a production-reachable data generator

`test_maker_view` is gated on `_is_organizer` only and is routed at `/testing/`.
It creates real `User` accounts with a default password of `pass123`,
mass-registers **existing real users** into a tournament without consent
(`register_existing_to_open_tournament`), and randomizes/confirms live match
scores. It should be behind `DEBUG`, a feature flag, or superuser-only.

`set_dispute_window` additionally mutates module globals at runtime:

```python
import core.views as _self
_self.DEFAULT_DISPUTE_WINDOW_MINUTES = minutes
```

This is per-process (inconsistent across gunicorn workers), lost on restart, and
invisible to the audit trail as configuration.

### 4.3 Audit log IP addresses are attacker-controlled

`log_action` (`core/audit.py:8-11`) trusts `HTTP_X_FORWARDED_FOR` with no
trusted-proxy check, so any client can set the IP recorded against their own
actions.

### 4.4 Login throttling is weak

`login_view` counts attempts per `REMOTE_ADDR` in the default cache. With no
`CACHES` configured this is `LocMemCache` — per-process and wiped on restart, so
the limit is effectively `5 × worker_count` and resets on every deploy. The
counter is also `set` with a fresh 300s TTL on each failure, so the window
slides rather than expiring. There is no per-account lockout.

### 4.5 Insecure-by-default settings

`settings.py` defaults `DEBUG=True` and ships a hardcoded
`django-insecure-change-me-in-production-...` `SECRET_KEY` fallback. A deployment
that forgets `DJANGO_DEBUG=False` runs with debug pages **and** the secret key
in the repository, which makes session cookies and password-reset tokens
forgeable. `SECURE_HSTS_SECONDS`, `SECURE_SSL_REDIRECT` and
`X_FRAME_OPTIONS`/referrer policy are unset even in the `not DEBUG` branch.

### 4.6 `logout_view` logs out on GET

`core/views.py:1095-1103` performs the logout for `GET` as well as `POST`,
outside CSRF protection. Any third-party page can log a user out.

---

## 5. Test suite

### 5.1 The suite is red — a hardcoded date has expired **[verified]**

```
FAIL: test_add_court_availability_supports_additional_start_times
  File "core/tests.py", line 220
    self.assertEqual(count_available_slots(tournament), 2)
AssertionError: 0 != 2
```

The test posts an availability window for `2026-05-04` only, but the tournament
is created with `start_date` defaulting to `timezone.localdate()` (today). Per
§3.10, `_build_slots` clamps to the later date and produces zero slots. The test
passed when written and fails permanently from 2026-05-05 onward. Fix by setting
the tournament's `start_date` explicitly in the test, and add the missing
organizer-facing warning for date-bounded availability rows.

### 5.2 A root-level script breaks test discovery **[verified]**

`test_delete_fix.py` sits in the repo root, matches Django's `test*.py` discovery
pattern, and runs ORM queries at import time:

```
File "test_delete_fix.py", line 17, in <module>
    tournament = Tournament.objects.first()
django.db.utils.OperationalError: no such table: core_tournament
```

This is counted as an error on every single test run. Move it under a
`scripts/` directory or rename it.

### 5.3 Coverage gaps line up with the broken code

Occurrences in `core/tests.py`: `organizer_remove_team` 0, `create_backup` 0,
`restore_backup` 0, `impersonate` 0, `respond_reschedule` 0,
`accept_team_invite` 0, `seed_participants` 0, `tournament_team_sub` 0. Every
finding in §1 and §3.1–§3.3 sits in one of these untested views.

### 5.4 The suite takes 260 seconds for 139 tests

> **Corrected 2026-09-20 (T-6.1).** The attribution below was wrong, and it is
> left in place rather than quietly rewritten because the correction is the
> useful part.
>
> The claim was that `_build_slots` dominated the runtime. Measured: after
> optimising slot building, a 247-test run took **197.1s** against a **198.2s**
> baseline — no meaningful change. The real cost is **PBKDF2 password
> hashing**. The suite creates hundreds of users and logs them in, and Django's
> default hasher is deliberately expensive. `core.tests_impersonation` alone
> went from 16.8s to 0.3s under a fast hasher; the full suite went from 198s to
> **7.6s**.
>
> The lesson is narrow but real: "this loop looks expensive" is a hypothesis,
> not a measurement. Nothing in the original finding was based on a profile.

The original claim, for the record:

Most of the cost is `_build_slots`, which materializes a tuple per court per
slot per day over a 365-day fallback horizon for open-ended availability rows.
`count_available_slots` calls it on every `tournament_config` page load and
`_validate_tournament_ready` calls it again — this is a production page-load
cost, not only a test cost.

Two secondary claims there were also imprecise: `tournament_config` calls
`count_available_slots` **once**, not twice. The genuine double-build is in
`generate_schedule` and `start_tournament`, which run
`_validate_tournament_ready` and then `generate_fixtures`, each building the
full slot list.

The slot-building work was kept regardless, because the per-request cost is
real even though it was not the suite's bottleneck: the date walk now steps
weekday-to-weekday instead of discarding six days in seven, extra start times
are parsed once per availability row rather than once per matching day, and
the readiness check stops counting once it reaches `required_matches`.

### 5.5 Impersonation has no tests, and its central comment is backwards **[verified]**

`impersonate_user` and `stop_impersonating` hand-edit Django's session auth
keys (`_auth_user_id`, `_auth_user_backend`, `_auth_user_hash`) and had zero
test coverage.

The comment on the stored hash claimed:

> Without this, if the admin's password changes during the impersonation
> session, Django would invalidate the session when we try to restore it.

It is the reverse. Storing the admin's hash *as it was* and restoring it later
is precisely what makes Django reject the session after a password change —
verified: the admin is redirected to `/login/` and the session is flushed.
Without the stored hash, the fallback recomputes a fresh hash and the session
would survive.

**The behaviour is correct; only the comment was wrong.** A credential
rotation must not be survivable by resuming a suspended session, so restoring
the stale hash is the safer of the two. Fixed by correcting the comment and
pinning both rotation paths with tests (T-6.2).

Separately, `stop_impersonating` had no `@require_POST`, so a third-party page
could end an admin's impersonation via a GET. It cannot escalate — the session
key it reads can only be set by the superuser-only `impersonate_user` — but it
is now POST-only. It deliberately still has no `@login_required`: the
impersonated account may be deactivated mid-session, and the admin must still
be able to get out.

---

## 6. Documentation

### 6.1 `DUAL_ROLE_TOGGLE_FEATURE.md` no longer matches the code

- States dual-role detection is `is_staff=True`; the code is
  `_is_organizer(user)`, which is `is_superuser or is_staff or
  organizer_profile.verified`. The doc's "Edge Cases Handled" section repeats
  the `is_staff` claim twice.
- States "Dashboard detects view_mode='organizer' and redirects to
  tournament_setup", with an ASCII flow diagram to match.
  `dashboard_view` does no such redirect — it computes `effective_view` and
  passes it to the template, which renders both blocks in one page.
- The documented verification path (`python promote_t2p1.py`) sets `is_staff`
  rather than creating a verified `OrganizerProfile`, i.e. it tests a code path
  the app no longer uses as its primary organizer signal.

### 6.2 README overstates three behaviours

- "Double Elimination — Winners and losers brackets": no losers bracket exists
  (§3.4).
- "Organizer tools: analytics, backups, audit log": analytics and the audit log
  are open to every authenticated user (§2.2); backups crash (§1.2).
- "a team can only be entered when its member count is exactly equal to the
  tournament players-per-team value": enforced in `enter_existing_team_view` and
  `join_team_view`, but not in `accept_team_invite` (§3.2).

### 6.3 README omissions

No mention of the `DJANGO_SECRET_KEY` / `DJANGO_DEBUG` / `DJANGO_ALLOWED_HOSTS`
/ `DJANGO_CSRF_TRUSTED_ORIGINS` environment variables that `settings.py` reads,
no deployment section (the quick start ends at `runserver 0.0.0.0:8000`, which
is a development server), and no documentation of the notification, organizer
application, impersonation, seeding, substitute or Test Maker features. The
"Project Layout" block lists `core/audit.py` but omits `core/models.py`'s
companions `core/services/`, `core/templatetags/` and `core/management/`.

### 6.4 Repository hygiene

Committed to the repo root and unreferenced by the application:

| File | Issue |
|---|---|
| `Document A.txt`, `Document B.txt` | LLM prompt scaffolding; B still contains `[PASTE REFERENCE DOCUMENT HERE]` placeholders |
| `eam_A,Team_B,Score_A,Score_B,Notes.txt` | Filename is a truncated shell accident |
| `teams.txt`, `username.txt`, `teamnames.txt` | Loose seed/scratch data; `teams.txt` holds plaintext passwords |
| `check_db.py`, `check_knockout.py`, `check_perms.py`, `check_upcoming.py`, `diagnose_*.py`, `promote_t2p1.py`, `seed_tt1.py`, `verify_*.py`, `test_delete_fix.py` | 13 ad-hoc scripts, several hardcoding primary keys (`Tournament.objects.get(pk=12)`) or usernames (`t2p1`) |
| `.vscode/settings.json` | Committed despite `.gitignore` listing `.vscode/` — auto-approves `django-admin` and `&` for an editor agent |

`requirements.txt` pins only `django>=4.2` with no upper bound; the review ran
against Django 5.2.

---

## Suggested order of work

1. §1.1 secrets in git — rotate credentials, purge history, ignore `backups/`.
2. §1.2 backup/restore — the restore path is currently destructive.
3. §1.3, §3.1, §3.2, §3.3 — user-visible breakage with reproductions in hand.
4. §2.1–§2.4 — authorization model, starting with `Tournament.created_by`.
5. §5.1, §5.2 — get the suite green so the rest can be regression-tested.
6. §3.4, §3.5 — either implement or stop advertising in README.
7. §6 — reconcile documentation with the code.
