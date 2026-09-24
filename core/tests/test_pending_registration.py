"""Teams awaiting organizer approval before they count toward capacity.

Split out of the old core/tests.py — see FOLLOWUP_PLAN.md F-3.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from ..models import (
    Team,
    Tournament,
    TeamMembership,
    TeamTournamentParticipation,
    OrganizerProfile,
)
from ..services.enrollment import active_participant_count


class PendingTeamRegistrationTests(TestCase):
    """Tests for the pending-status team registration workflow."""

    def setUp(self):
        self.organizer = User.objects.create_user(
            username="pending_org", password="pass123", is_staff=True
        )
        OrganizerProfile.objects.update_or_create(
            user=self.organizer,
            defaults={"verified": True, "org_name": "PendingOrg"},
        )
        self.captain = User.objects.create_user(username="pending_captain", password="pass123")

    def _mk_tournament(self, players_per_team=2):
        return Tournament.objects.create(
            name="Pending Test Tournament",
            format="round_robin",
            sport_type="table_tennis",
            registration_mode="team",
            status="registration_open",
            players_per_team=players_per_team,
        )

    def test_new_team_starts_as_pending_when_players_per_team_gt_1(self):
        """Creating a team via view starts it in 'pending' status when players_per_team > 1."""
        tournament = self._mk_tournament(players_per_team=2)
        self.client.force_login(self.captain)
        response = self.client.post(
            reverse("create_team", kwargs={"pk": tournament.pk}),
            {"team_name": "Alpha Team", "department": "Eng"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        participation = TeamTournamentParticipation.objects.get(
            team__name="Alpha Team", tournament=tournament
        )
        self.assertEqual(participation.status, "pending")
        # Capacity count should NOT include this pending team
        self.assertEqual(active_participant_count(tournament), 0)

    def test_single_player_team_mode_stays_active_on_create(self):
        """When players_per_team == 1, a newly created team is immediately 'active'."""
        tournament = self._mk_tournament(players_per_team=1)
        self.client.force_login(self.captain)
        self.client.post(
            reverse("create_team", kwargs={"pk": tournament.pk}),
            {"team_name": "Solo Squad", "department": ""},
            follow=True,
        )
        participation = TeamTournamentParticipation.objects.get(
            team__name="Solo Squad", tournament=tournament
        )
        self.assertEqual(participation.status, "active")

    def test_join_team_upgrades_participation_to_active_when_full(self):
        """Joining the last required slot promotes the team from pending to active."""
        tournament = self._mk_tournament(players_per_team=2)
        # Captain creates the team (pending)
        team = Team.objects.create(name="Beta Team", sport_type="table_tennis")
        TeamTournamentParticipation.objects.create(team=team, tournament=tournament, status="pending")
        TeamMembership.objects.create(team=team, user=self.captain, role="captain")

        # Second user joins — roster becomes full
        second_user = User.objects.create_user(username="pending_member", password="pass123")
        self.client.force_login(second_user)
        response = self.client.post(
            reverse("join_team", kwargs={"tournament_pk": tournament.pk, "team_pk": team.pk}),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        participation = TeamTournamentParticipation.objects.get(team=team, tournament=tournament)
        self.assertEqual(participation.status, "active")
        self.assertEqual(active_participant_count(tournament), 1)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("complete" in m.lower() for m in msgs))

    def test_close_registration_blocked_by_pending_teams(self):
        """close_registration returns errors when pending teams exist."""
        tournament = self._mk_tournament(players_per_team=2)
        # One pending team (incomplete)
        team = Team.objects.create(name="Gamma Team", sport_type="table_tennis")
        TeamTournamentParticipation.objects.create(team=team, tournament=tournament, status="pending")
        TeamMembership.objects.create(team=team, user=self.captain, role="captain")

        self.client.force_login(self.organizer)
        response = self.client.post(
            reverse("close_registration", kwargs={"pk": tournament.pk}),
            follow=True,
        )
        tournament.refresh_from_db()
        # Should stay open — blocked by pending team
        self.assertEqual(tournament.status, "registration_open")
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("forming" in m.lower() or "pending" in m.lower() for m in msgs))

