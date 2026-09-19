"""Guards for registration and roster rules."""
from django.contrib.auth.models import User
from django.test import TestCase

from core.models import (
    Court, Team, TeamInvite, TeamMembership, TeamTournamentCourtPreference,
    TeamTournamentParticipation, Tournament,
)
from core.views import _validate_tournament_ready


class CreateTeamCourtPreferenceTests(TestCase):
    """CreateTeamForm requires a court selection when the tournament has courts,
    but create_team_view never read cleaned_data["preferred_courts"], so the
    choice was discarded and the readiness check then blocked the start on it."""

    def test_preferred_courts_are_saved(self):
        tournament = Tournament.objects.create(
            name="CT", format="round_robin", status="registration_open",
            players_per_team=1,
        )
        court = Court.objects.create(tournament=tournament, name="C1")
        user = User.objects.create_user(username="cap", password="Regression-Pass-1")
        self.client.force_login(user)

        self.client.post(
            f"/tournament/{tournament.pk}/create-team/",
            {"team_name": "Alpha", "department": "", "preferred_courts": [str(court.pk)]},
            follow=True,
        )

        team = Team.objects.get(name="Alpha")
        self.assertEqual(
            TeamTournamentCourtPreference.objects.filter(
                participation__team=team, participation__tournament=tournament
            ).count(),
            1,
        )
        self.assertEqual(
            [e for e in _validate_tournament_ready(tournament) if "preference" in e],
            [],
            "readiness must not demand preferences the captain just supplied",
        )


class TeamInviteRosterRulesTests(TestCase):
    """accept_team_invite checked only for an existing membership on that team.
    join_team_view checks team fullness and one-team-per-tournament; the invite
    path checked neither."""

    def setUp(self):
        self.tournament = Tournament.objects.create(
            name="IV", format="round_robin", status="registration_open",
            players_per_team=2,
        )
        self.team_a = Team.objects.create(name="A")
        self.team_b = Team.objects.create(name="B")
        for team in (self.team_a, self.team_b):
            TeamTournamentParticipation.objects.create(
                team=team, tournament=self.tournament, status="active"
            )
        self.cap_a = User.objects.create_user(
            username="capa", password="Regression-Pass-1"
        )
        self.cap_b = User.objects.create_user(
            username="capb", password="Regression-Pass-1"
        )
        self.member = User.objects.create_user(
            username="m1", password="Regression-Pass-1"
        )
        TeamMembership.objects.create(team=self.team_a, user=self.cap_a, role="captain")
        TeamMembership.objects.create(team=self.team_b, user=self.cap_b, role="captain")
        TeamMembership.objects.create(team=self.team_b, user=self.member, role="member")

    def test_cannot_join_a_second_team_in_the_same_tournament(self):
        invite = TeamInvite.objects.create(
            team=self.team_a, invited_user=self.member, invited_by=self.cap_a
        )
        self.client.force_login(self.member)
        self.client.post(f"/team-invite/{invite.pk}/accept/")

        teams = set(
            TeamMembership.objects.filter(user=self.member).values_list(
                "team__name", flat=True
            )
        )
        self.assertEqual(teams, {"B"})

    def test_invite_stays_pending_so_it_can_be_retried(self):
        invite = TeamInvite.objects.create(
            team=self.team_a, invited_user=self.member, invited_by=self.cap_a
        )
        self.client.force_login(self.member)
        self.client.post(f"/team-invite/{invite.pk}/accept/")

        invite.refresh_from_db()
        self.assertEqual(invite.status, "pending")

    def test_cannot_exceed_players_per_team(self):
        filler = User.objects.create_user(
            username="m2", password="Regression-Pass-1"
        )
        TeamMembership.objects.create(team=self.team_a, user=filler, role="member")
        self.assertEqual(TeamMembership.objects.filter(team=self.team_a).count(), 2)

        newcomer = User.objects.create_user(
            username="m3", password="Regression-Pass-1"
        )
        invite = TeamInvite.objects.create(
            team=self.team_a, invited_user=newcomer, invited_by=self.cap_a
        )
        self.client.force_login(newcomer)
        self.client.post(f"/team-invite/{invite.pk}/accept/")

        self.assertEqual(TeamMembership.objects.filter(team=self.team_a).count(), 2)

    def test_a_legitimate_invite_still_works(self):
        newcomer = User.objects.create_user(
            username="fresh", password="Regression-Pass-1"
        )
        invite = TeamInvite.objects.create(
            team=self.team_a, invited_user=newcomer, invited_by=self.cap_a
        )
        self.client.force_login(newcomer)
        self.client.post(f"/team-invite/{invite.pk}/accept/")

        invite.refresh_from_db()
        self.assertEqual(invite.status, "accepted")
        self.assertTrue(
            TeamMembership.objects.filter(team=self.team_a, user=newcomer).exists()
        )
