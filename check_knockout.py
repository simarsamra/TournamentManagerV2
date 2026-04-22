#!/usr/bin/env python
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tournament_manager.settings")
django.setup()

from core.models import Tournament, Match

# Get the active tournament
t = Tournament.objects.filter(status='active').first()
if t:
    print(f'Active Tournament: {t.name} (pk={t.pk})')
    print(f'Format: {t.get_format_display()}')
    print()
    
    # Get knockout matches
    knockout_matches = t.matches.filter(bracket_type='winners')
    print(f'Total Knockout Matches: {knockout_matches.count()}')
    print()
    
    # Group by status
    status_counts = {}
    for m in knockout_matches:
        status_counts[m.status] = status_counts.get(m.status, 0) + 1
    
    print('Knockout Matches by Status:')
    for status, count in sorted(status_counts.items()):
        print(f'  {status}: {count}')
    
    print()
    print('Sample knockout matches:')
    for m in knockout_matches[:10]:
        team1_name = m.team1.name if m.team1 else "TBD"
        team2_name = m.team2.name if m.team2 else "TBD"
        print(f'  Match #{m.match_number}: {team1_name} vs {team2_name} - Status: {m.status}, Scheduled: {m.scheduled_time}')
else:
    print('No active tournament found')
    tournaments = Tournament.objects.all()
    print(f'Available tournaments: {[t.name for t in tournaments]}')
