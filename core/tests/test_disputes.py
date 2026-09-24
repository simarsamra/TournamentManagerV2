"""The score-dispute window: raising, resolving, and its deadline.

Split out of the old core/tests.py — see FOLLOWUP_PLAN.md F-3.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from ..models import (
    Team,
    Tournament,
    Match,
    TeamMembership,
    TeamTournamentParticipation,
)
from .helpers import _captain_user, _participation


class ScoreDisputeWindowTests(TestCase):
    def setUp(self):
        self.organizer = User.objects.create_user(
            username="deadline_org", password="pass123", is_staff=True
        )

    def _create_tournament(self, fmt="round_robin", name="Deadline Tournament"):
        return Tournament.objects.create(
            name=name,
            format=fmt,
            sport_type="table_tennis",
            status="active",
            points_per_win=3,
            points_per_loss=0,
            points_per_draw=1,
            teams_per_group_advance=1,
            num_groups=2,
        )

    def _create_team_with_membership(self, tournament, team_name, username):
        user = User.objects.create_user(username=username, password="pass123")
        team, _ = Team.objects.get_or_create(name=team_name)
        TeamTournamentParticipation.objects.get_or_create(team=team, tournament=tournament, defaults={"status": "active"})
        TeamMembership.objects.get_or_create(team=team, user=user, defaults={"role": "captain"})
        return team

    def _create_match(self, tournament, team1, team2, group=""):
        return Match.objects.create(
            tournament=tournament,
            match_number=1,
            team1=team1,
            team2=team2,
            group=group,
            status="upcoming",
            scheduled_time=timezone.now() - timedelta(hours=1),
        )

    def test_submit_score_starts_dispute_window(self):
        t = self._create_tournament()
        team1 = self._create_team_with_membership(t, "Alpha", "alpha_deadline")
        team2 = self._create_team_with_membership(t, "Beta", "beta_deadline")
        match = self._create_match(t, team1, team2)

        self.client.force_login(_captain_user(team1))
        self.client.post(
            reverse("submit_score", kwargs={"pk": match.pk}),
            {"score_team1": 2, "score_team2": 1},
            follow=True,
        )

        match.refresh_from_db()
        self.assertEqual(match.status, "pending_confirmation")
        self.assertEqual(match.submitted_by, _captain_user(team1))
        self.assertIsNotNone(match.dispute_deadline_at)
        self.assertIsNotNone(match.score_submitted_at)

    def test_dashboard_shows_dispute_window_notification_and_auto_lock_after_deadline(self):
        t = self._create_tournament(name="Deadline Auto Lock")
        team1 = self._create_team_with_membership(t, "Gamma", "gamma_deadline")
        team2 = self._create_team_with_membership(t, "Delta", "delta_deadline")
        match = self._create_match(t, team1, team2)
        match.status = "pending_confirmation"
        match.score_team1 = 3
        match.score_team2 = 2
        match.submitted_by = _captain_user(team1)
        match.score_submitted_at = timezone.now() - timedelta(hours=1)
        match.dispute_deadline_at = timezone.now() + timedelta(hours=2)
        match.save()

        self.client.force_login(_captain_user(team2))
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Score Dispute Window Open")
        self.assertContains(response, f"Match #{match.match_number}")

        match.dispute_deadline_at = timezone.now() - timedelta(minutes=1)
        match.save(update_fields=["dispute_deadline_at"])
        self.client.get(reverse("dashboard"))
        match.refresh_from_db()
        self.assertEqual(match.status, "confirmed")
        self.assertIsNotNone(match.score_locked_at)

    def test_team_can_lock_score_within_window(self):
        t = self._create_tournament(name="Manual Lock")
        team1 = self._create_team_with_membership(t, "Epsilon", "epsilon_deadline")
        team2 = self._create_team_with_membership(t, "Zeta", "zeta_deadline")
        match = self._create_match(t, team1, team2)
        match.status = "pending_confirmation"
        match.score_team1 = 1
        match.score_team2 = 0
        match.submitted_by = _captain_user(team1)
        match.score_submitted_at = timezone.now() - timedelta(hours=1)
        match.dispute_deadline_at = timezone.now() + timedelta(hours=3)
        match.save()

        self.client.force_login(_captain_user(team2))
        self.client.post(reverse("confirm_score", kwargs={"pk": match.pk}), follow=True)
        match.refresh_from_db()
        self.assertEqual(match.status, "confirmed")
        self.assertEqual(match.confirmed_by, _captain_user(team2))
        self.assertIsNotNone(match.score_locked_at)

    def test_critical_dispute_requires_resolution_notes(self):
        t = self._create_tournament(fmt="hybrid", name="Critical Hybrid")
        team1 = self._create_team_with_membership(t, "Eta", "eta_deadline")
        team2 = self._create_team_with_membership(t, "Theta", "theta_deadline")
        participation1 = _participation(team1, t)
        participation2 = _participation(team2, t)
        participation1.group = "A"
        participation2.group = "A"
        participation1.save(update_fields=["group"])
        participation2.save(update_fields=["group"])
        match = self._create_match(t, team1, team2, group="A")
        match.status = "pending_confirmation"
        match.score_team1 = 2
        match.score_team2 = 1
        match.submitted_by = _captain_user(team1)
        match.score_submitted_at = timezone.now()
        match.dispute_deadline_at = timezone.now() + timedelta(hours=1)
        match.save()

        self.client.force_login(_captain_user(team2))
        self.client.post(
            reverse("dispute_score", kwargs={"pk": match.pk}),
            {"dispute_notes": "Incorrect score"},
            follow=True,
        )
        match.refresh_from_db()
        self.assertEqual(match.status, "disputed")
        self.assertTrue(match.critical_dispute)

        self.client.force_login(self.organizer)
        self.client.post(
            reverse("resolve_dispute", kwargs={"pk": match.pk}),
            {"final_score_team1": "2", "final_score_team2": "1", "resolution_notes": ""},
            follow=True,
        )
        match.refresh_from_db()
        self.assertEqual(match.status, "disputed")

        self.client.post(
            reverse("resolve_dispute", kwargs={"pk": match.pk}),
            {
                "final_score_team1": "2",
                "final_score_team2": "1",
                "resolution_notes": "Video review and scorecards verified.",
            },
            follow=True,
        )
        match.refresh_from_db()
        self.assertEqual(match.status, "confirmed")
        self.assertEqual(match.dispute_resolution_notes, "Video review and scorecards verified.")

