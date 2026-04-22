#!/usr/bin/env python
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tournament_manager.settings")
django.setup()

from core.models import Tournament, Match, Team, TeamMembership
from django.contrib.auth.models import User
from django.utils import timezone

# Get match 195
match = Match.objects.get(match_number=195)
print(f'Match #195:')
print(f'  ID: {match.pk}')
print(f'  Status: {match.status}')
print(f'  Team1: {match.team1}')
print(f'  Team2: {match.team2}')
print(f'  Bracket Type: {match.bracket_type}')
print(f'  Group: "{match.group}"')
print(f'  Round: {match.round_number}')
print()

# Get Team 3 from the same tournament as match 195
team_3 = match.tournament.teams.get(name='Team 3')
print(f'Team 3:')
print(f'  ID: {team_3.pk}')
print(f'  Tournament: {team_3.tournament}')
print(f'  Members:')
for m in team_3.memberships.select_related('user'):
    print(f'    - {m.user.username} ({m.role})')
print()

# Get a Team 3 user (preferably not captain, to test the reschedule permission)
team_3_members = [m.user for m in team_3.memberships.all()]
print(f'Team 3 Users: {[u.username for u in team_3_members]}')
print()

# Check if team3 is in match 195
print('Match 195 Participants:')
print(f'  Team 1: {match.team1.name if match.team1 else "TBD"} (ID: {match.team1_id})')
print(f'  Team 2: {match.team2.name if match.team2 else "TBD"} (ID: {match.team2_id})')
print()

# Check if Team 3 is a participant
team_3_is_participant = (match.team1 == team_3) or (match.team2 == team_3)
print(f'Is Team 3 a participant in Match 195: {team_3_is_participant}')
print()

# Check the conditions for displaying forms
print('Form Display Conditions:')
print(f'  Tournament status: {match.tournament.status}')
print(f'  Tournament format: {match.tournament.format}')
print(f'  Match status: {match.status}')
print(f'  Match status in ("upcoming", "in_progress"): {match.status in ("upcoming", "in_progress")}')
print(f'  Match bracket_type: {match.bracket_type}')
print()

# Get any team 3 user
user_3 = team_3_members[0] if team_3_members else None
if user_3:
    from core.views import _get_team, _is_captain, _is_organizer
    
    # Test with and without tournament parameter
    team_without_tournament = _get_team(user_3)
    team_with_tournament = _get_team(user_3, match.tournament)
    
    print(f'User {user_3.username}:')
    print(f'  is_organizer: {_is_organizer(user_3)}')
    print(f'  _get_team(user) [without tournament]: {team_without_tournament} (pk={team_without_tournament.pk if team_without_tournament else None})')
    print(f'  _get_team(user, tournament) [with tournament]: {team_with_tournament} (pk={team_with_tournament.pk if team_with_tournament else None})')
    print()
    
    # Check form visibility WITHOUT tournament param (OLD CODE)
    team = team_without_tournament
    is_participant = team and ((match.team1 == team) or (match.team2 == team))
    can_submit = is_participant and match.status in ("upcoming", "in_progress")
    can_reschedule = is_participant and _is_captain(user_3, team)
    
    print(f'Form Visibility for {user_3.username} (WITHOUT tournament param - OLD):')
    print(f'  is_participant: {is_participant}')
    print(f'  can_submit: {can_submit}')
    print(f'  can_reschedule: {can_reschedule}')
    print()
    
    # Check form visibility WITH tournament param (NEW CODE - FIXED)
    team = team_with_tournament
    if team:
        print(f'  _is_captain: {_is_captain(user_3, team)}')
    is_participant_fixed = team and ((match.team1 == team) or (match.team2 == team))
    can_submit_fixed = is_participant_fixed and match.status in ("upcoming", "in_progress")
    can_reschedule_fixed = is_participant_fixed and _is_captain(user_3, team)
    
    print(f'Form Visibility for {user_3.username} (WITH tournament param - NEW/FIXED):')
    print(f'  is_participant: {is_participant_fixed}')
    print(f'  can_submit: {can_submit_fixed}')
    print(f'  can_reschedule: {can_reschedule_fixed}')
