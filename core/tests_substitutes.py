"""Substitutes must be scoped to one tournament.

They used to be plain TeamMemberships with role="sub". TeamMembership has no
tournament scope, so a sub joined the team in every tournament it competed in,
counted toward every roster-size check, and could act for the team anywhere.
"""
from django.contrib.auth.models import User
from django.test import TestCase

from core.models import (
    OrganizerProfile, Team, TeamMembership, TeamTournamentParticipation,
    Tournament, TournamentSubstitute,
)
from core.views import _get_team, _validate_tournament_ready


class SubstituteScopingTests(TestCase):
    def setUp(self):
        self.organizer = User.objects.create_user(
            username="org", password="Regression-Pass-1"
        )
        OrganizerProfile.objects.filter(user=self.organizer).update(verified=True)

        self.team = Team.objects.create(name="Alpha")
        self.captain = User.objects.create_user(
            username="cap", password="Regression-Pass-1"
        )
        TeamMembership.objects.create(team=self.team, user=self.captain, role="captain")

        # The team competes in two tournaments run by the same organizer.
        self.t1 = Tournament.objects.create(
            name="First", format="round_robin", status="registration_open",
            players_per_team=1, created_by=self.organizer,
        )
        self.t2 = Tournament.objects.create(
            name="Second", format="round_robin", status="registration_open",
            players_per_team=1, created_by=self.organizer,
        )
        self.p1 = TeamTournamentParticipation.objects.create(
            team=self.team, tournament=self.t1, status="active"
        )
        self.p2 = TeamTournamentParticipation.objects.create(
            team=self.team, tournament=self.t2, status="active"
        )
        self.sub = User.objects.create_user(
            username="standin", password="Regression-Pass-1"
        )
        self.client.force_login(self.organizer)

    def _add_sub(self, participation, username="standin"):
        return self.client.post(
            f"/tournament/{participation.tournament.pk}/teams/{participation.pk}/sub/",
            {"action": "add", "username": username},
        )

    def test_adding_a_sub_does_not_change_roster_size(self):
        """players_per_team is 1 and the captain fills it; a sub must not make
        the roster look like 2 and break the exact-N rule."""
        before = self.team.memberships.count()
        self._add_sub(self.p1)

        self.assertEqual(self.team.memberships.count(), before)
        self.assertTrue(TournamentSubstitute.objects.filter(participation=self.p1).exists())
        self.assertEqual(
            [e for e in _validate_tournament_ready(self.t1) if "members" in e], []
        )

    def test_sub_can_act_for_the_team_in_their_own_tournament(self):
        self._add_sub(self.p1)
        self.assertEqual(_get_team(self.sub, self.t1), self.team)

    def test_sub_cannot_act_for_the_team_in_another_tournament(self):
        self._add_sub(self.p1)
        self.assertIsNone(
            _get_team(self.sub, self.t2),
            "a substitute added for one tournament must not gain rights in another",
        )

    def test_removing_a_sub_is_scoped(self):
        self._add_sub(self.p1)
        self._add_sub(self.p2)
        self.assertEqual(TournamentSubstitute.objects.count(), 2)

        sub_row = TournamentSubstitute.objects.get(participation=self.p1)
        self.client.post(
            f"/tournament/{self.t1.pk}/teams/{self.p1.pk}/sub/",
            {"action": "remove", "sub_pk": sub_row.pk},
        )

        self.assertFalse(TournamentSubstitute.objects.filter(participation=self.p1).exists())
        self.assertTrue(TournamentSubstitute.objects.filter(participation=self.p2).exists())

    def test_cannot_add_an_existing_member_as_a_sub(self):
        self._add_sub(self.p1, username="cap")
        self.assertEqual(TournamentSubstitute.objects.count(), 0)

    def test_cannot_add_someone_already_competing_in_that_tournament(self):
        rival = Team.objects.create(name="Bravo")
        TeamTournamentParticipation.objects.create(
            team=rival, tournament=self.t1, status="active"
        )
        rival_player = User.objects.create_user(
            username="rival", password="Regression-Pass-1"
        )
        TeamMembership.objects.create(team=rival, user=rival_player, role="captain")

        self._add_sub(self.p1, username="rival")
        self.assertEqual(TournamentSubstitute.objects.count(), 0)

    def test_no_sub_role_memberships_are_created(self):
        self._add_sub(self.p1)
        self.assertFalse(TeamMembership.objects.filter(role="sub").exists())
