"""Guards for organizer-driven team removal.

organizer_remove_team used to 500 on every call: it read team.tournament,
team.user and user.captained_teams, none of which survived the global-team
schema change. It was reachable from two templates and had no test coverage.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from core.models import (
    Team, TeamMembership, TeamTournamentParticipation, Tournament,
)


class OrganizerTeamRemovalTests(TestCase):
    def setUp(self):
        self.org = User.objects.create_user(
            username="org", password="Regression-Pass-1", is_staff=True
        )
        self.tournament = Tournament.objects.create(
            name="RM", format="round_robin", status="registration_open",
            players_per_team=1,
        )
        self.team = Team.objects.create(name="Alpha")
        self.participation = TeamTournamentParticipation.objects.create(
            team=self.team, tournament=self.tournament, status="active"
        )
        self.captain = User.objects.create_user(
            username="cap", password="Regression-Pass-1"
        )
        TeamMembership.objects.create(team=self.team, user=self.captain, role="captain")

    def _remove_url(self):
        return reverse(
            "remove_team_from_tournament",
            kwargs={"pk": self.tournament.pk, "participation_pk": self.participation.pk},
        )

    def test_removal_succeeds_and_keeps_team_and_accounts(self):
        self.client.force_login(self.org)
        response = self.client.post(self._remove_url(), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            TeamTournamentParticipation.objects.filter(pk=self.participation.pk).exists()
        )
        # The team and its members are global; removal must not delete them.
        self.assertTrue(Team.objects.filter(pk=self.team.pk).exists())
        self.assertTrue(User.objects.filter(username="cap").exists())
        self.assertTrue(TeamMembership.objects.filter(team=self.team).exists())

    def test_removal_is_blocked_once_the_tournament_is_active(self):
        self.tournament.status = "active"
        self.tournament.save(update_fields=["status"])
        self.client.force_login(self.org)

        self.client.post(self._remove_url(), follow=True)

        self.assertTrue(
            TeamTournamentParticipation.objects.filter(pk=self.participation.pk).exists()
        )

    def test_non_organizer_cannot_remove(self):
        self.client.force_login(self.captain)
        self.client.post(self._remove_url(), follow=True)
        self.assertTrue(
            TeamTournamentParticipation.objects.filter(pk=self.participation.pk).exists()
        )

    def test_broken_route_is_gone(self):
        with self.assertRaises(NoReverseMatch):
            reverse("organizer_remove_team", kwargs={"pk": self.team.pk})


class TeamsPageTests(TestCase):
    """The teams table rendered {{ t.user.username }}, a field that no longer
    exists, so the Account column was permanently blank."""

    def test_account_column_shows_the_captain(self):
        org = User.objects.create_user(
            username="org2", password="Regression-Pass-1", is_staff=True
        )
        tournament = Tournament.objects.create(
            name="TP", format="round_robin", status="registration_open",
            players_per_team=1,
        )
        team = Team.objects.create(name="Bravo")
        TeamTournamentParticipation.objects.create(
            team=team, tournament=tournament, status="active"
        )
        captain = User.objects.create_user(
            username="bravo_cap", password="Regression-Pass-1"
        )
        TeamMembership.objects.create(team=team, user=captain, role="captain")

        self.client.force_login(org)
        session = self.client.session
        session["selected_tournament_id"] = tournament.pk
        session.save()

        response = self.client.get(reverse("teams"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "bravo_cap")
