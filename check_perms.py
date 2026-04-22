#!/usr/bin/env python
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tournament_manager.settings")
django.setup()

from core.models import Tournament, Match, Team
from django.contrib.auth.models import User

# Get the active tournament
t = Tournament.objects.filter(status='active').first()
if not t:
    print('No active tournament found')
    exit()

print(f'Active Tournament: {t.name} (pk={t.pk})')
print()

# Get an upcoming knockout match
upcoming = t.matches.filter(bracket_type='winners', status='upcoming').first()
if not upcoming:
    print('No upcoming knockout matches found')
    exit()

print(f'Sample Upcoming Knockout Match: #{upcoming.match_number}')
print(f'  Teams: {upcoming.team1.name} vs {upcoming.team2.name}')
print(f'  Status: {upcoming.status}')
print(f'  Scheduled: {upcoming.scheduled_time}')
print()

# Get a team from this match
team = upcoming.team1
print(f'Team: {team.name}')
print(f'  Members:')
for member in team.memberships.select_related('user'):
    print(f'    - {member.user.username} ({member.role})')
print()

# Check the match status in terms of what can be done
print('Match Properties:')
print(f'  match.status in ("upcoming", "in_progress"): {upcoming.status in ("upcoming", "in_progress")}')
print(f'  team1_id: {upcoming.team1_id}')
print(f'  team2_id: {upcoming.team2_id}')
print(f'  score_team1: {upcoming.score_team1}')
print(f'  score_team2: {upcoming.score_team2}')
print()

# Check if there might be any permission issues
print('Checking for potential blocks...')
print(f'  Tournament status: {t.status}')
print(f'  Is tournament active: {t.status == "active"}')
print(f'  Match has both teams assigned: {upcoming.team1_id and upcoming.team2_id}')
print()

# Check template-level conditions
print('Template-level checks (from match_detail view):')
print(f'  is_participant: would be True if user in team1 or team2')
print(f'  match.status == "upcoming": {upcoming.status == "upcoming"}')
print(f'  Template condition for reschedule: is_participant and match.status == "upcoming"')
print(f'  Would reschedule form show: YES')
print()
print(f'  match.status in ("upcoming", "in_progress"): {upcoming.status in ("upcoming", "in_progress")}')
print(f'  Template condition for score submit: is_participant and match.status in ("upcoming", "in_progress")')
print(f'  Would score form show: YES')
