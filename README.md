# 🏓 Tournament Manager

A comprehensive local-network web application for managing table tennis tournaments. Runs on a single machine and is accessible to all users on the same LAN via browser.

## Features

- **Multiple formats**: Round Robin, Knockout, Double Elimination, Hybrid (Groups + Knockout)
- **Decentralized score validation**: Teams submit and confirm scores mutually
- **Rescheduling**: Propose and approve new match times with conflict detection
- **Withdrawal handling**: Configurable forfeit/void policies with automatic bracket updates
- **Analytics dashboard**: Match stats, court utilization, team performance, schedule density
- **Backup & restore**: Manual and automatic JSON backups with validation
- **Audit logging**: Every action logged with user, timestamp, and details
- **Desktop-first UI**: Wide data tables, sidebar navigation, inline actions
- **Public views**: Standings and fixtures visible without login (for viewers/spectators)

## Quick Start

### 1. Install dependencies

```bash
pip install django
```

### 2. Initialize database

```bash
python manage.py migrate
```

### 3. Create organizer account

```bash
python manage.py createsuperuser
```

### 4. Run the server (LAN accessible)

```bash
python manage.py runserver 0.0.0.0:8000
```

### 5. Access the application

- **Local**: http://localhost:8000
- **LAN**: http://<your-local-ip>:8000
- Find your IP: `hostname -I` (Linux) or `ipconfig` (Windows)

## Default Admin Account

If using the pre-created admin:
- **Username**: `admin`
- **Password**: `admin123`

## How to Use

### As Organizer (admin/staff user)

1. **Login** → Create Tournament (select format, configure scoring)
2. **Configure** → Add courts, time slots, and teams (bulk or individual)
3. **Start Tournament** → Fixtures are auto-generated with schedule
4. **Monitor** → Dashboard shows stats; resolve disputed scores
5. **Backup** → Create backups from the Backup page

### As Team

1. **Register** or login with credentials provided by organizer
2. **Dashboard** → See upcoming matches and pending actions
3. **Submit scores** → After a match, submit the score on the match page
4. **Confirm scores** → When opponent submits, confirm or dispute
5. **Reschedule** → Request new time for upcoming matches
6. **Preferences** → Set preferred courts and availability

### As Viewer (no login)

- Visit `/public/standings/` for live standings
- Visit `/public/fixtures/` for match schedule

## Tournament Formats

| Format | Description |
|--------|-------------|
| Round Robin | All teams play each other; standings by points |
| Knockout | Single elimination bracket; winners advance |
| Double Elimination | Winners and losers brackets |
| Hybrid | Group stage (round robin) → Knockout playoffs |

## Architecture

- **Backend**: Django 6.x with SQLite
- **Frontend**: Server-rendered Django templates with custom CSS
- **Auth**: Django's built-in authentication with session management
- **Data**: SQLite database at `db.sqlite3`
- **Backups**: JSON files in `backups/` directory

## Project Structure

```
├── core/                   # Main application
│   ├── models.py          # Database models
│   ├── views.py           # All view functions
│   ├── forms.py           # Django forms
│   ├── urls.py            # URL routing
│   ├── scheduling.py      # Fixture generation engine
│   ├── standings.py       # Standings & bracket logic
│   ├── withdrawals.py     # Withdrawal handling
│   ├── backup.py          # Backup/restore system
│   └── audit.py           # Audit logging
├── templates/core/         # HTML templates
├── static/                 # CSS and JS
├── tournament_manager/     # Django project settings
├── manage.py
└── db.sqlite3
```

## Key URLs

| URL | Description |
|-----|-------------|
| `/dashboard/` | Team/organizer dashboard |
| `/fixtures/` | All matches with filters |
| `/match/<id>/` | Match detail, score submission |
| `/teams/` | Team list |
| `/standings/` | League table or bracket |
| `/analytics/` | Stats and charts |
| `/rescheduling/` | Reschedule requests |
| `/backup/` | Backup management (admin) |
| `/audit-log/` | Action history (admin) |
| `/settings/` | Tournament settings (admin) |
| `/public/standings/` | Public standings (no login) |
| `/public/fixtures/` | Public fixtures (no login) |
Manages different kinds of tournaments
