"""Shared fixtures for the modules split out of the old core/tests.py.

Only the seven UX/regression modules below use this base class. Every other
core/tests_*.py module at the top level (tests_standings.py,
tests_concurrency.py, and so on) defines its own fixtures and is out of
scope for this split — see FOLLOWUP_PLAN.md F-3.
"""
from django.contrib.auth.models import User
from django.test import TestCase

from ..models import Team, TeamMembership, TeamTournamentParticipation, Tournament


def _captain_user(team):
    return TeamMembership.objects.get(team=team, role="captain").user


def _participation(team, tournament):
    return TeamTournamentParticipation.objects.get(team=team, tournament=tournament)


class UXRegressionTestCase(TestCase):
    """Base class carrying the fixtures every split-out module used.

    The original UXAndLogicRegressionTests defined `self.organizer` plus
    these two factories once, and all 80 of its tests used them. Splitting
    the class without sharing this would mean copy-pasting the same three
    methods into seven files.
    """

    def setUp(self):
        self.organizer = User.objects.create_user(
            username="organizer", password="pass123", is_staff=True
        )

    def _create_tournament(self, fmt="round_robin", name="T1"):
        return Tournament.objects.create(
            name=name,
            format=fmt,
            sport_type="table_tennis",
            points_per_win=3,
            points_per_loss=0,
            points_per_draw=1,
            teams_per_group_advance=1,
            num_groups=2,
            default_match_duration=30,
        )

    def _create_team(self, tournament, team_name, username=None, seed=0):
        username = username or team_name.lower().replace(" ", "_")
        user = User.objects.create_user(username=username, password="pass123")
        team, _ = Team.objects.get_or_create(name=team_name)
        TeamTournamentParticipation.objects.get_or_create(
            team=team, tournament=tournament, defaults={"status": "active", "seed": seed}
        )
        TeamMembership.objects.get_or_create(team=team, user=user, defaults={"role": "captain"})
        return team
