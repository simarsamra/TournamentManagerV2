# Tournament Manager

Tournament Manager is a Django web app for running sports tournaments (table tennis by default) on a local network. Organizers can configure tournaments, schedules, and rules, while players can register, join teams, submit scores, and manage match workflows.

## Highlights

- Multiple formats: round robin, knockout, double elimination, and hybrid.
- Registration modes: team-based and individual-based tournaments.
- Team lifecycle: create standalone teams, enter teams into open tournaments, and manage memberships.
- Score workflow: submit, confirm, dispute, and audit all changes.
- Rescheduling and open slots management.
- Withdrawal handling with policy-based behavior.
- Organizer tools: analytics, backups, audit log, and test maker.
- Public pages: standings and fixtures without login.

## Current Behavior Rules

### Team creation without a selected tournament

- Users can create standalone teams even when no tournament is currently selected.
- A quick create-team action is visible from team navigation and the teams page empty state.

### Team-size enforcement for tournament entry

- For team-mode tournaments, a team can only be entered when its member count is exactly equal to the tournament players-per-team value.
- Example: if a tournament requires 2 players per team, a team with 3 members cannot register.

### Withdrawal behavior before and after activation

- Before tournament activation (setup, registration_open, ready, scheduled):
	- Team withdrawal is treated as deregistration.
	- Draft matches involving that team are cancelled.
	- No forfeits are applied.
- After activation:
	- Withdrawal uses the configured withdrawal policy (forfeit or void).
- Completed tournaments do not allow withdrawals.

## Quick Start

### 1. Create and activate a virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Run migrations

```bash
python manage.py migrate
```

### 4. Create an organizer account

```bash
python manage.py createsuperuser
```

### 5. Start the server

```bash
python manage.py runserver 0.0.0.0:8000
```

### 6. Open the app

- Local: http://localhost:8000
- LAN: http://<your-local-ip>:8000

## Organizer Flow

1. Create tournament and choose format/registration mode.
2. Add courts and availability or time slots.
3. Open registration.
4. Generate schedule draft.
5. Start tournament (publish fixtures).
6. Monitor dashboard, resolve disputes, and manage withdrawals.

## Player Flow

1. Create account and log in.
2. Join an open tournament (or create/enter a team).
3. Manage team members (captain/organizer controls).
4. Submit and confirm scores.
5. Request reschedules when needed.

## Tournament Formats

| Format | Description |
|---|---|
| Round Robin | All teams play each other and standings are points-based. |
| Knockout | Single elimination bracket. |
| Double Elimination | Winners and losers brackets. |
| Hybrid | Group phase followed by knockout playoffs. |

## Tech Stack

- Backend: Django + SQLite
- Frontend: Django templates + static CSS/JS
- Authentication: Django auth and sessions
- Data storage: db.sqlite3
- Backups: JSON files in backups

## Project Layout

```text
core/
	models.py
	views.py
	forms.py
	urls.py
	scheduling.py
	standings.py
	withdrawals.py
	backup.py
	audit.py
templates/core/
static/
tournament_manager/
manage.py
requirements.txt
```

## Key Routes

| Route | Purpose |
|---|---|
| /dashboard/ | Organizer/team dashboard |
| /join/ | Open tournaments listing |
| /teams/ | Teams or participants for selected tournament |
| /fixtures/ | Tournament fixtures |
| /standings/ | Standings or bracket |
| /rescheduling/ | Reschedule requests and actions |
| /analytics/ | Tournament analytics |
| /settings/ | Organizer settings |
| /backup/ | Backup and restore |
| /audit-log/ | Action history |
| /public/standings/ | Public standings |
| /public/fixtures/ | Public fixtures |
