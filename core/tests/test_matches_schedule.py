"""Publishing a schedule, start-tournament preconditions, and match-detail display.

Split out of the old core/tests.py (UXAndLogicRegressionTests) — see
FOLLOWUP_PLAN.md F-3.
"""
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from ..models import (
    Match,
    Court,
    CourtAvailability,
    Player,
    TeamTournamentParticipation,
    TeamTournamentCourtPreference,
)
from ..scheduling import generate_fixtures

from .helpers import UXRegressionTestCase, _captain_user


class ScheduleGenerationAndMatchDetailTests(UXRegressionTestCase):

    def test_fixtures_invalid_page_query_does_not_crash(self):
        tournament = self._create_tournament()
        self._create_team(tournament, "A")
        self._create_team(tournament, "B")
        self.client.force_login(self.organizer)

        response = self.client.get(reverse("fixtures"), {"page": "abc"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("matches", response.context)

    def test_organizer_generates_schedule_draft_then_publishes_tournament(self):
        tournament = self._create_tournament(name="Draft Flow")
        tournament.expected_teams_count = 2
        tournament.start_date = timezone.localdate() + timedelta(days=1)
        tournament.save(update_fields=["expected_teams_count", "start_date"])
        court = Court.objects.create(tournament=tournament, name="Court 1", is_available=True)
        CourtAvailability.objects.create(
            court=court,
            weekday=tournament.start_date.weekday(),
            start_time="12:00",
            end_time="14:00",
            start_date=tournament.start_date,
        )
        for name in ("Team A", "Team B"):
            team = self._create_team(tournament, name)
            Player.objects.create(team=team, name=f"{name} Player")
            p = TeamTournamentParticipation.objects.get(team=team, tournament=tournament)
            TeamTournamentCourtPreference.objects.get_or_create(participation=p, court=court)
        self.client.force_login(self.organizer)

        self.client.post(reverse("open_registration", kwargs={"pk": tournament.pk}), follow=True)
        self.client.post(reverse("close_registration", kwargs={"pk": tournament.pk}), follow=True)
        draft_response = self.client.post(reverse("generate_schedule", kwargs={"pk": tournament.pk}), follow=True)

        self.assertEqual(draft_response.status_code, 200)
        tournament.refresh_from_db()
        self.assertEqual(tournament.status, "scheduled")
        self.assertGreater(tournament.matches.count(), 0)

        publish_response = self.client.post(reverse("start_tournament", kwargs={"pk": tournament.pk}), follow=True)
        self.assertEqual(publish_response.status_code, 200)
        tournament.refresh_from_db()
        self.assertEqual(tournament.status, "active")
        self.assertIsNotNone(tournament.started_at)

    def test_start_tournament_requires_expected_team_count_and_preferences(self):
        tournament = self._create_tournament(name="Strict Start")
        tournament.expected_teams_count = 4
        tournament.players_per_team = 1
        tournament.start_date = timezone.localdate() + timedelta(days=1)
        tournament.save(update_fields=["expected_teams_count", "players_per_team", "start_date"])
        court = Court.objects.create(tournament=tournament, name="Center Court", is_available=True)
        CourtAvailability.objects.create(
            court=court,
            weekday=(timezone.localdate() + timedelta(days=1)).weekday(),
            start_time="12:00",
            end_time="14:00",
            start_date=timezone.localdate() + timedelta(days=1),
        )
        team1 = self._create_team(tournament, "A")
        team2 = self._create_team(tournament, "B")
        Player.objects.create(team=team1, name="P1")
        Player.objects.create(team=team2, name="P2")
        self.client.force_login(self.organizer)

        response = self.client.post(
            reverse("start_tournament", kwargs={"pk": tournament.pk}),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        tournament.refresh_from_db()
        self.assertEqual(tournament.status, "setup")
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("expected" in m.lower() for m in msgs))

        team3 = self._create_team(tournament, "C")
        team4 = self._create_team(tournament, "D")
        for team in (team3, team4):
            Player.objects.create(team=team, name=f"{team.name} Player")
            p = TeamTournamentParticipation.objects.get(team=team, tournament=tournament)
            TeamTournamentCourtPreference.objects.get_or_create(participation=p, court=court)
        response = self.client.post(
            reverse("start_tournament", kwargs={"pk": tournament.pk}),
            follow=True,
        )
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("preference" in m.lower() for m in msgs))

    def test_start_tournament_requires_full_rosters(self):
        tournament = self._create_tournament(name="Roster Check")
        tournament.expected_teams_count = 2
        tournament.players_per_team = 2
        tournament.start_date = timezone.localdate() + timedelta(days=1)
        tournament.save(update_fields=["expected_teams_count", "players_per_team", "start_date"])
        court = Court.objects.create(tournament=tournament, name="Court 2", is_available=True)
        CourtAvailability.objects.create(
            court=court,
            weekday=tournament.start_date.weekday(),
            start_time="12:00",
            end_time="14:00",
            start_date=tournament.start_date,
        )
        for name in ("Red", "Blue"):
            team = self._create_team(tournament, name)
            Player.objects.create(team=team, name=f"{name} Player 1")
            p = TeamTournamentParticipation.objects.get(team=team, tournament=tournament)
            TeamTournamentCourtPreference.objects.get_or_create(participation=p, court=court)
        self.client.force_login(self.organizer)

        response = self.client.post(
            reverse("start_tournament", kwargs={"pk": tournament.pk}),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("enough members" in m.lower() for m in msgs))

    def test_confirming_match_ahead_of_schedule_creates_open_slot(self):
        tournament = self._create_tournament(name="Early Finish Opens Slot")
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["status", "started_at"])
        court = Court.objects.create(tournament=tournament, name="Court 1", is_available=True)
        team1 = self._create_team(tournament, "Alpha", username="alpha_open_slot")
        team2 = self._create_team(tournament, "Beta", username="beta_open_slot")
        match = Match.objects.create(
            tournament=tournament,
            match_number=1,
            team1=team1,
            team2=team2,
            court=court,
            scheduled_time=timezone.now() + timedelta(days=1),
            scheduled_end_time=timezone.now() + timedelta(days=1, minutes=30),
            status="upcoming",
        )

        self.client.force_login(_captain_user(team1))
        submit_response = self.client.post(
            reverse("submit_score", kwargs={"pk": match.pk}),
            {"score_team1": 3, "score_team2": 1, "notes": "Played early"},
            follow=True,
        )
        self.assertEqual(submit_response.status_code, 200)

        self.client.force_login(_captain_user(team2))
        confirm_response = self.client.post(reverse("confirm_score", kwargs={"pk": match.pk}), follow=True)

        self.assertEqual(confirm_response.status_code, 200)
        match.refresh_from_db()
        self.assertEqual(match.status, "confirmed")
        self.assertEqual(tournament.open_slots.count(), 1)
        slot = tournament.open_slots.first()
        self.assertEqual(slot.court, court)
        self.assertEqual(slot.start_time, match.scheduled_time)
        self.assertEqual(slot.end_time, match.scheduled_end_time)

    def test_open_slots_view_syncs_completed_future_matches(self):
        tournament = self._create_tournament(name="Synced Open Slots")
        court = Court.objects.create(tournament=tournament, name="Court Sync", is_available=True)
        team1 = self._create_team(tournament, "Sync A", username="sync_a_user")
        team2 = self._create_team(tournament, "Sync B", username="sync_b_user")
        Match.objects.create(
            tournament=tournament,
            match_number=2,
            team1=team1,
            team2=team2,
            court=court,
            scheduled_time=timezone.now() + timedelta(days=2),
            scheduled_end_time=timezone.now() + timedelta(days=2, minutes=30),
            status="confirmed",
            winner=team1,
        )

        self.client.force_login(self.organizer)
        response = self.client.get(reverse("open_slots"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["slots"]), 1)
        slot = response.context["slots"][0]
        self.assertEqual(slot.court, court)

    def test_match_detail_shows_done_label_for_confirmed_status(self):
        tournament = self._create_tournament(name="Done Label")
        court = Court.objects.create(tournament=tournament, name="Center Court", is_available=True)
        team1 = self._create_team(tournament, "Done A", username="done_a_user")
        team2 = self._create_team(tournament, "Done B", username="done_b_user")
        match = Match.objects.create(
            tournament=tournament,
            match_number=43,
            team1=team1,
            team2=team2,
            court=court,
            scheduled_time=timezone.now() + timedelta(days=1),
            scheduled_end_time=timezone.now() + timedelta(days=1, minutes=30),
            status="confirmed",
        )

        self.client.force_login(_captain_user(team1))
        response = self.client.get(reverse("match_detail", kwargs={"pk": match.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Done")

    def test_knockout_disallows_draw_on_confirm(self):
        tournament = self._create_tournament(fmt="knockout")
        team1 = self._create_team(tournament, "Red", seed=1)
        team2 = self._create_team(tournament, "Blue", seed=2)
        generate_fixtures(tournament)
        match = tournament.matches.first()
        match.status = "pending_confirmation"
        match.score_team1 = 2
        match.score_team2 = 2
        match.submitted_by = _captain_user(team1)
        match.score_submitted_at = timezone.now()
        match.dispute_deadline_at = timezone.now() + timedelta(hours=24)
        match.save(update_fields=[
            "status", "score_team1", "score_team2", "submitted_by",
            "score_submitted_at", "dispute_deadline_at",
        ])

        self.client.force_login(_captain_user(team2))
        response = self.client.post(reverse("confirm_score", kwargs={"pk": match.pk}), follow=True)

        self.assertEqual(response.status_code, 200)
        match.refresh_from_db()
        self.assertEqual(match.status, "pending_confirmation")
        self.assertIsNone(match.winner)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("Draws are not allowed" in m for m in msgs))
