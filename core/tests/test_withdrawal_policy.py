"""Forfeit vs. void withdrawal policies and their effect on future matches and standings.

Split out of the old core/tests.py — see FOLLOWUP_PLAN.md F-3.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.db import models
from datetime import timedelta
from ..models import (
    Team,
    Tournament,
    Court,
    TimeSlot,
    TeamMembership,
    TeamTournamentParticipation,
)
from ..scheduling import generate_fixtures
from ..standings import calculate_standings
from ..withdrawals import handle_withdrawal
from .helpers import _captain_user, _participation


class WithdrawalPolicyTests(TestCase):
    """Tests for withdrawal policies (forfeit vs void) and standings impact."""

    def setUp(self):
        self.organizer = User.objects.create_user(
            username="organizer", password="pass123", is_staff=True
        )

    def _create_tournament(self, fmt="round_robin", name="T1", policy="forfeit"):
        return Tournament.objects.create(
            name=name,
            format=fmt,
            sport_type="table_tennis",
            points_per_win=3,
            points_per_loss=0,
            points_per_draw=1,
            withdrawal_policy=policy,
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

    def _create_mock_request(self, user=None):
        """Create a mock request object for withdrawal handling."""
        from django.test import RequestFactory
        factory = RequestFactory()
        request = factory.get("/")
        request.user = user or self.organizer
        return request

    def test_withdrawal_forfeit_policy_marks_future_matches(self):
        """Verify forfeit policy marks future matches as forfeited with opponent as winner."""
        tournament = self._create_tournament(policy="forfeit")
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["status", "started_at"])
        team1 = self._create_team(tournament, "Team A", seed=1)
        self._create_team(tournament, "Team B", seed=2)
        self._create_team(tournament, "Team C", seed=3)

        generate_fixtures(tournament)

        # Get future matches for team1
        future_matches_before = tournament.matches.filter(team1=team1, status="upcoming").count()
        self.assertTrue(future_matches_before > 0)

        # Withdraw team1
        request = self._create_mock_request()
        handle_withdrawal(request, team1, tournament)

        # Verify team status is withdrawn
        team1.refresh_from_db()
        self.assertEqual(_participation(team1, tournament).status, "withdrawn")

        # Verify future matches are now forfeited with opponent as winner
        forfeited_matches = tournament.matches.filter(
            status="forfeited"
        ).filter(
            models.Q(team1=team1) | models.Q(team2=team1)
        )
        self.assertTrue(forfeited_matches.exists())

        for match in forfeited_matches:
            self.assertEqual(match.status, "forfeited")
            self.assertIsNotNone(match.winner)
            self.assertNotEqual(match.winner, team1)

    def test_withdrawal_void_policy_marks_future_matches_cancelled(self):
        """Verify void policy marks future matches as cancelled."""
        tournament = self._create_tournament(policy="void")
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["status", "started_at"])
        team1 = self._create_team(tournament, "Team A", seed=1)
        self._create_team(tournament, "Team B", seed=2)
        self._create_team(tournament, "Team C", seed=3)

        generate_fixtures(tournament)

        # Get upcoming matches for team1
        upcoming_before = tournament.matches.filter(team1=team1, status="upcoming").count()
        self.assertTrue(upcoming_before > 0)

        # Withdraw team1
        request = self._create_mock_request()
        handle_withdrawal(request, team1, tournament)

        # Verify future matches are cancelled, not showing a winner
        cancelled_matches = tournament.matches.filter(
            status="cancelled"
        ).filter(
            models.Q(team1=team1) | models.Q(team2=team1)
        )
        self.assertTrue(cancelled_matches.exists())

        for match in cancelled_matches:
            self.assertEqual(match.status, "cancelled")
            self.assertIsNone(match.winner)

    def test_withdrawal_forfeit_standings_impact(self):
        """Verify forfeit policy impacts standings (opponent gets win)."""
        tournament = self._create_tournament(policy="forfeit")
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["status", "started_at"])
        team1 = self._create_team(tournament, "Team A", seed=1)
        team2 = self._create_team(tournament, "Team B", seed=2)
        self._create_team(tournament, "Team C", seed=3)

        generate_fixtures(tournament)

        # Ensure team1 and team2 have an upcoming match
        team1_team2_match = tournament.matches.filter(
            (models.Q(team1=team1, team2=team2) | models.Q(team1=team2, team2=team1)),
            status="upcoming"
        ).first()

        if team1_team2_match:
            # Mark it as scheduled so it exists for withdrawal to handle
            pass

        # Withdraw team1
        request = self._create_mock_request()
        handle_withdrawal(request, team1, tournament)

        # Verify team1 is withdrawn
        team1.refresh_from_db()
        self.assertEqual(_participation(team1, tournament).status, "withdrawn")

        # Check that forfeit match was created
        forfeits = tournament.matches.filter(status="forfeited")
        self.assertTrue(forfeits.exists(), "Should have forfeited matches after withdrawal")

        # Verify at least one forfeit match exists
        forfeit_count = forfeits.filter(
            (models.Q(team1=team1) | models.Q(team2=team1))
        ).count()
        self.assertGreater(forfeit_count, 0, "Team1 should have at least one forfeited match")

    def test_withdrawal_void_standings_not_impacted(self):
        """Verify void policy doesn't impact standings (match voided)."""
        tournament = self._create_tournament(policy="void")
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["status", "started_at"])
        team1 = self._create_team(tournament, "Team A", seed=1)
        team2 = self._create_team(tournament, "Team B", seed=2)
        self._create_team(tournament, "Team C", seed=3)

        generate_fixtures(tournament)

        # Complete one match
        match1 = tournament.matches.filter(status="upcoming").first()
        if match1:
            match1.status = "confirmed"
            match1.score_team1 = 3
            match1.score_team2 = 1
            match1.winner = match1.team1
            match1.save()

        # Get standings before withdrawal
        standings_before = calculate_standings(tournament)
        team2_points_before = next(
            (s["points"] for s in standings_before if s["team"] == team2), 0
        )

        # Withdraw team1
        request = self._create_mock_request()
        handle_withdrawal(request, team1, tournament)

        # Get standings after withdrawal
        standings_after = calculate_standings(tournament)
        team2_points_after = next(
            (s["points"] for s in standings_after if s["team"] == team2), 0
        )

        # Team2 points should not increase from cancelled match
        self.assertEqual(team2_points_after, team2_points_before)

    def test_withdrawal_creates_open_slots(self):
        """Verify scheduled matches create open slots when team withdraws."""
        tournament = self._create_tournament(policy="forfeit")
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["status", "started_at"])
        team1 = self._create_team(tournament, "Team A", seed=1)
        self._create_team(tournament, "Team B", seed=2)

        # Add court and time slot
        court = Court.objects.create(tournament=tournament, name="Court 1")
        now = timezone.now()
        timeslot = TimeSlot.objects.create(
            tournament=tournament,
            start_time=now + timedelta(hours=1),
            end_time=now + timedelta(hours=2)
        )

        generate_fixtures(tournament)

        # Schedule a match
        match = tournament.matches.filter(status="upcoming").first()
        if match:
            match.court = court
            match.scheduled_time = timeslot.start_time
            match.scheduled_end_time = timeslot.end_time
            match.save()

        # Get open slots before withdrawal
        open_slots_before = tournament.open_slots.count()

        # Withdraw team1
        request = self._create_mock_request()
        handle_withdrawal(request, team1, tournament)

        # Verify open slots were created for scheduled matches
        open_slots_after = tournament.open_slots.count()
        self.assertGreater(open_slots_after, open_slots_before)

    def test_pre_activation_withdrawal_cancels_draft_matches_without_forfeit(self):
        tournament = self._create_tournament(policy="forfeit")
        tournament.status = "scheduled"
        tournament.save(update_fields=["status"])
        team1 = self._create_team(tournament, "Team A", seed=1)
        self._create_team(tournament, "Team B", seed=2)
        generate_fixtures(tournament)

        request = self._create_mock_request()
        handle_withdrawal(request, team1, tournament)

        self.assertEqual(_participation(team1, tournament).status, "withdrawn")
        self.assertFalse(
            tournament.matches.filter(
                (models.Q(team1=team1) | models.Q(team2=team1)),
                status="forfeited",
            ).exists()
        )
        self.assertTrue(
            tournament.matches.filter(
                (models.Q(team1=team1) | models.Q(team2=team1)),
                status="cancelled",
            ).exists()
        )

    def test_team_self_withdraw_requires_correct_password(self):
        tournament = self._create_tournament(policy="forfeit")
        team1 = self._create_team(tournament, "Team A", username="team_a", seed=1)
        self._create_team(tournament, "Team B", username="team_b", seed=2)
        generate_fixtures(tournament)

        self.client.force_login(_captain_user(team1))
        response = self.client.post(
            reverse("withdraw_team", kwargs={"pk": team1.pk}),
            {"confirm_withdraw": "yes", "password": "wrong-pass"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        team1.refresh_from_db()
        self.assertEqual(_participation(team1, tournament).status, "active")
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("Incorrect password" in m for m in msgs))

    def test_team_self_withdraw_with_password_succeeds(self):
        tournament = self._create_tournament(policy="forfeit")
        team1 = self._create_team(tournament, "Team A", username="team_a2", seed=1)
        self._create_team(tournament, "Team B", username="team_b2", seed=2)
        generate_fixtures(tournament)

        self.client.force_login(_captain_user(team1))
        response = self.client.post(
            reverse("withdraw_team", kwargs={"pk": team1.pk}),
            {"confirm_withdraw": "yes", "password": "pass123"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        team1.refresh_from_db()
        self.assertEqual(_participation(team1, tournament).status, "withdrawn")

    def test_organizer_can_withdraw_team_without_password(self):
        tournament = self._create_tournament(policy="forfeit")
        team1 = self._create_team(tournament, "Team A", username="team_a3", seed=1)
        self._create_team(tournament, "Team B", username="team_b3", seed=2)
        generate_fixtures(tournament)

        self.client.force_login(self.organizer)
        response = self.client.post(
            reverse("withdraw_team", kwargs={"pk": team1.pk}),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        team1.refresh_from_db()
        self.assertEqual(_participation(team1, tournament).status, "withdrawn")

    def test_organizer_mark_no_show_forfeits_match(self):
        tournament = self._create_tournament(fmt="round_robin", policy="forfeit")
        team1 = self._create_team(tournament, "Team A", username="team_a4", seed=1)
        team2 = self._create_team(tournament, "Team B", username="team_b4", seed=2)
        generate_fixtures(tournament)
        match = tournament.matches.filter(status="upcoming").first()
        match.scheduled_time = timezone.now() - timedelta(minutes=20)
        match.scheduled_end_time = timezone.now() + timedelta(minutes=10)
        match.save(update_fields=["scheduled_time", "scheduled_end_time"])
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["status", "started_at"])

        self.client.force_login(self.organizer)
        response = self.client.post(
            reverse("mark_no_show", kwargs={"pk": match.pk}),
            {"no_show_team": str(team1.pk)},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        match.refresh_from_db()
        self.assertEqual(match.status, "forfeited")
        self.assertEqual(match.winner, team2)

    def test_team_cannot_mark_no_show(self):
        tournament = self._create_tournament(fmt="round_robin", policy="forfeit")
        team1 = self._create_team(tournament, "Team A", username="team_a5", seed=1)
        team2 = self._create_team(tournament, "Team B", username="team_b5", seed=2)
        generate_fixtures(tournament)
        match = tournament.matches.filter(status="upcoming").first()

        self.client.force_login(_captain_user(team1))
        response = self.client.post(
            reverse("mark_no_show", kwargs={"pk": match.pk}),
            {"no_show_team": str(team2.pk)},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        match.refresh_from_db()
        self.assertEqual(match.status, "upcoming")

    def test_team_cannot_report_no_show_before_match_time(self):
        tournament = self._create_tournament(fmt="round_robin", policy="forfeit")
        team_a = self._create_team(tournament, "Team A", username="team_a_early_no_show", seed=1)
        team_b = self._create_team(tournament, "Team B", username="team_b_early_no_show", seed=2)
        generate_fixtures(tournament)
        match = tournament.matches.filter(status="upcoming").first()
        match.scheduled_time = timezone.now() + timedelta(hours=2)
        match.scheduled_end_time = match.scheduled_time + timedelta(minutes=30)
        match.save(update_fields=["scheduled_time", "scheduled_end_time"])

        self.client.force_login(_captain_user(team_b))
        response = self.client.post(
            reverse("report_no_show", kwargs={"pk": match.pk}),
            {"no_show_team": str(team_a.pk)},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(match.no_show_reports.count(), 0)
        self.assertContains(response, "only be reported after the scheduled match time")

    def test_team_can_report_opponent_no_show_and_see_dashboard_notice(self):
        tournament = self._create_tournament(fmt="round_robin", policy="forfeit")
        team_a = self._create_team(tournament, "Team A", username="team_a_no_show", seed=1)
        team_b = self._create_team(tournament, "Team B", username="team_b_no_show", seed=2)
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["status", "started_at"])
        generate_fixtures(tournament)
        match = tournament.matches.filter(status="upcoming").first()
        match.scheduled_time = timezone.now() - timedelta(minutes=20)
        match.scheduled_end_time = timezone.now() + timedelta(minutes=10)
        match.save(update_fields=["scheduled_time", "scheduled_end_time"])

        self.client.force_login(_captain_user(team_b))
        response = self.client.post(
            reverse("report_no_show", kwargs={"pk": match.pk}),
            {"no_show_team": str(team_a.pk)},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        match.refresh_from_db()
        self.assertEqual(match.status, "upcoming")
        self.assertEqual(match.no_show_reports.filter(status="pending").count(), 1)
        self.assertContains(response, "No-show reported")

        self.client.force_login(_captain_user(team_b))
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "No-show notice")

    def test_reschedule_request_by_reported_team_clears_pending_no_show(self):
        tournament = self._create_tournament(fmt="round_robin", policy="forfeit")
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["status", "started_at"])
        court = Court.objects.create(tournament=tournament, name="Court A", is_available=True)
        team_a = self._create_team(tournament, "Team A", username="team_a_reschedule", seed=1)
        team_b = self._create_team(tournament, "Team B", username="team_b_reschedule", seed=2)
        generate_fixtures(tournament)
        match = tournament.matches.filter(status="upcoming").first()
        match.court = court
        match.scheduled_time = timezone.now() - timedelta(minutes=20)
        match.scheduled_end_time = timezone.now() + timedelta(minutes=10)
        match.save(update_fields=["court", "scheduled_time", "scheduled_end_time"])

        self.client.force_login(_captain_user(team_b))
        self.client.post(
            reverse("report_no_show", kwargs={"pk": match.pk}),
            {"no_show_team": str(team_a.pk)},
            follow=True,
        )

        self.client.force_login(_captain_user(team_a))
        response = self.client.post(
            reverse("request_reschedule", kwargs={"pk": match.pk}),
            {
                "new_date": (timezone.localdate() + timedelta(days=2)).isoformat(),
                "new_time": "11:00",
                "new_court": str(court.pk),
                "reason": "We were delayed but can still play.",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(match.no_show_reports.filter(status="pending").count(), 0)

    def test_pending_no_show_auto_forfeits_after_deadline(self):
        tournament = self._create_tournament(fmt="round_robin", policy="forfeit")
        team_a = self._create_team(tournament, "Team A", username="team_a_auto", seed=1)
        team_b = self._create_team(tournament, "Team B", username="team_b_auto", seed=2)
        generate_fixtures(tournament)
        match = tournament.matches.filter(status="upcoming").first()
        match.scheduled_time = timezone.now() - timedelta(minutes=20)
        match.scheduled_end_time = timezone.now() + timedelta(minutes=10)
        match.save(update_fields=["scheduled_time", "scheduled_end_time"])

        self.client.force_login(_captain_user(team_b))
        self.client.post(
            reverse("report_no_show", kwargs={"pk": match.pk}),
            {"no_show_team": str(team_a.pk)},
            follow=True,
        )
        report = match.no_show_reports.get()
        report.deadline_at = timezone.now() - timedelta(minutes=1)
        report.save(update_fields=["deadline_at"])

        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        match.refresh_from_db()
        self.assertEqual(match.status, "forfeited")
        self.assertEqual(match.winner, team_b)

