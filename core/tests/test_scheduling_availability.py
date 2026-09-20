"""Courts, availability rows, timeslots, and the end-date/slot estimators.

Split out of the old core/tests.py (UXAndLogicRegressionTests) — see
FOLLOWUP_PLAN.md F-3.
"""
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from django.db import models
from datetime import timedelta, date
from ..models import (
    Court,
    CourtAvailability,
    Player,
    TeamTournamentParticipation,
    TeamTournamentCourtPreference,
)
from ..scheduling import generate_fixtures
from ..scheduling import count_available_slots

from .helpers import UXRegressionTestCase


class CourtAvailabilityAndSchedulingTests(UXRegressionTestCase):
    def _next_weekday_on_or_after(self, start_date, weekday):
        """Return the first date >= start_date falling on `weekday` (0=Monday)."""
        offset = (weekday - start_date.weekday()) % 7
        return start_date + timedelta(days=offset)

    def test_add_timeslot_rejects_end_before_start(self):
        tournament = self._create_tournament()
        self.client.force_login(self.organizer)

        response = self.client.post(
            reverse("add_timeslot", kwargs={"pk": tournament.pk}),
            {
                "date": "2026-04-20",
                "start_time": "11:00",
                "end_time": "10:00",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(tournament.time_slots.count(), 0)
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("End time must be after start time" in m for m in messages))

    def test_add_court_availability_supports_bulk_creation_and_skips_duplicates(self):
        tournament = self._create_tournament(name="Bulk Availability")
        court1 = Court.objects.create(tournament=tournament, name="Court A", is_available=True)
        court2 = Court.objects.create(tournament=tournament, name="Court B", is_available=True)
        self.client.force_login(self.organizer)

        payload = {
            "courts": [str(court1.pk), str(court2.pk)],
            "weekdays": ["0", "2"],
            "start_time": "09:00",
            "end_time": "11:00",
            "start_date": "2026-04-20",
            "end_date": "2026-04-30",
            "is_active": "on",
        }

        response = self.client.post(
            reverse("add_court_availability", kwargs={"pk": tournament.pk}),
            payload,
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(CourtAvailability.objects.filter(court__tournament=tournament).count(), 4)
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("4" in m and "availability" in m.lower() for m in messages))

        duplicate_response = self.client.post(
            reverse("add_court_availability", kwargs={"pk": tournament.pk}),
            payload,
            follow=True,
        )

        self.assertEqual(duplicate_response.status_code, 200)
        self.assertEqual(CourtAvailability.objects.filter(court__tournament=tournament).count(), 4)
        duplicate_messages = [str(m) for m in duplicate_response.context["messages"]]
        self.assertTrue(any("skipped" in m.lower() for m in duplicate_messages))

    def test_add_court_availability_stores_matches_per_court_per_day(self):
        tournament = self._create_tournament(name="Matches Per Day")
        court = Court.objects.create(tournament=tournament, name="Court A", is_available=True)
        self.client.force_login(self.organizer)

        response = self.client.post(
            reverse("add_court_availability", kwargs={"pk": tournament.pk}),
            {
                "courts": [str(court.pk)],
                "weekdays": ["0"],
                "start_time": "09:00",
                "end_time": "12:00",
                "start_date": "2026-05-01",
                "matches_per_court_per_day": "2",
                "is_active": "on",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        availability = CourtAvailability.objects.get(court=court)
        self.assertEqual(availability.matches_per_court_per_day, 2)

    def test_add_court_availability_supports_additional_start_times(self):
        tournament = self._create_tournament(name="Explicit Times")
        court = Court.objects.create(tournament=tournament, name="Court A", is_available=True)
        self.client.force_login(self.organizer)

        # Pick a Monday on or after the tournament start date. _build_slots clamps
        # availability to max(tournament.start_date, availability.start_date), so an
        # absolute date here would silently yield zero slots once it fell in the past.
        target_day = self._next_weekday_on_or_after(tournament.start_date, 0)

        response = self.client.post(
            reverse("add_court_availability", kwargs={"pk": tournament.pk}),
            {
                "courts": [str(court.pk)],
                "weekdays": ["0"],
                "start_time": "10:00",
                "additional_start_times": "13:00",
                "start_date": target_day.isoformat(),
                "end_date": target_day.isoformat(),
                "matches_per_court_per_day": "2",
                "is_active": "on",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        availability = CourtAvailability.objects.get(court=court)
        self.assertEqual(availability.additional_start_times, "13:00")
        self.assertEqual(availability.matches_per_court_per_day, 2)
        self.assertEqual(count_available_slots(tournament), 2)

    def test_estimate_court_availability_end_date_uses_matches_per_court_per_day(self):
        tournament = self._create_tournament(name="Estimate Matches Per Day")
        tournament.expected_teams_count = 4
        tournament.save(update_fields=["expected_teams_count"])
        court = Court.objects.create(tournament=tournament, name="Court A", is_available=True)
        self.client.force_login(self.organizer)

        response = self.client.post(
            reverse("estimate_court_availability_end_date", kwargs={"pk": tournament.pk}),
            {
                "courts": [str(court.pk)],
                "weekdays": ["0", "2"],
                "start_time": "09:00",
                "end_time": "14:00",
                "start_date": "2026-05-01",
                "matches_per_court_per_day": "2",
            },
        )

        self.assertEqual(response.status_code, 200)
        json_data = response.json()
        self.assertEqual(json_data["status"], "ok")
        self.assertIn("Estimated end date", json_data["message"])
        self.assertIn("per court per day", json_data["message"])

    def test_add_court_availability_backfills_unscheduled_knockout_rounds(self):
        tournament = self._create_tournament(fmt="knockout", name="Knockout Backfill")
        tournament.start_date = date(2026, 5, 5)
        tournament.end_date = date(2026, 5, 8)
        tournament.save(update_fields=["start_date", "end_date"])

        for idx in range(1, 17):
            self._create_team(tournament, f"K Team {idx}", seed=idx)

        courts = [
            Court.objects.create(tournament=tournament, name=f"Court {idx}", is_available=True)
            for idx in range(1, 4)
        ]
        for court in courts:
            for weekday in [1, 2, 3, 4]:  # Tue-Fri only; not enough slots for all rounds.
                CourtAvailability.objects.create(
                    court=court,
                    weekday=weekday,
                    start_time="12:30",
                    end_time="13:00",
                    start_date=date(2026, 5, 5),
                    end_date=date(2026, 5, 8),
                    matches_per_court_per_day=1,
                    is_active=True,
                )

        generate_fixtures(tournament)
        self.assertGreater(tournament.matches.filter(scheduled_time__isnull=True).count(), 0)

        self.client.force_login(self.organizer)
        response = self.client.post(
            reverse("add_court_availability", kwargs={"pk": tournament.pk}),
            {
                "courts": [str(c.pk) for c in courts],
                "weekdays": ["0"],
                "start_time": "12:30",
                "end_time": "13:00",
                "start_date": "2026-05-11",
                "end_date": "2026-05-11",
                "matches_per_court_per_day": "1",
                "is_active": "on",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(tournament.matches.filter(scheduled_time__isnull=True).count(), 0)

    def test_estimate_tournament_end_date_knockout_includes_semifinal_and_final_rest_days(self):
        tournament = self._create_tournament(fmt="knockout", name="Knockout Rest Estimate")
        tournament.start_date = date(2026, 5, 4)
        tournament.save(update_fields=["start_date"])

        for idx in range(1, 9):
            self._create_team(tournament, f"Rest Team {idx}", seed=idx)

        court = Court.objects.create(tournament=tournament, name="Court A", is_available=True)
        for weekday in [0, 1, 2, 3, 4]:
            CourtAvailability.objects.create(
                court=court,
                weekday=weekday,
                start_time="09:00",
                end_time="09:30",
                start_date=date(2026, 5, 4),
                end_date=date(2026, 5, 31),
                matches_per_court_per_day=1,
                is_active=True,
            )

        self.client.force_login(self.organizer)
        response = self.client.get(reverse("estimate_tournament_end_date", kwargs={"pk": tournament.pk}))

        self.assertEqual(response.status_code, 200)
        json_data = response.json()
        self.assertEqual(json_data["estimated_end_date"], "2026-05-14")

    def test_estimate_court_availability_end_date_knockout_includes_semifinal_and_final_rest_days(self):
        tournament = self._create_tournament(fmt="knockout", name="Availability Rest Estimate")
        tournament.expected_teams_count = 8
        tournament.save(update_fields=["expected_teams_count"])
        court = Court.objects.create(tournament=tournament, name="Court A", is_available=True)

        self.client.force_login(self.organizer)
        response = self.client.post(
            reverse("estimate_court_availability_end_date", kwargs={"pk": tournament.pk}),
            {
                "courts": [str(court.pk)],
                "weekdays": ["0", "1", "2", "3", "4"],
                "start_time": "09:00",
                "end_time": "09:30",
                "start_date": "2026-05-04",
                "matches_per_court_per_day": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        json_data = response.json()
        self.assertEqual(json_data["status"], "ok")
        self.assertEqual(json_data["estimated_end_date"], "2026-05-14")

    def test_add_court_availability_rejects_invalid_bulk_time_range(self):
        tournament = self._create_tournament(name="Bad Availability")
        court = Court.objects.create(tournament=tournament, name="Court A", is_available=True)
        self.client.force_login(self.organizer)

        response = self.client.post(
            reverse("add_court_availability", kwargs={"pk": tournament.pk}),
            {
                "courts": [str(court.pk)],
                "weekdays": ["1"],
                "start_time": "15:00",
                "end_time": "14:00",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(CourtAvailability.objects.filter(court__tournament=tournament).count(), 0)
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("End time must be after start time" in m for m in messages))

    def test_add_court_defaults_to_available_and_shows_on_registration(self):
        tournament = self._create_tournament(name="Availability Default")
        tournament.status = "registration_open"
        tournament.save(update_fields=["status"])
        self.client.force_login(self.organizer)

        response = self.client.post(
            reverse("add_court", kwargs={"pk": tournament.pk}),
            {"name": "Center Court"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        court = Court.objects.get(tournament=tournament, name="Center Court")
        self.assertTrue(court.is_available)
        self.client.logout()

        register_response = self.client.get(
            reverse("tournament_register", kwargs={"pk": tournament.pk})
        )
        self.assertEqual(register_response.status_code, 302)

        team_user = User.objects.create_user(username="court_view_user", password="pass123")
        self.client.force_login(team_user)
        create_team_response = self.client.get(reverse("create_team", kwargs={"pk": tournament.pk}))
        self.assertEqual(create_team_response.status_code, 200)
        self.assertContains(create_team_response, "Center Court")

    def test_active_availability_marks_court_available(self):
        tournament = self._create_tournament(name="Availability Reactivate")
        court = Court.objects.create(tournament=tournament, name="Court A", is_available=False)
        self.client.force_login(self.organizer)

        response = self.client.post(
            reverse("add_court_availability", kwargs={"pk": tournament.pk}),
            {
                "courts": [str(court.pk)],
                "weekdays": ["1"],
                "start_time": "09:00",
                "end_time": "11:00",
                "is_active": "on",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        court.refresh_from_db()
        self.assertTrue(court.is_available)

    def test_generate_fixtures_uses_court_availability_slots(self):
        tournament = self._create_tournament(name="Court Bound")
        tournament.start_date = timezone.localdate() + timedelta(days=1)
        tournament.save(update_fields=["start_date"])
        court = Court.objects.create(tournament=tournament, name="Court 1", is_available=True)
        CourtAvailability.objects.create(
            court=court,
            weekday=tournament.start_date.weekday(),
            start_time="12:00",
            end_time="13:00",
            start_date=tournament.start_date,
            end_date=tournament.start_date,
        )
        team1 = self._create_team(tournament, "Falcons")
        team2 = self._create_team(tournament, "Wolves")
        for team in (team1, team2):
            p = TeamTournamentParticipation.objects.get(team=team, tournament=tournament)
            TeamTournamentCourtPreference.objects.get_or_create(participation=p, court=court)
        Player.objects.create(team=team1, name="Falcons Player")
        Player.objects.create(team=team2, name="Wolves Player")

        generate_fixtures(tournament)
        match = tournament.matches.first()

        self.assertIsNotNone(match)
        self.assertEqual(match.court, court)
        self.assertIsNotNone(match.scheduled_time)
        self.assertEqual(match.scheduled_time.hour, 12)
        self.assertEqual(match.scheduled_end_time.hour, 12)
        self.assertEqual(match.scheduled_end_time.minute, 30)

    def test_generate_fixtures_prevents_same_team_multiple_matches_on_same_day(self):
        tournament = self._create_tournament(name="No Same Day Double Booking")
        start_date = timezone.localdate() + timedelta(days=1)
        tournament.start_date = start_date
        tournament.save(update_fields=["start_date"])
        court1 = Court.objects.create(tournament=tournament, name="FOF1", is_available=True)
        court2 = Court.objects.create(tournament=tournament, name="MOF2", is_available=True)

        for court in (court1, court2):
            for day_offset in range(6):
                day = start_date + timedelta(days=day_offset)
                CourtAvailability.objects.create(
                    court=court,
                    weekday=day.weekday(),
                    start_time="12:00",
                    end_time="13:00",
                    start_date=day,
                    end_date=day,
                )

        teams = [self._create_team(tournament, f"Team{i}", seed=i) for i in range(1, 5)]
        for team in teams:
            Player.objects.create(team=team, name=f"{team.name} Player")
            p = TeamTournamentParticipation.objects.get(team=team, tournament=tournament)
            TeamTournamentCourtPreference.objects.get_or_create(participation=p, court=court1)
            TeamTournamentCourtPreference.objects.get_or_create(participation=p, court=court2)

        generate_fixtures(tournament)

        for team in teams:
            seen_days = set()
            team_matches = tournament.matches.filter(models.Q(team1=team) | models.Q(team2=team))
            for match in team_matches:
                self.assertIsNotNone(match.scheduled_time)
                match_day = timezone.localtime(match.scheduled_time).date()
                self.assertNotIn(match_day, seen_days, f"{team.name} was scheduled twice on {match_day}")
                seen_days.add(match_day)
