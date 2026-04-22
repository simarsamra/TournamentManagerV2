#!/usr/bin/env python
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tournament_manager.settings")
django.setup()

from core.models import Match, Team
from core.views import _get_team
from django.contrib.auth.models import User

# Get match 195
match = Match.objects.get(match_number=195)
tournament = match.tournament

print(f'Match #195 Tournament: {tournament.name} (pk={tournament.pk})')
print()

# Get Team 3 from this tournament
team_3_in_tt1 = tournament.teams.get(name='Team 3')
print(f'Team 3 in {tournament.name}: pk={team_3_in_tt1.pk}')
print()

# Get Team 3 users
team_3_members = [m.user for m in team_3_in_tt1.memberships.all()]
user = team_3_members[0]

print(f'User: {user.username}')
print(f'User teams:')
for m in user.memberships.all().select_related('team__tournament'):
    print(f'  - {m.team.name} (pk={m.team.pk}) in {m.team.tournament.name} - status: {m.team.status}')
print()

# Check what _get_team returns
team_from_get_team = _get_team(user)
print(f'_get_team(user) returns: {team_from_get_team}')
print(f'  pk: {team_from_get_team.pk if team_from_get_team else None}')
print(f'  tournament: {team_from_get_team.tournament.name if team_from_get_team else None}')
print()

# The issue:
print(f'ISSUE:')
print(f'  Match is in tournament: {match.tournament.name} (pk={match.tournament.pk})')
print(f'  Team 3 (in match) is: pk={team_3_in_tt1.pk}')
print(f'  _get_team returns: pk={team_from_get_team.pk if team_from_get_team else None}')
print(f'  Are they the same team? {team_from_get_team == team_3_in_tt1}')
print()

# The condition in match_detail
print(f'MATCH DETAIL VIEW CONDITION:')
is_participant = team_from_get_team and ((match.team1 == team_from_get_team) or (match.team2 == team_from_get_team))
print(f'  is_participant: {is_participant}')
print(f'    team_from_get_team: {team_from_get_team}')
print(f'    match.team1: {match.team1}')
print(f'    match.team2: {match.team2}')
