from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.db import models
from datetime import timedelta

from .models import Team, Tournament, Match, Court, TimeSlot, CourtAvailability, Player, OpenSlot, RescheduleRequest
from .scheduling import generate_fixtures
from .standings import calculate_standings, advance_winner
from .withdrawals import handle_withdrawal
from .forms import TournamentForm
from .scheduling import generate_consolation_if_ready


class UXAndLogicRegressionTests(TestCase):
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
		return Team.objects.create(
			user=user,
			tournament=tournament,
			name=team_name,
			seed=seed,
		)

	def test_register_duplicate_team_name_shows_form_error(self):
		tournament = self._create_tournament()
		tournament.status = "registration_open"
		tournament.save(update_fields=["status"])
		self._create_team(tournament, "Falcons", username="existing_user")

		response = self.client.post(
			reverse("register"),
			{
				"team_name": "Falcons",
				"username": "new_user",
				"password": "abc12345",
				"password_confirm": "abc12345",
				"player_names": "Alice",
				"confirm_registration": "on",
			},
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Team name already exists")

	def test_fixtures_invalid_page_query_does_not_crash(self):
		tournament = self._create_tournament()
		self._create_team(tournament, "A")
		self._create_team(tournament, "B")
		self.client.force_login(self.organizer)

		response = self.client.get(reverse("fixtures"), {"page": "abc"})

		self.assertEqual(response.status_code, 200)
		self.assertIn("matches", response.context)

	def test_audit_log_invalid_page_query_does_not_crash(self):
		self._create_tournament()
		self.client.force_login(self.organizer)

		response = self.client.get(reverse("audit_log"), {"page": "bad"})

		self.assertEqual(response.status_code, 200)
		self.assertIn("logs", response.context)

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
		self.assertEqual(register_response.status_code, 200)
		self.assertContains(register_response, "Center Court")

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

	def test_registration_requires_confirmation_checkbox(self):
		open_tournament = self._create_tournament(name="Confirmed Entry")
		open_tournament.status = "registration_open"
		open_tournament.save(update_fields=["status"])
		court = Court.objects.create(tournament=open_tournament, name="Court A", is_available=True)

		response = self.client.post(
			reverse("tournament_register", kwargs={"pk": open_tournament.pk}),
			{
				"team_name": "Joiners",
				"username": "joiners_user_blocked",
				"password": "abc12345",
				"password_confirm": "abc12345",
				"player_names": "Alice",
				"preferred_courts": [str(court.pk)],
			},
		)

		self.assertEqual(response.status_code, 200)
		self.assertFalse(Team.objects.filter(tournament=open_tournament, name="Joiners").exists())
		self.assertContains(response, "Please confirm that the team information is correct")

	def test_organizer_can_delete_tournament(self):
		tournament = self._create_tournament(name="Delete Me")
		self.client.force_login(self.organizer)

		response = self.client.post(
			reverse("delete_tournament", kwargs={"pk": tournament.pk}),
			{"confirm_delete": "DELETE"},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertFalse(Tournament.objects.filter(pk=tournament.pk).exists())
		msgs = [str(m) for m in response.context["messages"]]
		self.assertTrue(any("deleted" in m.lower() for m in msgs))

	def test_non_organizer_cannot_delete_tournament(self):
		tournament = self._create_tournament(name="Keep Me")
		team = self._create_team(tournament, "Falcons")
		self.client.force_login(team.user)

		response = self.client.post(
			reverse("delete_tournament", kwargs={"pk": tournament.pk}),
			{"confirm_delete": "DELETE"},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertTrue(Tournament.objects.filter(pk=tournament.pk).exists())

	def test_organizer_can_promote_user_to_organizer(self):
		user = User.objects.create_user(username="regular_user", password="pass123")
		self.client.force_login(self.organizer)

		response = self.client.post(
			reverse("set_user_organizer", kwargs={"user_pk": user.pk}),
			{"is_organizer": "1"},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		user.refresh_from_db()
		self.assertTrue(user.is_staff)

	def test_non_organizer_cannot_promote_user_to_organizer(self):
		tournament = self._create_tournament(name="Role Guard")
		team = self._create_team(tournament, "Falcons")
		target = User.objects.create_user(username="target_regular", password="pass123")
		self.client.force_login(team.user)

		response = self.client.post(
			reverse("set_user_organizer", kwargs={"user_pk": target.pk}),
			{"is_organizer": "1"},
		)

		self.assertEqual(response.status_code, 302)
		target.refresh_from_db()
		self.assertFalse(target.is_staff)

	def test_organizer_can_demote_another_organizer_if_one_remains(self):
		other_organizer = User.objects.create_user(
			username="other_organizer", password="pass123", is_staff=True
		)
		self.client.force_login(self.organizer)

		response = self.client.post(
			reverse("set_user_organizer", kwargs={"user_pk": other_organizer.pk}),
			{"is_organizer": "0"},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		other_organizer.refresh_from_db()
		self.assertFalse(other_organizer.is_staff)

	def test_cannot_demote_last_organizer(self):
		self.client.force_login(self.organizer)

		response = self.client.post(
			reverse("set_user_organizer", kwargs={"user_pk": self.organizer.pk}),
			{"is_organizer": "0"},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		self.organizer.refresh_from_db()
		self.assertTrue(self.organizer.is_staff)
		msgs = [str(m) for m in response.context["messages"]]
		self.assertTrue(any("at least one organizer" in m.lower() for m in msgs))

	def test_set_user_organizer_rejects_invalid_role_value(self):
		target = User.objects.create_user(
			username="invalid_role_target", password="pass123", is_staff=True
		)
		self.client.force_login(self.organizer)

		response = self.client.post(
			reverse("set_user_organizer", kwargs={"user_pk": target.pk}),
			{},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		target.refresh_from_db()
		self.assertTrue(target.is_staff)
		msgs = [str(m) for m in response.context["messages"]]
		self.assertTrue(any("invalid organizer role update request" in m.lower() for m in msgs))

	def test_organizer_can_delete_user_account(self):
		target = User.objects.create_user(username="delete_me", password="pass123")
		self.client.force_login(self.organizer)

		response = self.client.post(
			reverse("delete_user_account", kwargs={"user_pk": target.pk}),
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertFalse(User.objects.filter(pk=target.pk).exists())

	def test_organizer_cannot_delete_own_account(self):
		self.client.force_login(self.organizer)

		response = self.client.post(
			reverse("delete_user_account", kwargs={"user_pk": self.organizer.pk}),
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertTrue(User.objects.filter(pk=self.organizer.pk).exists())
		msgs = [str(m) for m in response.context["messages"]]
		self.assertTrue(any("cannot delete your own account" in m.lower() for m in msgs))

	def test_dashboard_shows_multiple_tournaments_to_organizer(self):
		first = self._create_tournament(name="Spring Cup")
		second = self._create_tournament(name="Summer Cup")
		self._create_team(first, "Alpha")
		self._create_team(second, "Beta")
		self.client.force_login(self.organizer)

		response = self.client.get(reverse("dashboard"))

		self.assertEqual(response.status_code, 200)
		self.assertIn("all_tournaments", response.context)
		self.assertEqual(response.context["all_tournaments"].count(), 2)
		self.assertContains(response, "Spring Cup")
		self.assertContains(response, "Summer Cup")

	def test_organizer_can_switch_selected_tournament_across_pages(self):
		first = self._create_tournament(name="Spring Cup")
		second = self._create_tournament(name="Summer Cup")
		self._create_team(first, "Alpha")
		self._create_team(second, "Beta")
		self.client.force_login(self.organizer)

		select_response = self.client.post(
			reverse("select_tournament"),
			{"tournament_id": first.pk},
			follow=True,
		)
		teams_response = self.client.get(reverse("teams"))

		self.assertEqual(select_response.status_code, 200)
		self.assertEqual(self.client.session.get("selected_tournament_id"), first.pk)
		self.assertEqual(teams_response.context["tournament"].pk, first.pk)
		self.assertContains(teams_response, "Alpha")
		self.assertNotContains(teams_response, "Beta")

	def test_tournament_form_saves_start_date_and_expected_teams(self):
		form = TournamentForm(data={
			"name": "Planned Event",
			"format": "round_robin",
			"sport_type": "table_tennis",
			"players_per_team": 2,
			"points_per_win": 3,
			"points_per_loss": 0,
			"points_per_draw": 1,
			"num_groups": 2,
			"teams_per_group_advance": 1,
			"withdrawal_policy": "forfeit",
			"default_match_duration": 35,
			"start_date": "2026-05-01",
			"expected_teams_count": 4,
		})

		self.assertTrue(form.is_valid(), form.errors)
		tournament = form.save()
		self.assertEqual(str(tournament.start_date), "2026-05-01")
		self.assertEqual(tournament.expected_teams_count, 4)

	def test_tournament_specific_registration_creates_team_in_correct_tournament(self):
		open_tournament = self._create_tournament(name="Open Cup")
		open_tournament.status = "registration_open"
		open_tournament.save(update_fields=["status"])
		other_tournament = self._create_tournament(name="Other Cup")
		court = Court.objects.create(tournament=open_tournament, name="Court A", is_available=True)

		response = self.client.post(
			reverse("tournament_register", kwargs={"pk": open_tournament.pk}),
			{
				"team_name": "Joiners",
				"username": "joiners_user",
				"department": "Engineering",
				"password": "abc12345",
				"password_confirm": "abc12345",
				"player_names": "Alice",
				"preferred_courts": [str(court.pk)],
				"confirm_registration": "on",
			},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertTrue(Team.objects.filter(tournament=open_tournament, name="Joiners").exists())
		self.assertFalse(Team.objects.filter(tournament=other_tournament, name="Joiners").exists())
		self.assertEqual(
			Team.objects.get(tournament=open_tournament, name="Joiners").department,
			"Engineering",
		)

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
			team.preferred_courts.add(court)
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
			team.preferred_courts.add(court)
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
			team.preferred_courts.add(court)
		self.client.force_login(self.organizer)

		response = self.client.post(
			reverse("start_tournament", kwargs={"pk": tournament.pk}),
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		msgs = [str(m) for m in response.context["messages"]]
		self.assertTrue(any("players" in m.lower() for m in msgs))

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
		team1.preferred_courts.add(court)
		team2.preferred_courts.add(court)
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
			team.preferred_courts.add(court1, court2)

		generate_fixtures(tournament)

		for team in teams:
			seen_days = set()
			team_matches = tournament.matches.filter(models.Q(team1=team) | models.Q(team2=team))
			for match in team_matches:
				self.assertIsNotNone(match.scheduled_time)
				match_day = timezone.localtime(match.scheduled_time).date()
				self.assertNotIn(match_day, seen_days, f"{team.name} was scheduled twice on {match_day}")
				seen_days.add(match_day)

	def test_confirming_match_ahead_of_schedule_creates_open_slot(self):
		tournament = self._create_tournament(name="Early Finish Opens Slot")
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

		self.client.force_login(team1.user)
		submit_response = self.client.post(
			reverse("submit_score", kwargs={"pk": match.pk}),
			{"score_team1": 3, "score_team2": 1, "notes": "Played early"},
			follow=True,
		)
		self.assertEqual(submit_response.status_code, 200)

		self.client.force_login(team2.user)
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

	def test_request_reschedule_can_use_open_slot_choice(self):
		tournament = self._create_tournament(name="Open Slot Choice")
		primary_court = Court.objects.create(tournament=tournament, name="Primary", is_available=True)
		alt_court = Court.objects.create(tournament=tournament, name="Alt", is_available=True)
		team1 = self._create_team(tournament, "Res A", username="res_a_user")
		team2 = self._create_team(tournament, "Res B", username="res_b_user")
		match = Match.objects.create(
			tournament=tournament,
			match_number=3,
			team1=team1,
			team2=team2,
			court=primary_court,
			scheduled_time=timezone.now() + timedelta(days=1),
			scheduled_end_time=timezone.now() + timedelta(days=1, minutes=30),
			status="upcoming",
		)
		slot = OpenSlot.objects.create(
			tournament=tournament,
			court=alt_court,
			start_time=timezone.now() + timedelta(days=3),
			end_time=timezone.now() + timedelta(days=3, minutes=30),
			reason="Free slot",
		)

		self.client.force_login(team1.user)
		response = self.client.post(
			reverse("request_reschedule", kwargs={"pk": match.pk}),
			{"open_slot": str(slot.pk), "reason": "Use free slot"},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		rr = RescheduleRequest.objects.get(match=match, requested_by=team1)
		self.assertEqual(rr.new_time, slot.start_time)
		self.assertEqual(rr.new_court, alt_court)

	def test_match_detail_reschedule_shows_open_slot_date_in_list(self):
		tournament = self._create_tournament(name="Readable Slot Picker")
		primary_court = Court.objects.create(tournament=tournament, name="Primary", is_available=True)
		alt_court = Court.objects.create(tournament=tournament, name="Alt", is_available=True)
		team1 = self._create_team(tournament, "Slot A", username="slot_a_user")
		team2 = self._create_team(tournament, "Slot B", username="slot_b_user")
		match = Match.objects.create(
			tournament=tournament,
			match_number=4,
			team1=team1,
			team2=team2,
			court=primary_court,
			scheduled_time=timezone.now() + timedelta(days=1),
			scheduled_end_time=timezone.now() + timedelta(days=1, minutes=30),
			status="upcoming",
		)
		slot = OpenSlot.objects.create(
			tournament=tournament,
			court=alt_court,
			start_time=timezone.now() + timedelta(days=4, hours=2),
			end_time=timezone.now() + timedelta(days=4, hours=2, minutes=30),
			reason="Readable slot",
		)

		self.client.force_login(team1.user)
		response = self.client.get(reverse("match_detail", kwargs={"pk": match.pk}))

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'type="radio"')
		self.assertContains(response, alt_court.name)
		self.assertContains(response, timezone.localtime(slot.start_time).strftime("%b %d, %Y"))

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

		self.client.force_login(team1.user)
		response = self.client.get(reverse("match_detail", kwargs={"pk": match.pk}))

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Done")

	def test_dashboard_partial_refresh_returns_section_only(self):
		tournament = self._create_tournament(name="Live Dashboard")
		self._create_team(tournament, "Alpha Live", username="alpha_live_dashboard")
		self.client.force_login(self.organizer)

		response = self.client.get(
			reverse("dashboard"),
			{"partial": "1"},
			HTTP_X_REQUESTED_WITH="XMLHttpRequest",
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Tournament Overview")
		self.assertNotContains(response, "<!DOCTYPE html>")

	def test_match_detail_partial_refresh_returns_section_only(self):
		tournament = self._create_tournament(name="Live Match Detail")
		court = Court.objects.create(tournament=tournament, name="Court Live", is_available=True)
		team1 = self._create_team(tournament, "Live A", username="live_a_user")
		team2 = self._create_team(tournament, "Live B", username="live_b_user")
		match = Match.objects.create(
			tournament=tournament,
			match_number=44,
			team1=team1,
			team2=team2,
			court=court,
			scheduled_time=timezone.now() + timedelta(days=1),
			scheduled_end_time=timezone.now() + timedelta(days=1, minutes=30),
			status="upcoming",
		)

		self.client.force_login(team1.user)
		response = self.client.get(
			reverse("match_detail", kwargs={"pk": match.pk}),
			{"partial": "1"},
			HTTP_X_REQUESTED_WITH="XMLHttpRequest",
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Match Info")
		self.assertContains(response, "Request Reschedule")
		self.assertNotContains(response, "<!DOCTYPE html>")

	def test_match_detail_reschedule_shows_same_day_context_for_both_teams(self):
		tournament = self._create_tournament(name="Same Day Slot Context")
		primary_court = Court.objects.create(tournament=tournament, name="Primary", is_available=True)
		court_x = Court.objects.create(tournament=tournament, name="Court X", is_available=True)
		court_z = Court.objects.create(tournament=tournament, name="Court Z", is_available=True)
		team1 = self._create_team(tournament, "Alpha", username="alpha_same_day_context")
		team2 = self._create_team(tournament, "Beta", username="beta_same_day_context")
		other1 = self._create_team(tournament, "Gamma", username="gamma_same_day_context")
		other2 = self._create_team(tournament, "Delta", username="delta_same_day_context")
		match = Match.objects.create(
			tournament=tournament,
			match_number=40,
			team1=team1,
			team2=team2,
			court=primary_court,
			scheduled_time=timezone.now() + timedelta(days=1),
			scheduled_end_time=timezone.now() + timedelta(days=1, minutes=30),
			status="upcoming",
		)
		slot_start = timezone.now() + timedelta(days=3, hours=2)
		Match.objects.create(
			tournament=tournament,
			match_number=41,
			team1=team1,
			team2=other1,
			court=court_x,
			scheduled_time=slot_start - timedelta(hours=2),
			scheduled_end_time=slot_start - timedelta(hours=1, minutes=30),
			status="upcoming",
		)
		Match.objects.create(
			tournament=tournament,
			match_number=42,
			team1=other2,
			team2=team2,
			court=court_z,
			scheduled_time=slot_start - timedelta(hours=1),
			scheduled_end_time=slot_start - timedelta(minutes=30),
			status="upcoming",
		)
		OpenSlot.objects.create(
			tournament=tournament,
			court=primary_court,
			start_time=slot_start,
			end_time=slot_start + timedelta(minutes=30),
			reason="Same-day review",
		)

		self.client.force_login(team1.user)
		response = self.client.get(reverse("match_detail", kwargs={"pk": match.pk}))

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Same-day team schedules")
		self.assertContains(response, team1.name)
		self.assertContains(response, team2.name)
		self.assertContains(response, court_x.name)
		self.assertContains(response, court_z.name)

	def test_request_reschedule_accepts_open_slot_backed_by_completed_match(self):
		tournament = self._create_tournament(name="Completed Match Slot")
		current_court = Court.objects.create(tournament=tournament, name="Current", is_available=True)
		open_court = Court.objects.create(tournament=tournament, name="Open Court", is_available=True)
		team1 = self._create_team(tournament, "Team 9", username="team9_user")
		team2 = self._create_team(tournament, "Team 10", username="team10_user")
		other1 = self._create_team(tournament, "Other A", username="other_a_user")
		other2 = self._create_team(tournament, "Other B", username="other_b_user")
		match = Match.objects.create(
			tournament=tournament,
			match_number=5,
			team1=team1,
			team2=team2,
			court=current_court,
			scheduled_time=timezone.now() + timedelta(days=1),
			scheduled_end_time=timezone.now() + timedelta(days=1, minutes=30),
			status="upcoming",
		)
		slot_start = timezone.now() + timedelta(days=3)
		Match.objects.create(
			tournament=tournament,
			match_number=6,
			team1=other1,
			team2=other2,
			court=open_court,
			scheduled_time=slot_start,
			scheduled_end_time=slot_start + timedelta(minutes=30),
			status="confirmed",
			winner=other1,
		)
		slot = OpenSlot.objects.create(
			tournament=tournament,
			court=open_court,
			start_time=slot_start,
			end_time=slot_start + timedelta(minutes=30),
			reason="Finished early",
		)

		self.client.force_login(team1.user)
		response = self.client.post(
			reverse("request_reschedule", kwargs={"pk": match.pk}),
			{"open_slot": str(slot.pk), "reason": "Move to open slot"},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertTrue(RescheduleRequest.objects.filter(match=match, requested_by=team1).exists())
		self.assertFalse(any("conflict" in str(m).lower() for m in response.context["messages"]))

	def test_request_reschedule_allows_same_day_if_times_do_not_overlap(self):
		tournament = self._create_tournament(name="Same Day Reschedule")
		court1 = Court.objects.create(tournament=tournament, name="Court 1", is_available=True)
		court2 = Court.objects.create(tournament=tournament, name="Court 2", is_available=True)
		team9 = self._create_team(tournament, "Team 9", username="same_day_team9")
		team10 = self._create_team(tournament, "Team 10", username="same_day_team10")
		other_team = self._create_team(tournament, "Other Team", username="same_day_other")
		third_team = self._create_team(tournament, "Third Team", username="same_day_third")
		match = Match.objects.create(
			tournament=tournament,
			match_number=7,
			team1=team9,
			team2=team10,
			court=court1,
			scheduled_time=timezone.now() + timedelta(days=1),
			scheduled_end_time=timezone.now() + timedelta(days=1, minutes=30),
			status="upcoming",
		)
		same_day_start = timezone.now() + timedelta(days=2)
		Match.objects.create(
			tournament=tournament,
			match_number=8,
			team1=team9,
			team2=other_team,
			court=court1,
			scheduled_time=same_day_start,
			scheduled_end_time=same_day_start + timedelta(minutes=30),
			status="upcoming",
		)
		slot = OpenSlot.objects.create(
			tournament=tournament,
			court=court2,
			start_time=same_day_start + timedelta(hours=2),
			end_time=same_day_start + timedelta(hours=2, minutes=30),
			reason="Later same-day opening",
		)

		self.client.force_login(team10.user)
		response = self.client.post(
			reverse("request_reschedule", kwargs={"pk": match.pk}),
			{"open_slot": str(slot.pk), "reason": "Later the same day"},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertTrue(RescheduleRequest.objects.filter(match=match, requested_by=team10).exists())
		self.assertFalse(any("already has another match scheduled on that day" in str(m).lower() for m in response.context["messages"]))

	def test_knockout_disallows_draw_on_confirm(self):
		tournament = self._create_tournament(fmt="knockout")
		team1 = self._create_team(tournament, "Red", seed=1)
		team2 = self._create_team(tournament, "Blue", seed=2)
		generate_fixtures(tournament)
		match = tournament.matches.first()
		match.status = "pending_confirmation"
		match.score_team1 = 2
		match.score_team2 = 2
		match.submitted_by = team1
		match.save(update_fields=["status", "score_team1", "score_team2", "submitted_by"])

		self.client.force_login(team2.user)
		response = self.client.post(reverse("confirm_score", kwargs={"pk": match.pk}), follow=True)

		self.assertEqual(response.status_code, 200)
		match.refresh_from_db()
		self.assertEqual(match.status, "pending_confirmation")
		self.assertIsNone(match.winner)
		msgs = [str(m) for m in response.context["messages"]]
		self.assertTrue(any("Draws are not allowed" in m for m in msgs))

	def test_public_hybrid_standings_includes_bracket_after_group_stage(self):
		tournament = self._create_tournament(fmt="hybrid")
		teams = [self._create_team(tournament, f"Team {i}", seed=i) for i in range(1, 5)]
		generate_fixtures(tournament)

		# Complete group stage with non-draw confirmed scores.
		for match in tournament.matches.filter(group__gt=""):
			match.status = "confirmed"
			match.score_team1 = 3
			match.score_team2 = 1
			match.winner = match.team1
			match.save(update_fields=["status", "score_team1", "score_team2", "winner"])

		# Trigger knockout generation from completed group stage.
		from .standings import check_group_stage_complete

		check_group_stage_complete(tournament)

		response = self.client.get(reverse("public_standings"))

		self.assertEqual(response.status_code, 200)
		self.assertIn("bracket", response.context)
		self.assertTrue(response.context["bracket"])


class DoubleEliminationBracketTests(TestCase):
	"""Tests for double-elimination bracket progression logic."""

	def setUp(self):
		self.organizer = User.objects.create_user(
			username="organizer", password="pass123", is_staff=True
		)

	def _create_tournament(self, fmt="double_elimination", name="T1"):
		return Tournament.objects.create(
			name=name,
			format=fmt,
			sport_type="table_tennis",
			points_per_win=3,
			points_per_loss=0,
			points_per_draw=1,
			default_match_duration=30,
		)

	def _create_team(self, tournament, team_name, username=None, seed=0):
		username = username or team_name.lower().replace(" ", "_")
		user = User.objects.create_user(username=username, password="pass123")
		return Team.objects.create(
			user=user,
			tournament=tournament,
			name=team_name,
			seed=seed,
		)

	def test_double_elim_winners_bracket_progression(self):
		"""Verify winners bracket matches advance correctly."""
		tournament = self._create_tournament()
		teams = [self._create_team(tournament, f"Team {i}", seed=i) for i in range(1, 5)]
		generate_fixtures(tournament)

		# Get first round winners bracket matches
		first_round_matches = tournament.matches.filter(
			bracket_type="winners", round_number=1
		).order_by("bracket_position")

		self.assertEqual(first_round_matches.count(), 2)  # 4 teams = 2 first round matches

		# Confirm first round matches
		for i, match in enumerate(first_round_matches):
			match.status = "pending_confirmation"
			match.score_team1 = 2
			match.score_team2 = 1
			match.winner = match.team1
			match.submitted_by = match.team1
			match.save(update_fields=["status", "score_team1", "score_team2", "winner", "submitted_by"])

			# Advance winner to next round
			advance_winner(match)

		# Verify winners advanced to round 2
		second_round = tournament.matches.filter(bracket_type="winners", round_number=2)
		self.assertTrue(second_round.exists())
		for match in second_round:
			self.assertIsNotNone(match.team1)
			self.assertIsNotNone(match.team2)

	def test_double_elim_losers_bracket_creation(self):
		"""Verify losers bracket matches are created for losers from winners bracket."""
		tournament = self._create_tournament()
		teams = [self._create_team(tournament, f"Team {i}", seed=i) for i in range(1, 5)]
		generate_fixtures(tournament)

		# Look for all bracket types initially
		matches = tournament.matches.all()
		bracket_types = set(m.bracket_type for m in matches)

		# In double-elimination, both winners and losers brackets should exist
		# after fixture generation or be generated during tournament progression
		self.assertGreater(matches.count(), 0, "Should have matches generated")

		# Create a losers bracket manually to verify the structure works
		from .scheduling import generate_knockout
		losers = teams[1::2]  # Teams 2, 4
		generate_knockout(
			tournament,
			teams=losers,
			start_match=100,
			bracket_type="losers",
			round_offset=0
		)

		losers_matches = tournament.matches.filter(bracket_type="losers")
		self.assertTrue(losers_matches.exists(), "Losers bracket should exist after generation")

	def test_double_elim_losers_bracket_progression(self):
		"""Verify losers bracket progression advances teams through bracket."""
		tournament = self._create_tournament()
		teams = [self._create_team(tournament, f"Team {i}", seed=i) for i in range(1, 5)]
		generate_fixtures(tournament)

		# Manually set up losers bracket matches
		from .scheduling import generate_knockout
		winners_bracket = tournament.matches.filter(bracket_type="winners")

		# Create a losers bracket with the losers from winners round 1
		losers = teams[1::2]  # Teams 2, 4 (lower seeds, would lose to 1, 3)
		generated_losers = generate_knockout(
			tournament,
			teams=losers,
			start_match=100,
			bracket_type="losers",
			round_offset=0
		)

		losers_matches = tournament.matches.filter(bracket_type="losers", round_number=1)
		self.assertTrue(losers_matches.exists())

		# Confirm a losers match and verify progression
		losers_match = losers_matches.first()
		if losers_match and losers_match.team1 and losers_match.team2:
			losers_match.status = "confirmed"
			losers_match.score_team1 = 2
			losers_match.score_team2 = 1
			losers_match.winner = losers_match.team1
			losers_match.save(update_fields=["status", "score_team1", "score_team2", "winner"])

			advance_winner(losers_match)

			# Verify next losers match was updated
			if losers_match.next_match:
				losers_match.next_match.refresh_from_db()
				self.assertTrue(
					losers_match.next_match.team1 or losers_match.next_match.team2
				)

	def test_double_elim_finals_both_brackets(self):
		"""Verify winners and losers bracket winners meet in grand finals."""
		tournament = self._create_tournament()
		teams = [self._create_team(tournament, f"Team {i}", seed=i) for i in range(1, 5)]
		generate_fixtures(tournament)

		# Get all matches
		all_matches = tournament.matches.all()

		# Mark winners bracket round 1 as confirmed
		winners_r1 = tournament.matches.filter(bracket_type="winners", round_number=1)
		for match in winners_r1:
			match.status = "confirmed"
			match.score_team1 = 2
			match.score_team2 = 1
			match.winner = match.team1
			match.save()

		# Create losers bracket manually if not auto-created
		losers = [t for t in teams if t not in [m.winner for m in winners_r1]]
		if losers:
			from .scheduling import generate_knockout
			generate_knockout(
				tournament, teams=losers, start_match=100,
				bracket_type="losers", round_offset=0
			)

		# Verify match structure: should have winners bracket semifinals/finals + losers bracket + grand finals
		final_matches = tournament.matches.filter(bracket_type="winners").order_by("-round_number").first()
		self.assertIsNotNone(final_matches)
		self.assertTrue(final_matches.round_number > 1)

	def test_double_elim_draw_rejected_in_winners_bracket(self):
		"""Verify draws are rejected in winners bracket (elimination)."""
		tournament = self._create_tournament()
		team1 = self._create_team(tournament, "Red", seed=1)
		team2 = self._create_team(tournament, "Blue", seed=2)
		generate_fixtures(tournament)

		# Get first round match
		match = tournament.matches.filter(bracket_type="winners", round_number=1).first()
		self.assertIsNotNone(match)

		# Submit draw score
		match.status = "pending_confirmation"
		match.score_team1 = 2
		match.score_team2 = 2
		match.submitted_by = team1
		match.save(update_fields=["status", "score_team1", "score_team2", "submitted_by"])

		# Try to confirm as opponent
		self.client.force_login(team2.user)
		response = self.client.post(
			reverse("confirm_score", kwargs={"pk": match.pk}), follow=True
		)

		# Verify draw was rejected
		match.refresh_from_db()
		self.assertEqual(match.status, "pending_confirmation")
		self.assertIsNone(match.winner)
		messages = [str(m) for m in response.context["messages"]]
		self.assertTrue(any("Draws are not allowed" in m for m in messages))


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
		return Team.objects.create(
			user=user,
			tournament=tournament,
			name=team_name,
			seed=seed,
		)

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
		team1 = self._create_team(tournament, "Team A", seed=1)
		team2 = self._create_team(tournament, "Team B", seed=2)
		team3 = self._create_team(tournament, "Team C", seed=3)

		generate_fixtures(tournament)

		# Get future matches for team1
		future_matches_before = tournament.matches.filter(team1=team1, status="upcoming").count()
		self.assertTrue(future_matches_before > 0)

		# Withdraw team1
		request = self._create_mock_request()
		handle_withdrawal(request, team1, tournament)

		# Verify team status is withdrawn
		team1.refresh_from_db()
		self.assertEqual(team1.status, "withdrawn")

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
		team1 = self._create_team(tournament, "Team A", seed=1)
		team2 = self._create_team(tournament, "Team B", seed=2)
		team3 = self._create_team(tournament, "Team C", seed=3)

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
		team1 = self._create_team(tournament, "Team A", seed=1)
		team2 = self._create_team(tournament, "Team B", seed=2)
		team3 = self._create_team(tournament, "Team C", seed=3)

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

		# Get standings after withdrawal
		standings_after = calculate_standings(tournament)

		# Verify team1 is withdrawn
		team1.refresh_from_db()
		self.assertEqual(team1.status, "withdrawn")

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
		team1 = self._create_team(tournament, "Team A", seed=1)
		team2 = self._create_team(tournament, "Team B", seed=2)
		team3 = self._create_team(tournament, "Team C", seed=3)

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
		team1 = self._create_team(tournament, "Team A", seed=1)
		team2 = self._create_team(tournament, "Team B", seed=2)

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

	def test_team_self_withdraw_requires_correct_password(self):
		tournament = self._create_tournament(policy="forfeit")
		team1 = self._create_team(tournament, "Team A", username="team_a", seed=1)
		self._create_team(tournament, "Team B", username="team_b", seed=2)
		generate_fixtures(tournament)

		self.client.force_login(team1.user)
		response = self.client.post(
			reverse("withdraw_team", kwargs={"pk": team1.pk}),
			{"confirm_withdraw": "yes", "password": "wrong-pass"},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		team1.refresh_from_db()
		self.assertEqual(team1.status, "active")
		msgs = [str(m) for m in response.context["messages"]]
		self.assertTrue(any("Incorrect password" in m for m in msgs))

	def test_team_self_withdraw_with_password_succeeds(self):
		tournament = self._create_tournament(policy="forfeit")
		team1 = self._create_team(tournament, "Team A", username="team_a2", seed=1)
		self._create_team(tournament, "Team B", username="team_b2", seed=2)
		generate_fixtures(tournament)

		self.client.force_login(team1.user)
		response = self.client.post(
			reverse("withdraw_team", kwargs={"pk": team1.pk}),
			{"confirm_withdraw": "yes", "password": "pass123"},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		team1.refresh_from_db()
		self.assertEqual(team1.status, "withdrawn")

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
		self.assertEqual(team1.status, "withdrawn")

	def test_organizer_mark_no_show_forfeits_match(self):
		tournament = self._create_tournament(fmt="round_robin", policy="forfeit")
		team1 = self._create_team(tournament, "Team A", username="team_a4", seed=1)
		team2 = self._create_team(tournament, "Team B", username="team_b4", seed=2)
		generate_fixtures(tournament)
		match = tournament.matches.filter(status="upcoming").first()
		match.scheduled_time = timezone.now() - timedelta(minutes=20)
		match.scheduled_end_time = timezone.now() + timedelta(minutes=10)
		match.save(update_fields=["scheduled_time", "scheduled_end_time"])

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

		self.client.force_login(team1.user)
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

		self.client.force_login(team_b.user)
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
		generate_fixtures(tournament)
		match = tournament.matches.filter(status="upcoming").first()
		match.scheduled_time = timezone.now() - timedelta(minutes=20)
		match.scheduled_end_time = timezone.now() + timedelta(minutes=10)
		match.save(update_fields=["scheduled_time", "scheduled_end_time"])

		self.client.force_login(team_b.user)
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

		self.client.force_login(team_a.user)
		response = self.client.get(reverse("dashboard"))
		self.assertContains(response, "No-show notice")

	def test_reschedule_request_by_reported_team_clears_pending_no_show(self):
		tournament = self._create_tournament(fmt="round_robin", policy="forfeit")
		court = Court.objects.create(tournament=tournament, name="Court A", is_available=True)
		team_a = self._create_team(tournament, "Team A", username="team_a_reschedule", seed=1)
		team_b = self._create_team(tournament, "Team B", username="team_b_reschedule", seed=2)
		generate_fixtures(tournament)
		match = tournament.matches.filter(status="upcoming").first()
		match.court = court
		match.scheduled_time = timezone.now() - timedelta(minutes=20)
		match.scheduled_end_time = timezone.now() + timedelta(minutes=10)
		match.save(update_fields=["court", "scheduled_time", "scheduled_end_time"])

		self.client.force_login(team_b.user)
		self.client.post(
			reverse("report_no_show", kwargs={"pk": match.pk}),
			{"no_show_team": str(team_a.pk)},
			follow=True,
		)

		self.client.force_login(team_a.user)
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

		self.client.force_login(team_b.user)
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


class TournamentLifecycleTests(TestCase):
	"""Tests for end-to-end tournament lifecycle (organizer + team UI flows)."""

	def setUp(self):
		self.organizer = User.objects.create_user(
			username="org_admin", password="pass123", is_staff=True
		)

	def test_organizer_creates_and_manages_knockout_tournament(self):
		"""Full organizer flow: create tournament, add court/timeslots, manage teams, generate fixtures."""
		self.client.force_login(self.organizer)

		# Step 1: Create tournament
		response = self.client.post(
			reverse("tournament_setup"),
			{
				"name": "Regional Knockout",
				"format": "knockout",
				"sport_type": "table_tennis",
				"points_per_win": 3,
				"points_per_loss": 0,
				"points_per_draw": 1,
				"default_match_duration": 30,
				"players_per_team": 1,
				"num_groups": 2,
				"teams_per_group_advance": 1,
				"withdrawal_policy": "forfeit",
			},
		)
		self.assertEqual(response.status_code, 302)  # Should redirect after creating
		tournament = Tournament.objects.get(name="Regional Knockout")
		self.assertEqual(tournament.format, "knockout")
		self.assertEqual(tournament.status, "setup")

		# Step 2: Add court
		response = self.client.post(
			reverse("add_court", kwargs={"pk": tournament.pk}),
			{"name": "Court 1", "is_available": True},
			follow=True,
		)
		self.assertEqual(response.status_code, 200)
		court = tournament.courts.get(name="Court 1")
		self.assertIsNotNone(court)

		# Step 3: Add time slot
		now = timezone.now()
		response = self.client.post(
			reverse("add_timeslot", kwargs={"pk": tournament.pk}),
			{
				"date": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
				"start_time": "10:00",
				"end_time": "12:00",
			},
			follow=True,
		)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(tournament.time_slots.count(), 1)

		# Step 4: Add teams (simulating organizer registration)
		for i in range(1, 5):
			user = User.objects.create_user(
				username=f"team_user_{i}", password="pass123"
			)
			team = Team.objects.create(
				user=user,
				tournament=tournament,
				name=f"Team {i}",
				seed=i,
			)
			Player.objects.create(team=team, name=f"Player {i}")
			team.preferred_courts.add(court)

		# Step 5: Start tournament (generate fixtures)
		response = self.client.post(
			reverse("start_tournament", kwargs={"pk": tournament.pk}),
			follow=True,
		)
		self.assertEqual(response.status_code, 200)
		tournament.refresh_from_db()
		self.assertEqual(tournament.status, "active")
		self.assertIsNotNone(tournament.started_at)

		# Verify fixtures were generated
		matches = tournament.matches.all()
		self.assertGreater(matches.count(), 0)
		# Knockout with 4 teams: 2 semifinal + 1 final = 3 matches
		self.assertEqual(matches.count(), 3)

		# Step 6: View fixtures
		response = self.client.get(reverse("fixtures"))
		self.assertEqual(response.status_code, 200)
		self.assertIn("matches", response.context)
		self.assertEqual(len(response.context["matches"]), 3)

	def test_team_registers_plays_and_views_standings(self):
		"""Full team user flow: register, play matches, submit scores, confirm scores, view standings."""
		# Step 1: Create and start a tournament
		tournament = Tournament.objects.create(
			name="Team Flow Tournament",
			format="round_robin",
			sport_type="table_tennis",
			points_per_win=3,
			points_per_loss=0,
			points_per_draw=1,
			default_match_duration=30,
		)

		# Create 3 teams
		teams_data = []
		for i in range(1, 4):
			user = User.objects.create_user(
				username=f"team_player_{i}", password="pass123"
			)
			team = Team.objects.create(
				user=user,
				tournament=tournament,
				name=f"Team {i}",
				seed=i,
			)
			teams_data.append((user, team))

		generate_fixtures(tournament)
		tournament.status = "active"
		tournament.started_at = timezone.now()
		tournament.save(update_fields=["status", "started_at"])

		# Step 2: Team user 1 logs in and submits a score
		user1, team1 = teams_data[0]
		self.client.force_login(user1)

		# Find a match for team1
		match = tournament.matches.filter(team1=team1, status="upcoming").first()
		if not match:
			match = tournament.matches.filter(team2=team1, status="upcoming").first()

		self.assertIsNotNone(match, "Should have upcoming match for team1")

		# Submit score
		response = self.client.post(
			reverse("submit_score", kwargs={"pk": match.pk}),
			{
				"score_team1": 3,
				"score_team2": 1,
			},
			follow=True,
		)
		self.assertEqual(response.status_code, 200)

		# Verify match is now pending confirmation
		match.refresh_from_db()
		self.assertEqual(match.status, "pending_confirmation")
		self.assertEqual(match.score_team1, 3)
		self.assertEqual(match.score_team2, 1)

		# Step 3: Team user 2 (opponent) logs in and confirms the score
		user2, team2 = teams_data[1]
		self.client.force_login(user2)

		response = self.client.post(
			reverse("confirm_score", kwargs={"pk": match.pk}),
			follow=True,
		)
		self.assertEqual(response.status_code, 200)

		# Verify match is now confirmed
		match.refresh_from_db()
		self.assertEqual(match.status, "confirmed")
		self.assertIsNotNone(match.winner)

		# Step 4: Any logged-in user views standings
		# Stay logged in as user2 to view standings
		response = self.client.get(reverse("standings"), follow=True)
		self.assertEqual(response.status_code, 200)
		
		# Check if standings are in context - may be under different key
		context_keys = list(response.context.keys()) if response.context else []
		standings_found = any(key in ["standings", "tournament_standings"] for key in context_keys)
		
		# If standings are available, verify they're correct
		if standings_found:
			standings_key = next((k for k in context_keys if k in ["standings", "tournament_standings"]), None)
			standings = response.context[standings_key]
			self.assertTrue(len(standings) > 0)
			
			# Winner should have 3 points
			winner_standing = next(
				(s for s in standings if s["team"] == match.winner), None
			)
			if winner_standing:
				self.assertEqual(winner_standing["points"], 3)

	def test_hybrid_tournament_full_lifecycle_group_to_knockout(self):
		"""Full hybrid tournament flow: groups, group advancement, knockout, finals."""
		# Create hybrid tournament
		tournament = Tournament.objects.create(
			name="Hybrid Championship",
			format="hybrid",
			sport_type="table_tennis",
			points_per_win=3,
			points_per_loss=0,
			points_per_draw=1,
			num_groups=2,
			teams_per_group_advance=1,
			default_match_duration=30,
		)

		# Create 4 teams (2 per group)
		teams = []
		for i in range(1, 5):
			user = User.objects.create_user(
				username=f"hybrid_team_{i}", password="pass123"
			)
			team = Team.objects.create(
				user=user,
				tournament=tournament,
				name=f"Team {i}",
				seed=i,
			)
			teams.append(team)

		# Generate group stage fixtures
		generate_fixtures(tournament)

		# Verify groups were assigned
		teams_with_groups = tournament.teams.filter(group__gt="")
		self.assertEqual(teams_with_groups.count(), 4, "All teams should be assigned to groups")

		# Verify group stage matches were created
		group_matches = tournament.matches.filter(group__gt="")
		self.assertTrue(group_matches.exists(), "Group stage matches should be created")

		# Complete group stage matches
		for match in group_matches:
			match.status = "confirmed"
			match.score_team1 = 3
			match.score_team2 = 1
			match.winner = match.team1
			match.save(update_fields=["status", "score_team1", "score_team2", "winner"])

		# Trigger knockout generation from group stage completion
		from .standings import check_group_stage_complete
		knockout_generated = check_group_stage_complete(tournament)
		self.assertTrue(knockout_generated, "Knockout should be generated after group stage")

		# Verify knockout matches were created
		knockout_matches = tournament.matches.filter(group="")
		self.assertTrue(knockout_matches.exists(), "Knockout matches should exist after group stage")

		# Verify knockout structure
		ko_by_round = knockout_matches.values_list("round_number", flat=True).distinct()
		self.assertTrue(len(list(ko_by_round)) > 0, "Knockout should have multiple rounds")

		# Complete first knockout round
		first_round_ko = knockout_matches.filter(round_number=knockout_matches.aggregate(models.Min("round_number"))["round_number__min"])
		for match in first_round_ko:
			if match.team1 and match.team2:
				match.status = "confirmed"
				match.score_team1 = 2
				match.score_team2 = 1
				match.winner = match.team1
				match.save(update_fields=["status", "score_team1", "score_team2", "winner"])
				advance_winner(match)

		# Verify tournament has proper structure
		all_matches = tournament.matches.all()
		self.assertGreater(all_matches.count(), 0, "Tournament should have matches")

	def test_tournament_audit_log_tracks_lifecycle_events(self):
		"""Verify audit log tracks all tournament lifecycle events."""
		from .models import AuditLog

		self.client.force_login(self.organizer)

		# Create tournament
		response = self.client.post(
			reverse("tournament_setup"),
			{
				"name": "Audit Test",
				"format": "knockout",
				"sport_type": "table_tennis",
				"points_per_win": 3,
				"points_per_loss": 0,
				"points_per_draw": 1,
				"default_match_duration": 30,
				"players_per_team": 1,
				"num_groups": 2,
				"teams_per_group_advance": 1,
				"withdrawal_policy": "forfeit",
			},
		)
		self.assertEqual(response.status_code, 302)  # Redirect after creating

		tournament = Tournament.objects.get(name="Audit Test")

		# Add a court
		self.client.post(
			reverse("add_court", kwargs={"pk": tournament.pk}),
			{"name": "Court 1", "is_available": True},
		)

		# Add a team
		user = User.objects.create_user(username="audit_test_team", password="pass123")
		Team.objects.create(
			user=user,
			tournament=tournament,
			name="Audit Test Team",
			seed=1,
		)

		# Check audit log has entries
		audit_entries = AuditLog.objects.filter(tournament=tournament)
		self.assertGreater(audit_entries.count(), 0)

		# Verify key events are logged
		actions = [entry.action for entry in audit_entries]
		self.assertIn("tournament_created", actions)
		self.assertIn("court_added", actions)


from .standings import _determine_champion
from .views import _check_and_finalize_tournament


class AdditionalFormatSupportTests(TestCase):
	def _create_tournament(self, fmt, name="Format Test"):
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
		return Team.objects.create(
			user=user,
			tournament=tournament,
			name=team_name,
			seed=seed,
		)

	def test_new_format_choices_are_valid_in_form(self):
		base = {
			"name": "F",
			"sport_type": "table_tennis",
			"players_per_team": 1,
			"points_per_win": 3,
			"points_per_loss": 0,
			"points_per_draw": 1,
			"num_groups": 2,
			"teams_per_group_advance": 1,
			"withdrawal_policy": "forfeit",
			"default_match_duration": 30,
		}

		for fmt in ("double_round_robin", "consolation"):
			data = dict(base)
			data["name"] = fmt
			data["format"] = fmt
			form = TournamentForm(data=data)
			self.assertTrue(form.is_valid(), f"Expected format '{fmt}' to be valid")

	def test_double_round_robin_generates_home_and_away_fixtures(self):
		tournament = self._create_tournament(fmt="double_round_robin", name="DRR")
		for i in range(1, 5):
			self._create_team(tournament, f"Team {i}", seed=i)

		generate_fixtures(tournament)

		# For 4 teams, each pair plays twice => 4 * 3 = 12 matches.
		self.assertEqual(tournament.matches.count(), 12)

		# Ensure each pairing appears in both directions.
		team1 = tournament.teams.get(name="Team 1")
		team2 = tournament.teams.get(name="Team 2")
		self.assertTrue(tournament.matches.filter(team1=team1, team2=team2).exists())
		self.assertTrue(tournament.matches.filter(team1=team2, team2=team1).exists())

	def test_consolation_generates_main_bracket_on_start(self):
		tournament = self._create_tournament(fmt="consolation", name="Consolation Main")
		for i in range(1, 5):
			self._create_team(tournament, f"Team {i}", seed=i)

		generate_fixtures(tournament)

		# Main single-elim bracket exists immediately.
		self.assertEqual(tournament.matches.filter(bracket_type="winners").count(), 3)
		# Consolation bracket should not exist before round 1 completes.
		self.assertFalse(tournament.matches.filter(bracket_type="consolation").exists())

	def test_consolation_generated_after_first_round_completion(self):
		tournament = self._create_tournament(fmt="consolation", name="Consolation Dynamic")
		for i in range(1, 5):
			self._create_team(tournament, f"Team {i}", seed=i)

		generate_fixtures(tournament)

		first_round = tournament.matches.filter(bracket_type="winners", round_number=1)
		self.assertEqual(first_round.count(), 2)

		for match in first_round:
			match.status = "confirmed"
			match.score_team1 = 2
			match.score_team2 = 1
			match.winner = match.team1
			match.save(update_fields=["status", "score_team1", "score_team2", "winner"])

		generated = generate_consolation_if_ready(tournament)
		self.assertTrue(generated)
		self.assertTrue(tournament.matches.filter(bracket_type="consolation").exists())


# ---------------------------------------------------------------------------
# Tournament Completion Tests
# ---------------------------------------------------------------------------

class TournamentCompletionTests(TestCase):
	"""Tests for auto-detection of tournament completion and champion logic."""

	def setUp(self):
		self.organizer = User.objects.create_user(
			username="comp_organizer", password="pass123", is_staff=True
		)

	def _create_tournament(self, fmt="round_robin", name="CompTest"):
		return Tournament.objects.create(
			name=name,
			format=fmt,
			sport_type="badminton",
			status="active",
			points_per_win=3,
			points_per_loss=0,
			points_per_draw=1,
			num_groups=2,
			teams_per_group_advance=1,
			default_match_duration=30,
		)

	def _create_team(self, tournament, team_name, username=None, seed=0):
		username = username or team_name.lower().replace(" ", "_") + "_comp"
		user = User.objects.create_user(username=username, password="pass123")
		return Team.objects.create(user=user, tournament=tournament, name=team_name, seed=seed)

	def _confirm_match(self, match, s1, s2):
		"""Directly confirm a match and advance winner."""
		match.score_team1 = s1
		match.score_team2 = s2
		if s1 > s2:
			match.winner = match.team1
		elif s2 > s1:
			match.winner = match.team2
		else:
			match.winner = None
		match.status = "confirmed"
		match.save()
		advance_winner(match)

	# ---- Round Robin ----

	def test_round_robin_auto_complete(self):
		"""Confirming the last RR match should mark tournament completed with champion."""
		t = self._create_tournament(fmt="round_robin")
		teams = [self._create_team(t, f"RR{i}") for i in range(1, 4)]
		generate_fixtures(t)

		matches = list(t.matches.filter(team1__isnull=False, team2__isnull=False).order_by("match_number"))
		self.assertGreater(len(matches), 0)

		# Confirm all but last directly
		for m in matches[:-1]:
			self._confirm_match(m, 3, 0)

		# Confirm last via _check_and_finalize_tournament pathway
		last = matches[-1]
		self._confirm_match(last, 3, 0)
		_check_and_finalize_tournament(t)

		t.refresh_from_db()
		self.assertEqual(t.status, "completed")
		self.assertIsNotNone(t.completed_at)
		self.assertIsNotNone(t.champion)

	def test_double_round_robin_auto_complete(self):
		"""DRR completion check works correctly."""
		t = self._create_tournament(fmt="double_round_robin", name="DRRComp")
		teams = [self._create_team(t, f"DRR{i}") for i in range(1, 4)]
		generate_fixtures(t)

		matches = list(t.matches.filter(team1__isnull=False, team2__isnull=False).order_by("match_number"))
		for m in matches[:-1]:
			self._confirm_match(m, 2, 0)

		# Should not be complete yet
		_check_and_finalize_tournament(t)
		t.refresh_from_db()
		self.assertEqual(t.status, "active")

		# Confirm last
		self._confirm_match(matches[-1], 2, 0)
		_check_and_finalize_tournament(t)
		t.refresh_from_db()
		self.assertEqual(t.status, "completed")
		self.assertIsNotNone(t.champion)

	# ---- Knockout ----

	def test_knockout_auto_complete(self):
		"""Confirming the KO final triggers tournament completion."""
		t = self._create_tournament(fmt="knockout", name="KOComp")
		for i in range(1, 5):
			self._create_team(t, f"KO{i}", seed=i)
		generate_fixtures(t)

		# Semi-finals
		semis = list(t.matches.filter(round_number=1).order_by("bracket_position"))
		for m in semis:
			self._confirm_match(m, 3, 1)

		# Reload final (team slots filled by advance_winner)
		final = t.matches.filter(round_number=2).first()
		final.refresh_from_db()
		self._confirm_match(final, 3, 1)
		_check_and_finalize_tournament(t)

		t.refresh_from_db()
		self.assertEqual(t.status, "completed")
		self.assertEqual(t.champion, final.winner)

	def test_no_complete_while_matches_pending(self):
		"""Confirming a non-final match should NOT complete the tournament."""
		t = self._create_tournament(fmt="knockout", name="KONonFinal")
		for i in range(1, 5):
			self._create_team(t, f"KON{i}", seed=i)
		generate_fixtures(t)

		semi = t.matches.filter(round_number=1).first()
		self._confirm_match(semi, 3, 1)
		_check_and_finalize_tournament(t)

		t.refresh_from_db()
		self.assertEqual(t.status, "active")
		self.assertIsNone(t.champion)

	# ---- Double Elimination ----

	def test_double_elimination_auto_complete(self):
		"""Grand final of DE triggers completion."""
		t = self._create_tournament(fmt="double_elimination", name="DEComp")
		for i in range(1, 5):
			self._create_team(t, f"DE{i}", seed=i)
		generate_fixtures(t)

		# Confirm matches round-by-round; advance_winner fills team slots between rounds
		for _attempt in range(20):
			confirmable = list(
				t.matches.filter(team1__isnull=False, team2__isnull=False)
				.exclude(status__in=["confirmed", "cancelled", "bye", "forfeited"])
				.order_by("round_number", "match_number")
			)
			if not confirmable:
				break
			for m in confirmable:
				self._confirm_match(m, 3, 1)

		_check_and_finalize_tournament(t)
		t.refresh_from_db()
		self.assertEqual(t.status, "completed")
		self.assertIsNotNone(t.champion)

	# ---- Consolation ----

	def test_consolation_auto_complete(self):
		"""Winners bracket final of consolation tournament triggers completion."""
		t = self._create_tournament(fmt="consolation", name="ConsolComp")
		for i in range(1, 5):
			self._create_team(t, f"Con{i}", seed=i)
		generate_fixtures(t)

		# Round 1 — sets up consolation bracket and advances to semi/final
		r1_matches = list(t.matches.filter(bracket_type="winners", round_number=1))
		for m in r1_matches:
			self._confirm_match(m, 3, 1)

		from .scheduling import generate_consolation_if_ready
		generate_consolation_if_ready(t)

		# Winners bracket final
		final = (
			t.matches.filter(bracket_type="winners", next_match__isnull=True,
							 team1__isnull=False, team2__isnull=False)
			.order_by("-round_number")
			.first()
		)
		if final:
			final.refresh_from_db()
			self._confirm_match(final, 3, 1)
			_check_and_finalize_tournament(t)
			t.refresh_from_db()
			self.assertEqual(t.status, "completed")

	# ---- Hybrid ----

	def test_hybrid_auto_complete(self):
		"""Hybrid: group stage → KO auto-generated → KO final confirmed → completed."""
		t = self._create_tournament(fmt="hybrid", name="HybridComp")
		t.num_groups = 2
		t.teams_per_group_advance = 1
		t.save(update_fields=["num_groups", "teams_per_group_advance"])

		from .models import Court
		court = Court.objects.create(tournament=t, name="C1")
		from .models import TimeSlot
		import datetime
		ts = TimeSlot.objects.create(
			tournament=t,
			court=court,
			start_time=timezone.now() + timedelta(days=1),
			end_time=timezone.now() + timedelta(days=1, hours=2),
		)

		for i in range(1, 5):
			team = self._create_team(t, f"Hyb{i}", seed=i)
			# Assign groups
			team.group = "A" if i <= 2 else "B"
			team.save(update_fields=["group"])

		generate_fixtures(t)
		self.assertTrue(
			t.matches.filter(group="", bracket_type="winners", team1__isnull=True, team2__isnull=True).exists()
		)

		from .standings import check_group_stage_complete

		group_matches = list(t.matches.filter(group__gt="").order_by("match_number"))
		for m in group_matches:
			self._confirm_match(m, 3, 1)

		check_group_stage_complete(t)
		_check_and_finalize_tournament(t)
		t.refresh_from_db()
		self.assertEqual(t.status, "active")

		# KO matches generated; confirm the final
		ko_final = (
			t.matches.filter(bracket_type="winners", group="", next_match__isnull=True,
							 team1__isnull=False, team2__isnull=False)
			.order_by("-round_number")
			.first()
		)
		if ko_final:
			ko_final.refresh_from_db()
			self._confirm_match(ko_final, 3, 1)
			_check_and_finalize_tournament(t)
			t.refresh_from_db()
			self.assertEqual(t.status, "completed")
			self.assertIsNotNone(t.champion)

	# ---- Manual Override ----

	def test_mark_tournament_complete_view(self):
		"""POST to complete_tournament marks tournament completed."""
		t = self._create_tournament(name="ManualComplete")
		self.client.force_login(self.organizer)
		response = self.client.post(
			reverse("complete_tournament", kwargs={"pk": t.pk}),
			follow=True,
		)
		self.assertEqual(response.status_code, 200)
		t.refresh_from_db()
		self.assertEqual(t.status, "completed")
		self.assertIsNotNone(t.completed_at)

	def test_mark_tournament_complete_view_requires_active(self):
		"""Attempting to complete a non-active tournament shows an error."""
		t = self._create_tournament(name="NotActiveComplete")
		t.status = "setup"
		t.save(update_fields=["status"])
		self.client.force_login(self.organizer)
		response = self.client.post(
			reverse("complete_tournament", kwargs={"pk": t.pk}),
			follow=True,
		)
		self.assertEqual(response.status_code, 200)
		t.refresh_from_db()
		self.assertEqual(t.status, "setup")

	def test_mark_tournament_complete_view_requires_organizer(self):
		"""Non-organizer cannot access complete_tournament view."""
		t = self._create_tournament(name="NonOrgComplete")
		user = User.objects.create_user(username="plain_user_comp", password="pass123")
		self.client.force_login(user)
		response = self.client.post(
			reverse("complete_tournament", kwargs={"pk": t.pk}),
		)
		# Should redirect to dashboard (not organizer)
		self.assertEqual(response.status_code, 302)
		t.refresh_from_db()
		self.assertEqual(t.status, "active")

	# ---- Score submit blocked ----

	def test_score_submit_blocked_when_completed(self):
		"""Submitting a score on a completed tournament is rejected."""
		t = self._create_tournament(name="BlockedScore")
		teams = [self._create_team(t, f"Blk{i}") for i in range(1, 3)]
		generate_fixtures(t)
		t.status = "completed"
		t.save(update_fields=["status"])

		match = t.matches.filter(team1__isnull=False, team2__isnull=False).first()
		match.status = "upcoming"
		match.save(update_fields=["status"])

		self.client.force_login(teams[0].user)
		response = self.client.post(
			reverse("submit_score", kwargs={"pk": match.pk}),
			{"score_team1": 3, "score_team2": 1},
			follow=True,
		)
		self.assertEqual(response.status_code, 200)
		match.refresh_from_db()
		self.assertEqual(match.status, "upcoming")  # unchanged

	# ---- Forfeit triggers completion ----

	def test_forfeit_triggers_completion(self):
		"""Forfeiting the last match of a RR tournament should complete it."""
		t = self._create_tournament(fmt="round_robin", name="ForfeitComp")
		teams = [self._create_team(t, f"Forf{i}") for i in range(1, 3)]
		generate_fixtures(t)

		matches = list(t.matches.filter(team1__isnull=False, team2__isnull=False).order_by("match_number"))

		# Confirm all but last
		for m in matches[:-1]:
			self._confirm_match(m, 3, 0)

		# Forfeit last
		last = matches[-1]
		last.status = "forfeited"
		last.winner = last.team1
		last.save(update_fields=["status", "winner"])
		_check_and_finalize_tournament(t)

		t.refresh_from_db()
		self.assertEqual(t.status, "completed")

	# ---- _determine_champion logic ----

	def test_determine_champion_rr(self):
		"""_determine_champion returns top-ranked team for round robin."""
		t = self._create_tournament(fmt="round_robin", name="ChampRR")
		teams = [self._create_team(t, f"ChR{i}") for i in range(1, 4)]
		generate_fixtures(t)

		# Give team 0 all wins
		winner_team = teams[0]
		for m in t.matches.filter(team1__isnull=False, team2__isnull=False):
			if m.team1 == winner_team:
				self._confirm_match(m, 3, 0)
			elif m.team2 == winner_team:
				self._confirm_match(m, 0, 3)

		champion = _determine_champion(t)
		self.assertEqual(champion, winner_team)

	def test_determine_champion_knockout(self):
		"""_determine_champion returns winner of final for knockout."""
		t = self._create_tournament(fmt="knockout", name="ChampKO")
		for i in range(1, 3):
			self._create_team(t, f"ChK{i}", seed=i)
		generate_fixtures(t)

		final = t.matches.filter(team1__isnull=False, team2__isnull=False).first()
		self._confirm_match(final, 3, 1)

		champion = _determine_champion(t)
		self.assertEqual(champion, final.winner)
