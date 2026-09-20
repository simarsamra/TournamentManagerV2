# Tournament Manager

Tournament Manager is a Django web app for running sports tournaments (table
tennis by default) on a local network. Organizers configure tournaments,
schedules and rules; players register, join teams, submit scores and manage
match workflows.

Tested against **Django 5.2 LTS on Python 3.11**.

## Highlights

- Multiple formats: round robin, double round robin, knockout, consolation and
  hybrid (double elimination is present but currently behaves as single
  elimination — see [Tournament Formats](#tournament-formats)).
- Registration modes: team-based and individual-based tournaments.
- Team lifecycle: create standalone teams, enter teams into open tournaments,
  manage memberships, and invite members.
- Score workflow: submit, confirm, dispute, and audit all changes.
- Rescheduling, open slots and no-show reporting.
- Withdrawal handling with policy-based behaviour.
- Per-tournament substitutes.
- Notifications for invites, matches and score events.
- Organizer applications with an admin approval step.
- Organizer tools: analytics, backups and audit log.
- Site-admin tools: user management and impersonation.
- Public pages: tournament list and detail, standings, fixtures, organizer and
  user profiles — all without login.

## Quick Start

### 1. Create and activate a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Run migrations

```bash
python manage.py migrate
```

### 4. Create an admin account

```bash
python manage.py createsuperuser
```

A superuser is both a site admin and an organizer. To make an ordinary user an
organizer, either approve their application from the Settings page, or create a
verified profile directly:

```python
# python manage.py shell
from core.models import OrganizerProfile
OrganizerProfile.objects.update_or_create(user=user, defaults={"verified": True})
```

### 5. Start the development server

```bash
python manage.py runserver 0.0.0.0:8000
```

### 6. Open the app

- Local: http://localhost:8000
- LAN: http://&lt;your-local-ip&gt;:8000

> `runserver` is a development server. It must not face an untrusted network —
> see [Deployment](#deployment).

### Running the tests

```bash
python manage.py test
```

The suite is ~286 tests and runs in well under a minute. `settings.py`
substitutes a fast password hasher when — and only when — the first argument to
`manage.py` is `test`; without it the suite spends almost all of its time in
PBKDF2.

## Configuration

Every setting below is read from the environment at startup. Defaults in
parentheses are what you get with nothing set.

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | a committed development key | Signing key for sessions, CSRF tokens and password-reset links. **Required when `DJANGO_DEBUG=False`** — startup fails with `ImproperlyConfigured` if the fallback is still in use. |
| `DJANGO_DEBUG` | `True` | Debug mode. Set to `False` for any deployment; doing so also switches on the security settings listed below. |
| `DJANGO_ALLOWED_HOSTS` | `127.0.0.1,localhost` | Comma-separated hostnames the app will serve. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `http://127.0.0.1,http://localhost` | Comma-separated origins (with scheme) trusted for CSRF. |
| `DJANGO_BACKUP_DIR` | `../tournament_manager_backups` | Where backup JSON is written. Defaults **outside** the working tree because backups contain password hashes. |
| `DJANGO_CACHE_BACKEND` | `LocMemCache` in debug, `DatabaseCache` otherwise | Cache backend. Login throttling counts attempts here. |
| `DJANGO_CACHE_LOCATION` | `tm_cache_table` | Cache location — the table name for `DatabaseCache`. |
| `DJANGO_ENABLE_TEST_MAKER` | on in debug, off otherwise | Enables the Test Maker tool. It creates real accounts and rewrites live match scores, so leave it off outside development. |
| `DJANGO_TEST_MAKER_PREFIX` | `tm_` | Username prefix for accounts Test Maker creates. Its bulk actions are confined to accounts carrying this prefix, so it cannot sweep up real users. |
| `DJANGO_TRUSTED_PROXY_COUNT` | `0` | How many reverse proxies sit in front of the app. `0` means `X-Forwarded-For` is ignored and `REMOTE_ADDR` is used. Setting it higher than the real proxy count lets clients spoof their recorded IP. |
| `DJANGO_OPEN_AVAILABILITY_DAYS` | `365` | How far ahead the scheduler projects an open-ended court-availability row when the tournament has no end date. Lower is faster; too low and sparse tournaments report "not enough court availability". |
| `DJANGO_SECURE_SSL_REDIRECT` | `True` (when `DEBUG=False`) | Redirect HTTP to HTTPS. Set to `False` only if the reverse proxy already does this. |
| `DJANGO_HSTS_SECONDS` | `31536000` (when `DEBUG=False`) | `Strict-Transport-Security` max-age. Set to `0` until HTTPS is confirmed working. |

With `DJANGO_DEBUG=False` the app also enables, without any env var: secure and
HttpOnly session cookies, `SameSite=Lax`, HSTS with subdomains and preload,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`,
`X-Frame-Options: DENY`, and `SECURE_PROXY_SSL_HEADER` for
`X-Forwarded-Proto`. That last one is only safe if your proxy always
overwrites that header.

## Deployment

```bash
# 1. A real secret key
export DJANGO_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(50))')"

# 2. Leave debug mode
export DJANGO_DEBUG=False
export DJANGO_ALLOWED_HOSTS="tournaments.example.com"
export DJANGO_CSRF_TRUSTED_ORIGINS="https://tournaments.example.com"

# 3. Somewhere to put backups, outside the checkout
export DJANGO_BACKUP_DIR=/var/lib/tournament-manager/backups

# 4. Schema, cache table and static files
python manage.py migrate
python manage.py createcachetable          # required: DatabaseCache is the non-debug default
python manage.py collectstatic --noinput

# 5. Confirm the configuration
python manage.py check --deploy

# 6. Serve through a WSGI server behind a reverse proxy
gunicorn tournament_manager.wsgi:application --bind 127.0.0.1:8000
```

Notes:

- **`createcachetable` is not optional.** Outside debug the cache backend is
  `DatabaseCache`, and without the table every cache call raises. Login
  throttling fails *open* rather than locking everyone out, so a missing table
  does not break the site — it silently disables throttling. Nothing will tell
  you; run the command.
- Set `DJANGO_TRUSTED_PROXY_COUNT` to the number of proxies in front of the
  app, or audit-log IPs will record the proxy rather than the client.
- The default database is SQLite, which is a poor fit for concurrent writes.
  Several locking behaviours in this codebase (`select_for_update`) are correct
  but inert on SQLite.
- `manage.py runserver` is never appropriate for a deployment.

## Current Behaviour Rules

### Team creation without a selected tournament

- Users can create standalone teams even when no tournament is currently
  selected.
- A quick create-team action is visible from team navigation and the teams page
  empty state.

### Team-size enforcement for tournament entry

- For team-mode tournaments, a team can only be entered when its member count
  is **exactly** equal to the tournament's players-per-team value.
- Example: if a tournament requires 2 players per team, a team with 3 members
  cannot register.
- The same equality is enforced again when registration is closed and when the
  tournament is started.

### Withdrawal behaviour before and after activation

- Before tournament activation (`setup`, `registration_open`, `ready`,
  `scheduled`):
  - Team withdrawal is treated as deregistration.
  - Draft matches involving that team are cancelled.
  - No forfeits are applied.
- After activation:
  - Withdrawal uses the configured withdrawal policy (forfeit or void).
- Completed tournaments do not allow withdrawals.

### Tournament ownership

- A tournament records the organizer who created it (`Tournament.created_by`).
- Organizers manage only their own tournaments. Site admins (superuser or
  staff) manage all of them.
- Tournaments created before this field existed have no owner, and any
  organizer may manage them.

### Substitutes

- Substitutes are recorded per tournament participation, not on the team
  roster, so a substitute for one tournament is not silently a member of the
  team everywhere else.

## Organizer Flow

1. Create a tournament and choose format and registration mode.
2. Add courts and availability, or manual time slots.
3. Open registration; approve or seed participants.
4. Generate the schedule draft.
5. Start the tournament (publish fixtures).
6. Monitor the dashboard, resolve disputes, handle reschedules and
   withdrawals.

## Player Flow

1. Create an account and log in.
2. Join an open tournament, or create and enter a team.
3. Manage team members (captain and organizer controls) and respond to
   invites.
4. Submit and confirm scores.
5. Request reschedules when needed.

## Becoming an Organizer

1. A logged-in user applies at `/organizer/apply/`.
2. A site admin reviews the application from the Settings page.
3. Approval sets `OrganizerProfile.verified`, which is the organizer signal
   throughout the app.

A user who is both a verified organizer and a team member gets a view toggle in
the ribbon — see [`DUAL_ROLE_TOGGLE_FEATURE.md`](DUAL_ROLE_TOGGLE_FEATURE.md).

## Tournament Formats

| Format | Description |
|---|---|
| Round Robin | All teams play each other; standings are points-based. |
| Double Round Robin | As above, twice — home and away. |
| Knockout | Single elimination bracket. |
| Double Elimination | **Currently single elimination.** The losers bracket is not implemented — a team is out after one defeat. See `REMEDIATION_PLAN.md` T-4.4. |
| Consolation | Knockout, plus a secondary bracket generated from the first-round losers once round 1 completes. |
| Hybrid | Group phase followed by knockout playoffs. |

## Backups

Backups are JSON files written to `DJANGO_BACKUP_DIR`. They serialize
`auth.User`, **including password hashes** — treat a backup file as credential
material and keep it out of version control.

Restore validates the file first and refuses anything invalid, then replaces
the contents of every backed-up table inside a single transaction.

**Backups taken before format version 2 cannot be restored.** They were written
by a version that stored a team-to-court relation which no longer exists;
`validate_backup` rejects them rather than restoring a corrupt subset.

## Tech Stack

- Backend: Django 5.2 + SQLite
- Frontend: Django templates, HTMX partials, static CSS/JS
- Authentication: Django auth and sessions
- Data storage: `db.sqlite3`
- Backups: JSON, outside the working tree by default

## Project Layout

```text
core/
	models.py            domain models
	views.py             all request handling
	forms.py             forms and validation
	urls.py              URLconf
	apps.py              app config; connects signals
	signals.py           post_save handlers (+ suppression for restores)
	scheduling.py        fixture generation and slot building
	standings.py         standings and tiebreaks
	withdrawals.py       withdrawal policy
	backup.py            backup, validate, restore
	audit.py             audit log helpers, client IP resolution
	context_processors.py
	admin.py, admin_config.py
	services/            enrollment
	templatetags/        core_extras
	management/commands/ backfill and integrity commands
	migrations/
	tests*.py            the test suite
templates/core/          templates, with partials/ for HTMX fragments
static/
docs/                    reference-workflows.txt
scripts/                 ad-hoc diagnostics and seeding (see scripts/README.md)
tournament_manager/      settings, root URLconf, WSGI/ASGI
manage.py
requirements.txt
```

## Key Routes

| Route | Purpose | Access |
|---|---|---|
| `/` | Public home | anyone |
| `/tournaments/` | Public tournament list | anyone |
| `/tournaments/<pk>/` | Public tournament detail | anyone |
| `/public/standings/` | Public standings | anyone |
| `/public/fixtures/` | Public fixtures | anyone |
| `/organizers/<pk>/` | Organizer public page | anyone |
| `/users/<username>/` | User public profile | anyone |
| `/dashboard/` | Organizer/team dashboard | any user |
| `/join/` | Open tournaments listing | any user |
| `/teams/` | Teams or participants for the selected tournament | any user |
| `/teams/my-invites/` | Invitations addressed to you | any user |
| `/dashboard/registrations/` | Your registrations | any user |
| `/notifications/` | Notification inbox | any user |
| `/fixtures/` | Tournament fixtures | any user |
| `/standings/` | Standings or bracket | any user |
| `/rescheduling/` | Reschedule requests and actions | any user |
| `/open-slots/` | Open slot listing | any user |
| `/organizer/apply/` | Apply to become an organizer | any user |
| `/analytics/` | Tournament analytics | organizer, or a player enrolled in the tournament |
| `/tournament/<pk>/config/` | Tournament configuration | owning organizer |
| `/tournament/<pk>/seed/` | Seed participants | owning organizer |
| `/backup/` | Backup and restore | organizer |
| `/audit-log/` | Action history | organizer |
| `/settings/` | Settings; user management for site admins | organizer |
| `/settings/impersonate/<pk>/` | Impersonate a user (POST) | superuser |
| `/testing/` | Test Maker | site admin, and only when enabled |

## Further Reading

- [`CODE_REVIEW_FINDINGS.md`](CODE_REVIEW_FINDINGS.md) — audit of bugs and
  shortcomings, with what has been fixed.
- [`REMEDIATION_PLAN.md`](REMEDIATION_PLAN.md) — the task-by-task plan those
  findings were worked through.
- [`DUAL_ROLE_TOGGLE_FEATURE.md`](DUAL_ROLE_TOGGLE_FEATURE.md) — the
  organizer/team view toggle.
- [`docs/reference-workflows.txt`](docs/reference-workflows.txt) — the workflow
  specification the app is built against.
- [`scripts/README.md`](scripts/README.md) — the ad-hoc scripts.
