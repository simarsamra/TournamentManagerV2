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
    print()
    
    # Get upcoming knockout matches
    upcoming_matches = t.matches.filter(bracket_type='winners', status='upcoming')
    print(f'Upcoming Knockout Matches ({upcoming_matches.count()}):')
    for m in upcoming_matches:
        team1_name = m.team1.name if m.team1 else "TBD"
        team2_name = m.team2.name if m.team2 else "TBD"
        print(f'  Match #{m.match_number} (Round {m.round_number}): {team1_name} vs {team2_name}')
        print(f'    Scheduled: {m.scheduled_time}')
        print(f'    Score: {m.score_team1} - {m.score_team2}' if m.score_team1 is not None else "    Score: Not recorded")
        print()
    
    # Check if there are any recent matches that might be in progress or just completed
    print()
    print('In-Progress/Pending Confirmation Knockout Matches:')
    in_progress = t.matches.filter(bracket_type='winners').filter(status__in=['in_progress', 'pending_confirmation'])
    if in_progress.exists():
        for m in in_progress:
            team1_name = m.team1.name if m.team1 else "TBD"
            team2_name = m.team2.name if m.team2 else "TBD"
            print(f'  Match #{m.match_number}: {team1_name} vs {team2_name} - Status: {m.status}')
    else:
        print('  None')
