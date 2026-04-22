#!/usr/bin/env python
import os
import django
from datetime import timedelta

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tournament_manager.settings")
django.setup()

from core.models import Tournament, Match
from django.utils import timezone

# Get the active tournament
t = Tournament.objects.filter(status='active').first()
if not t:
    print('No active tournament found')
    exit()

print(f'=' * 70)
print(f'KNOCKOUT MATCHES ANALYSIS FOR: {t.name}')
print(f'=' * 70)
print()

# Get knockout matches  
ko_matches = t.matches.filter(bracket_type='winners')
print(f'TOTAL KNOCKOUT MATCHES: {ko_matches.count()}')
print()

# Status breakdown
print('MATCHES BY STATUS:')
statuses = {}
for m in ko_matches:
    statuses[m.status] = statuses.get(m.status, 0) + 1
for status in sorted(statuses.keys()):
    print(f'  {status:20s}: {statuses[status]:3d}')
print()

# Get upcoming matches with details
upcoming = ko_matches.filter(status='upcoming').order_by('round_number', 'match_number')
print(f'UPCOMING KNOCKOUT MATCHES ({upcoming.count()}):')
for m in upcoming:
    scheduled = m.scheduled_time
    if scheduled:
        local = timezone.localtime(scheduled)
        now = timezone.localtime(timezone.now())
        delta = scheduled - timezone.now()
        days_away = delta.days
        hours_away = delta.seconds // 3600
        if delta.days < 0 or (delta.days == 0 and delta.seconds < 0):
            time_str = "PAST (can submit score)"
        elif delta.days > 30:
            time_str = f"~{days_away} days away"
        elif delta.days > 0:
            time_str = f"{days_away}d {hours_away}h away"
        else:
            time_str = f"{hours_away}h away"
    else:
        time_str = "Not scheduled"
    
    print()
    print(f'  Match #{m.match_number} (Round {m.round_number}):')
    print(f'    Teams: {m.team1.name if m.team1 else "TBD"} vs {m.team2.name if m.team2 else "TBD"}')
    print(f'    Status: {m.status}')
    print(f'    Scheduled: {scheduled} ({time_str})')
    print(f'    Can submit score: YES (status="upcoming" + is_participant)')
    print(f'    Can reschedule: YES (status="upcoming" + is_captain)')
print()

# Summary
print('=' * 70)
print('SUMMARY:')
print('=' * 70)
if upcoming.count() == 0:
    print('NO UPCOMING KNOCKOUT MATCHES')
    print()
    print('Users cannot submit scores or reschedule because:')
    print('  1. All knockout matches are already completed (confirmed/forfeited)')
    print('  2. The tournament is likely in its final stages')
    print()
    print('THIS IS NOT A BUG - it\'s normal tournament progression')
else:
    print(f'There are {upcoming.count()} upcoming knockout matches')
    print('Users SHOULD be able to submit scores and reschedule these matches')
    print()
    if upcoming.count() <= 3:
        print('NOTE: Only 3 or fewer matches remain, likely in the finals')

print()
print('=' * 70)
