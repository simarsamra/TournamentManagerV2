# Tournament Manager

[![CI](https://github.com/simarsamra/TournamentManagerV2/actions/workflows/ci.yml/badge.svg)](https://github.com/simarsamra/TournamentManagerV2/actions/workflows/ci.yml)

Tournament Manager is a Django web app for running sports tournaments (table
tennis by default) on a local network. Organizers configure tournaments,
schedules and rules; players register, join teams, submit scores and manage
match workflows.

Tested against **Django 5.2 LTS on Python 3.11**.

## Highlights

- Multiple formats: round robin, double round robin, knockout, double
  elimination, consolation and hybrid.
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
- Optional AI analytics: ask questions in plain language, answered by a local
  model via Ollama, with every number checked (see
  [AI analytics](#ai-analytics-optional)).
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
pip install -r requirements.txt -c constraints.txt
```

`constraints.txt` pins the exact versions this project is tested against;
`requirements.txt`'s own ranges are the wider compatibility window. See
[Upgrading dependencies](#upgrading-dependencies) to move that pin forward.

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

The suite is ~297 tests and runs in under ten seconds. `settings.py`
substitutes a fast password hasher when — and only when — the first argument to
`manage.py` is `test`; without it the suite spends almost all of its time in
PBKDF2.

### Continuous integration

`.github/workflows/ci.yml` runs on every pull request and on pushes to `main`:

| Step | What it catches |
|---|---|
| `manage.py check` | Broken settings, app or model configuration |
| `manage.py makemigrations --check --dry-run` | A model change committed without its migration |
| `manage.py test --verbosity=2` | The whole suite |
| `manage.py check --deploy --fail-level WARNING` | A production setting regressing — cookie flags, HSTS, SSL redirect |
| Fallback-key probe | The startup guard failing to refuse the committed `SECRET_KEY` outside `DEBUG` |

The last two run with `DJANGO_DEBUG=False`, so the non-debug branch of
`settings.py` is exercised on every run rather than only in a deployment.

## Configuration

Every setting below is read from the environment at startup. Defaults in
parentheses are what you get with nothing set.

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | a committed development key | Signing key for sessions, CSRF tokens and password-reset links. **Required when `DJANGO_DEBUG=False`** — startup fails with `ImproperlyConfigured` if the fallback is still in use. |
| `DJANGO_DEBUG` | `True` | Debug mode. Set to `False` for any deployment; doing so also switches on the security settings listed below. |
| `DJANGO_ALLOWED_HOSTS` | `127.0.0.1,localhost` | Comma-separated hostnames the app will serve. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `http://127.0.0.1,http://localhost` | Comma-separated origins (with scheme) trusted for CSRF. |
| `DATABASE_URL` | unset | Full PostgreSQL connection URL. Takes precedence over every `DJANGO_DB_*` variable below. |
| `DJANGO_DB_ENGINE` | `sqlite3` | `sqlite3` or `postgresql`. See [Running on PostgreSQL](#running-on-postgresql). |
| `DJANGO_DB_NAME` | `db.sqlite3` / `tournament_manager` | Database file (SQLite) or name (PostgreSQL). |
| `DJANGO_DB_USER` | `tournament_manager` | PostgreSQL role. Ignored on SQLite. |
| `DJANGO_DB_PASSWORD` | empty | PostgreSQL password. Ignored on SQLite. |
| `DJANGO_DB_HOST` | `127.0.0.1` | PostgreSQL host. Ignored on SQLite. |
| `DJANGO_DB_PORT` | `5432` | PostgreSQL port. Ignored on SQLite. |
| `DJANGO_CONN_MAX_AGE` | `60` | Seconds to reuse a database connection. `0` opens a new one per request. |
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

The optional AI analytics feature has its own settings; see
[AI analytics → Settings](#settings).

## Deployment

```bash
# 1. Install through the pinned versions this project is tested against
pip install -r requirements.txt -c constraints.txt

# 2. A real secret key
export DJANGO_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(50))')"

# 3. Leave debug mode
export DJANGO_DEBUG=False
export DJANGO_ALLOWED_HOSTS="tournaments.example.com"
export DJANGO_CSRF_TRUSTED_ORIGINS="https://tournaments.example.com"

# 4. Somewhere to put backups, outside the checkout
export DJANGO_BACKUP_DIR=/var/lib/tournament-manager/backups

# 5. Schema, cache table and static files
python manage.py migrate
python manage.py createcachetable          # required: DatabaseCache is the non-debug default
python manage.py collectstatic --noinput

# 6. Confirm the configuration
python manage.py check --deploy

# 7. Serve through a WSGI server behind a reverse proxy
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
  See [Running on PostgreSQL](#running-on-postgresql).
- `manage.py runserver` is never appropriate for a deployment.

### Upgrading dependencies

`requirements.txt` and `requirements-postgres.txt` declare a compatibility
range (currently the Django 5.2 LTS series); `constraints.txt` pins the exact
versions CI and this deployment recipe actually install. Moving that pin
forward is a deliberate, reviewable step, not something that happens by
installing on a different day:

```bash
python -m venv /tmp/upgrade-venv
/tmp/upgrade-venv/bin/pip install -r requirements.txt -r requirements-postgres.txt
/tmp/upgrade-venv/bin/pip freeze --exclude-editable > constraints.txt
```

Widen the range in `requirements.txt` (or `requirements-postgres.txt`) first
if the new version falls outside it, then commit the regenerated
`constraints.txt` and let CI validate the result before merging.

## Running on PostgreSQL

SQLite is the default and needs no configuration. PostgreSQL is what a
deployment with more than one concurrent user wants.

```bash
pip install -r requirements.txt -r requirements-postgres.txt -c constraints.txt

export DJANGO_DB_ENGINE=postgresql
export DJANGO_DB_NAME=tournament_manager
export DJANGO_DB_USER=tournament_manager
export DJANGO_DB_PASSWORD=...
export DJANGO_DB_HOST=127.0.0.1
export DJANGO_DB_PORT=5432

python manage.py migrate
```

A `DATABASE_URL` takes precedence over all of the above, so a platform that
injects one needs nothing else:

```bash
export DATABASE_URL="postgres://user:password@host:5432/tournament_manager"
```

### Why it matters more than performance

SQLite was not making concurrent writes safe — it was making them *fail*. It
locks whole tables, so a second simultaneous writer got
`database table is locked` and gave up. That accidentally preserved some
invariants the code never enforced itself.

PostgreSQL commits both writers. Registration was a check-then-act — count the
participants, then create one — and on PostgreSQL two people joining at once
both passed the check, filling a one-slot tournament with two teams and no
error anywhere. Both behaviours were reproduced before the fix.

Registration and reschedule responses now take an explicit row lock
(`SELECT ... FOR UPDATE`) so the check and the write are one step.
`core/tests_concurrency.py` covers this and runs only on PostgreSQL, where row
locking is real; it is skipped on SQLite.

**If you write new code that reads a count and then writes based on it, take
the lock.** `core/views/helpers._claim_participant_slot` is the pattern.

### Moving existing data from SQLite

```bash
# 1. Dump from SQLite. Content types and permissions are recreated by migrate,
#    so excluding them avoids primary-key collisions on load.
DJANGO_DB_ENGINE=sqlite3 python manage.py dumpdata \
    --natural-foreign --natural-primary \
    --exclude contenttypes --exclude auth.permission --exclude sessions.session \
    --indent 2 -o dump.json

# 2. Create the database and schema.
createdb tournament_manager
DJANGO_DB_ENGINE=postgresql python manage.py migrate --no-input

# 3. Load.
DJANGO_DB_ENGINE=postgresql python manage.py loaddata dump.json
```

Verified end to end on PostgreSQL 16: row counts, foreign keys and bracket
structure all survive, and `loaddata` resets the sequences, so the next insert
gets a fresh primary key rather than colliding.

Two differences to expect. PostgreSQL enforces `NOT NULL` where SQLite may have
let a null through, so a dump from a long-lived SQLite file can fail to load on
a column the schema always declared as required — fix the offending rows in the
dump. And `dump.json` contains password hashes, so treat it like a backup file
and delete it once the load succeeds.

## AI analytics (optional)

Organizers can ask questions about a tournament in plain language on the
analytics page, for example "How have the Aces been doing lately?", "Who do
the Bolts play next?" or "What if the Aces beat the Bolts?". A model served
by [Ollama](https://ollama.com) **on the same machine** answers them.
Nothing leaves the server. The feature is off until you enable it.

How it answers (full design in [`AI_ANALYTICS_PLAN.md`](AI_ANALYTICS_PLAN.md)):

1. **The model picks which card answers the question.** Its reply is
   constrained to a JSON schema whose only team choices are this
   tournament's teams, and the server validates it again.
2. **The app computes the numbers** with the same code as the analytics
   page. The model never sees the database.
3. **The model writes up to three sentences.** They're shown only if every
   number in them appears in the computed figures. Otherwise just the
   figures are shown.

Model calls never run inside a web request. The site queues questions, a
separate `ai_worker` process answers them one at a time, and the page
checks back every 2 seconds.

**Recaps.** A tournament's organizers can also press **Write a recap**:
- The model writes a few sentences about the results since the last recap,
  and how the table moved.
- It goes through the same number check.
- Only a recap that passes is published. It then appears in a **Latest recap**
  card for everyone who can view the tournament's analytics.
- Only managers can write one, whatever `DJANGO_AI_ANALYTICS_AUDIENCE` says.
- A new recap can only be written once there are new results.

**Tournament news.** Every dashboard of an active or completed tournament
has a **Tournament News** board, for everyone who can view its analytics:
- The `ai_worker` writes the news on its own, in one model call per update:
  a main story in the style of a sports back page (a punny title, an intro,
  the results with wordplay, 🏆 Standings, 🔥 Next Up and a sign-off), a
  fun headline for each new result, and a teaser for each of the next
  fixtures. Before the first result it previews the opening fixtures.
- The board sorts the headlines into **Today**, **Yesterday** (or the
  **Last matchday** when neither had matches) and **Coming up** when the page
  is opened, so "Today" stays right the next day.
- It's written once for the whole tournament and stored. Opening a
  dashboard never calls the model.
- A new update is written only when there are new results, and at most once
  every `DJANGO_AI_NEWS_INTERVAL_MINUTES`, so a burst of results becomes
  one update.
- Each headline and each part of the story goes through the number check on its own. A headline with
  a number that isn't in the results is dropped, and the rest are still
  shown. The match under it is still listed, with its real score.
- Scores, fixtures, times and courts come straight from the database, so
  they're always current. Only the headline text is written by the model.
- **🎯 My team's take.** Players see a small button in the board's header
  that flips it to a story written just for their team: their latest
  results, where they sit and who's around them, and their next matches
  (with the opponent's rank and head-to-head). **📰 Tournament news** flips
  back. It's written once per team for each main news update, in about half
  a minute, and teammates share it. It gets the same per-part number check.
  Organizers without a team don't see the button.

### Set it up

The examples use `qwen3.5:9b`, which suits a 12 GB NVIDIA GPU (about
6.6 GB, leaving room for an 8K context).

```bash
# 1. Install Ollama (creates the `ollama` systemd service), then pull a model
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3.5:9b

# 2. Recommended Ollama settings for a shared machine (loopback only,
#    one request and one model at a time). Paste the [Service] block from:
sudo systemctl edit ollama        # docs/deploy/ollama-override.conf
sudo systemctl restart ollama

# 3. Switch the feature on in the site's environment (the same file gunicorn
#    uses), then apply the new table
export DJANGO_AI_ANALYTICS_ENABLED=true
python manage.py migrate

# 4. Check everything end to end: reachable, model pulled, JSON replies,
#    thinking off, and how long a call takes
python manage.py ai_doctor

# 5. Run the worker as a service, and purge old questions daily
#    (edit the user and paths in the unit files first)
sudo cp docs/deploy/tm-ai-worker.service docs/deploy/tm-ai-purge.service \
        docs/deploy/tm-ai-purge.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tm-ai-worker tm-ai-purge.timer
```

Then open the analytics page of a tournament you manage. The **Ask about
this tournament** box is at the top.

**Never expose Ollama's port (11434).** Ollama has no authentication.
Keep `OLLAMA_HOST=127.0.0.1:11434`, its default. `manage.py check` warns if
`DJANGO_OLLAMA_URL` points anywhere but this machine.

**Memory.** The model, gunicorn and the database share the machine. After a
question has been answered, `ollama ps` shows how much GPU memory the model
really uses.

### Settings

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_AI_ANALYTICS_ENABLED` | `False` | Master switch. While off, the Ask box is hidden and the AI URLs return 404. |
| `DJANGO_OLLAMA_URL` | `http://127.0.0.1:11434` | Where Ollama listens. Environment proxy settings are ignored for it. |
| `DJANGO_OLLAMA_MODEL` | `qwen3.5:9b` | Model to use; must be pulled. |
| `DJANGO_OLLAMA_THINK` | `false` | Sent as Ollama's `think` flag. `false` skips the hidden reasoning pass of thinking models such as Qwen 3.5, which is much faster. Set it to empty for a model without the flag (`ai_doctor` tells you). |
| `DJANGO_OLLAMA_NUM_CTX` | `8192` | Context length sent with each request. |
| `DJANGO_OLLAMA_TIMEOUT_SECONDS` | `60` | Per-call timeout, in the worker. |
| `DJANGO_OLLAMA_KEEP_ALIVE` | `30m` | How long Ollama keeps the model in memory after a call. Only the first question after a quiet spell pays the load time. |
| `DJANGO_AI_ANALYTICS_AUDIENCE` | `managers` | Who may ask: `managers` (each tournament's organizers) or `all` (anyone who can open its analytics). |
| `DJANGO_AI_EXPLANATIONS` | `True` | Written explanations. When off, answers are the figures only. |
| `DJANGO_AI_CONVERSATION` | `True` | Conversational answers: the model sees the whole tournament (table with points gaps, streaks and matches left, every result, the fixtures, head-to-head records) plus the last few questions, so it answers anything the data covers and follow-ups. Numbers not in the data are flagged under the answer. Off, or for a tournament too big for the snapshot, questions are routed to one card as before. |
| `DJANGO_AI_CONVERSATION_TURNS` | `3` | Earlier questions and answers sent with a follow-up. |
| `DJANGO_AI_NEWS_AUTO` | `True` | The worker writes each tournament's dashboard news when new results are in. When off, recaps are only written when an organizer asks. |
| `DJANGO_AI_NEWS_INTERVAL_MINUTES` | `30` | Shortest time between two news updates for one tournament (including retries after an update failed the number check). |
| `DJANGO_AI_QUESTIONS_PER_USER_PER_HOUR` | `60` | Per-user quota (the per-IP limit on the Ask button is 240 an hour). |
| `DJANGO_AI_MAX_PENDING` | `20` | Questions allowed to wait at once, site-wide; beyond it, "busy, try later". |
| `DJANGO_AI_MAX_QUESTION_CHARS` | `300` | Longest question accepted. |
| `DJANGO_AI_JOB_STALE_SECONDS` | `600` | A question still "running" after this long is marked failed (a crashed worker). |
| `DJANGO_AI_RETENTION_DAYS` | `30` | `ai_purge` deletes questions older than this, except each tournament's published recap. |
| `DJANGO_AI_LOG_LEVEL` | `INFO` | Level of the `core.ai` log on stderr (under systemd: `journalctl -u tm-ai-worker`). |

### What is stored

Each question is saved in the `AIQuestion` table with:
- the question and its status;
- the card the model chose;
- the figures it was shown;
- the explanation (even when hidden);
- the model name and timings.

Only the person who asked can see their question. Rows are deleted after
`DJANGO_AI_RETENTION_DAYS`, and they're deliberately left out of backups.
What the model is shown never includes:
- the audit log;
- match or availability notes;
- usernames, emails or player names;
- internal team names;
- other tournaments.

### Choosing and checking the model

`ai_eval` runs 42 labelled questions and 6 explanation cases through the
real prompts and checks. It reports:
- routing accuracy, listing each wrong route;
- how many explanations pass the number check;
- latency and model load time.

It doesn't touch the database, so it's safe to run on the live server.

```bash
python manage.py ai_eval --model qwen3.5:9b --model gemma4:12b --json eval.json
```

### Troubleshooting

| Symptom | Likely cause |
|---|---|
| Answers stay on "Thinking…" | The worker isn't running: `systemctl status tm-ai-worker`. |
| "The AI service isn't available right now" | Ollama is down or unreachable: `systemctl status ollama`, then `manage.py ai_doctor`. |
| "The model took too long to answer" | Raise `DJANGO_OLLAMA_TIMEOUT_SECONDS`, or check that the model runs on the GPU (`ollama ps`). |
| Explanations are often hidden | The model is stating numbers that aren't in the figures. The worker logs each one (`journalctl -u tm-ai-worker`), and `ai_eval` lists them. Try another model, or set `DJANGO_AI_EXPLANATIONS=false`. |
| Questions go to the wrong card | Run `ai_eval` to see which ones; compare models. |

To switch the feature off, unset `DJANGO_AI_ANALYTICS_ENABLED` and stop
`tm-ai-worker`. The site behaves exactly as it did before.

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

### Double elimination and the grand final

- A defeat in the winners bracket drops a team into the losers bracket. A
  second defeat eliminates it.
- The losers-bracket champion reaches the grand final with one defeat; the
  winners-bracket champion reaches it with none. With `enable_bracket_reset`
  on (the default), a grand final won by the losers-bracket champion is
  followed by a decider, so nobody is eliminated on a single defeat. Turn it
  off for a fixed match count at the cost of that asymmetry.
- Slot reservation accounts for the decider: `2n-2` matches, or `2n-1` with
  bracket reset enabled.
- When the field is not a power of two, byes in the first winners round leave
  losers-bracket slots that nothing can fill. Those matches are resolved as
  walkovers at generation time — they are never scheduled and are hidden from
  the bracket display.

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
| Double Elimination | Winners bracket, losers bracket and a grand final. A team is out after two defeats. |
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
	views/               request handling, split by domain
		helpers.py       authorisation predicates, lookup, match finalisation
		auth.py          sign-in, registration, profile, dashboard
		tournaments.py   setup, courts, availability, lifecycle
		teams.py         rosters, invitations, captaincy
		matches.py       fixtures, scores, disputes, reschedules
		registration.py  joining, registration review, seeding
		reporting.py     standings, analytics, backups, public pages
		ai.py            AI analytics: ask and answer-status views
		admin_tools.py   settings, user management, impersonation
		test_maker.py    development-only data generator
	forms.py             forms and validation
	urls.py              URLconf
	apps.py              app config; connects signals
	signals.py           post_save handlers (+ suppression for restores)
	scheduling.py        fixture generation and slot building
	standings.py         standings and tiebreaks
	analytics.py         analytics calculations, shared by the page and the AI
	ai/                  optional AI analytics: Ollama client, router, facts,
	                     explanations, job queue, eval question set
	checks.py            system checks for the AI settings
	test_runner.py       test runner that blocks outbound HTTP
	withdrawals.py       withdrawal policy
	backup.py            backup, validate, restore
	audit.py             audit log helpers, client IP resolution
	context_processors.py
	admin.py, admin_config.py
	services/            enrollment
	templatetags/        core_extras
	management/commands/ seed_demo, backfill and integrity commands;
	                     ai_doctor, ai_worker, ai_eval, ai_purge
	migrations/
	tests*.py            the test suite
templates/core/          templates, with partials/ for HTMX fragments
static/
docs/                    reference-workflows.txt; deploy/ systemd units for the AI worker
scripts/                 fixtures/ sample data only (see scripts/README.md)
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
| `/analytics/` | Tournament analytics | the tournament's organizer, or a player enrolled in it |
| `/analytics/ask/` | Ask the AI a question (POST) | per `DJANGO_AI_ANALYTICS_AUDIENCE`; 404 while AI analytics is off |
| `/analytics/ask/<pk>/` | Status and answer of your question or recap | the person who asked; 404 while AI analytics is off |
| `/analytics/recap/` | Commission a recap of the latest results (POST) | the tournament's organizer; 404 while AI analytics is off |
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
- [`ANALYTICS_PLAN.md`](ANALYTICS_PLAN.md) — the analytics page fixes
  (A-1 to A-13) and their results.
- [`AI_ANALYTICS_PLAN.md`](AI_ANALYTICS_PLAN.md) — design and task log for
  the optional AI analytics.
- [`docs/reference-workflows.txt`](docs/reference-workflows.txt) — the workflow
  specification the app is built against.
- [`scripts/README.md`](scripts/README.md) — where the old ad-hoc scripts
  went (deleted, converted to tests, or promoted to `manage.py seed_demo`).
