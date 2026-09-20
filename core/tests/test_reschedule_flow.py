"""The request-reschedule flow: open-slot choice, same-day rules, captain/participant permissions.

Split out of the old core/tests.py (UXAndLogicRegressionTests) — see
FOLLOWUP_PLAN.md F-3.
"""
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from ..models import (
    Team,
    Match,
    Court,
    OpenSlot,
    RescheduleRequest,
    TeamMembership,
    TeamTournamentParticipation,
    TournamentIndividualRegistration,
)

from .helpers import UXRegressionTestCase, _captain_user


class RescheduleRequestFlowTests(UXRegressionTestCase):

    def test_request_reschedule_can_use_open_slot_choice(self):
        tournament = self._create_tournament(name="Open Slot Choice")
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["status", "started_at"])
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

        self.client.force_login(_captain_user(team1))
        response = self.client.post(
            reverse("request_reschedule", kwargs={"pk": match.pk}),
            {"open_slot": str(slot.pk), "reason": "Use free slot"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        rr = RescheduleRequest.objects.get(match=match, requested_by=_captain_user(team1))
        self.assertEqual(rr.new_time, slot.start_time)
        self.assertEqual(rr.new_court, alt_court)

    def test_request_reschedule_open_slot_hx_request_returns_partial_and_creates_request(self):
        tournament = self._create_tournament(name="Open Slot Choice HTMX")
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["status", "started_at"])
        primary_court = Court.objects.create(tournament=tournament, name="Primary HTMX", is_available=True)
        alt_court = Court.objects.create(tournament=tournament, name="Alt HTMX", is_available=True)
        team1 = self._create_team(tournament, "Res HTMX A", username="res_htmx_a_user")
        team2 = self._create_team(tournament, "Res HTMX B", username="res_htmx_b_user")
        match = Match.objects.create(
            tournament=tournament,
            match_number=31,
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
            reason="Free slot HTMX",
        )

        self.client.force_login(_captain_user(team1))
        response = self.client.post(
            reverse("request_reschedule", kwargs={"pk": match.pk}),
            {"open_slot": str(slot.pk), "reason": "Use free slot over HTMX"},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reschedule request sent")
        self.assertContains(response, "Request Reschedule")
        self.assertNotContains(response, "<!DOCTYPE html>")
        rr = RescheduleRequest.objects.get(match=match, requested_by=_captain_user(team1))
        self.assertEqual(rr.new_time, slot.start_time)
        self.assertEqual(rr.new_court, alt_court)

    def test_match_detail_reschedule_shows_open_slot_date_in_list(self):
        tournament = self._create_tournament(name="Readable Slot Picker")
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["status", "started_at"])
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

        self.client.force_login(_captain_user(team1))
        response = self.client.get(reverse("match_detail", kwargs={"pk": match.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'type="radio"')
        self.assertContains(response, alt_court.name)
        self.assertContains(response, timezone.localtime(slot.start_time).strftime("%b %d, %Y"))

    def test_match_detail_reschedule_shows_same_day_context_for_both_teams(self):
        tournament = self._create_tournament(name="Same Day Slot Context")
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["status", "started_at"])
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

        self.client.force_login(_captain_user(team1))
        response = self.client.get(reverse("match_detail", kwargs={"pk": match.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Same-day team schedules")
        self.assertContains(response, team1.name)
        self.assertContains(response, team2.name)
        self.assertContains(response, court_x.name)
        self.assertContains(response, court_z.name)

    def test_match_detail_reschedule_uses_display_names_for_individual_mode(self):
        tournament = self._create_tournament(name="Individual Slot Context")
        tournament.registration_mode = "individual"
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["registration_mode", "status", "started_at"])
        primary_court = Court.objects.create(tournament=tournament, name="Primary", is_available=True)
        court_x = Court.objects.create(tournament=tournament, name="Court X", is_available=True)

        user1 = User.objects.create_user(username="individual_slot_a", password="pass123")
        user2 = User.objects.create_user(username="individual_slot_b", password="pass123")
        user3 = User.objects.create_user(username="individual_slot_c", password="pass123")

        shadow1 = Team.objects.create(name="__tm_shadow_alpha", sport_type=tournament.sport_type, is_internal=True)
        shadow2 = Team.objects.create(name="__tm_shadow_beta", sport_type=tournament.sport_type, is_internal=True)
        shadow3 = Team.objects.create(name="__tm_shadow_gamma", sport_type=tournament.sport_type, is_internal=True)

        for seed, shadow in enumerate([shadow1, shadow2, shadow3], start=1):
            TeamTournamentParticipation.objects.create(team=shadow, tournament=tournament, status="active", seed=seed)

        TeamMembership.objects.create(team=shadow1, user=user1, role="captain")
        TeamMembership.objects.create(team=shadow2, user=user2, role="captain")
        TeamMembership.objects.create(team=shadow3, user=user3, role="captain")

        TournamentIndividualRegistration.objects.create(
            tournament=tournament,
            user=user1,
            display_name="Player A",
            shadow_team=shadow1,
            status="active",
        )
        TournamentIndividualRegistration.objects.create(
            tournament=tournament,
            user=user2,
            display_name="Player B",
            shadow_team=shadow2,
            status="active",
        )
        TournamentIndividualRegistration.objects.create(
            tournament=tournament,
            user=user3,
            display_name="Player C",
            shadow_team=shadow3,
            status="active",
        )

        match = Match.objects.create(
            tournament=tournament,
            match_number=401,
            team1=shadow1,
            team2=shadow2,
            court=primary_court,
            scheduled_time=timezone.now() + timedelta(days=1),
            scheduled_end_time=timezone.now() + timedelta(days=1, minutes=30),
            status="upcoming",
        )
        slot_start = timezone.now() + timedelta(days=3, hours=2)
        Match.objects.create(
            tournament=tournament,
            match_number=402,
            team1=shadow1,
            team2=shadow3,
            court=court_x,
            scheduled_time=slot_start - timedelta(hours=1),
            scheduled_end_time=slot_start - timedelta(minutes=30),
            status="upcoming",
        )
        OpenSlot.objects.create(
            tournament=tournament,
            court=primary_court,
            start_time=slot_start,
            end_time=slot_start + timedelta(minutes=30),
            reason="Individual mode review",
        )

        self.client.force_login(user1)
        response = self.client.get(reverse("match_detail", kwargs={"pk": match.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Player A")
        self.assertContains(response, "Player B")
        self.assertContains(response, "Player C")
        self.assertNotContains(response, "__tm_shadow_alpha")
        self.assertNotContains(response, "__tm_shadow_beta")
        self.assertNotContains(response, "__tm_shadow_gamma")

    def test_reschedule_request_hides_self_actions_and_shows_requester_username(self):
        tournament = self._create_tournament(name="Reschedule Request UI")
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["status", "started_at"])
        court = Court.objects.create(tournament=tournament, name="Center Court", is_available=True)
        team1 = self._create_team(tournament, "Team 009", username="team009_captain")
        team2 = self._create_team(tournament, "Team 010", username="team010_captain")
        match = Match.objects.create(
            tournament=tournament,
            match_number=45,
            team1=team1,
            team2=team2,
            court=court,
            scheduled_time=timezone.now() + timedelta(days=1),
            scheduled_end_time=timezone.now() + timedelta(days=1, minutes=30),
            status="upcoming",
        )
        captain = _captain_user(team1)
        RescheduleRequest.objects.create(
            match=match,
            requested_by=captain,
            new_time=timezone.now() + timedelta(days=2),
            new_court=court,
            reason="Conflict",
        )

        self.client.force_login(captain)
        response = self.client.get(reverse("match_detail", kwargs={"pk": match.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "team009_captain")
        self.assertContains(response, "<th>Team</th>", html=False)
        self.assertNotContains(response, "Approve")
        self.assertNotContains(response, "Reject")

    def test_rescheduling_view_shows_requester_and_hides_self_actions(self):
        tournament = self._create_tournament(name="Rescheduling Dashboard")
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["status", "started_at"])
        court = Court.objects.create(tournament=tournament, name="Center Court", is_available=True)
        team1 = self._create_team(tournament, "Team 009", username="team009_rescheduling")
        team2 = self._create_team(tournament, "Team 010", username="team010_rescheduling")
        match = Match.objects.create(
            tournament=tournament,
            match_number=46,
            team1=team1,
            team2=team2,
            court=court,
            scheduled_time=timezone.now() + timedelta(days=1),
            scheduled_end_time=timezone.now() + timedelta(days=1, minutes=30),
            status="upcoming",
        )
        captain = _captain_user(team1)
        RescheduleRequest.objects.create(
            match=match,
            requested_by=captain,
            new_time=timezone.now() + timedelta(days=2),
            new_court=court,
            reason="Conflict",
        )

        self.client.force_login(captain)
        session = self.client.session
        session["selected_tournament_id"] = tournament.pk
        session.save()
        response = self.client.get(reverse("rescheduling"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "team009_rescheduling")
        self.assertNotContains(response, "✓")
        self.assertNotContains(response, "✗")

    def test_request_reschedule_accepts_open_slot_backed_by_completed_match(self):
        tournament = self._create_tournament(name="Completed Match Slot")
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["status", "started_at"])
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

        self.client.force_login(_captain_user(team1))
        response = self.client.post(
            reverse("request_reschedule", kwargs={"pk": match.pk}),
            {"open_slot": str(slot.pk), "reason": "Move to open slot"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(RescheduleRequest.objects.filter(match=match, requested_by=_captain_user(team1)).exists())
        self.assertFalse(any("conflict" in str(m).lower() for m in response.context["messages"]))

    def test_request_reschedule_allows_same_day_if_times_do_not_overlap(self):
        tournament = self._create_tournament(name="Same Day Reschedule")
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["status", "started_at"])
        court1 = Court.objects.create(tournament=tournament, name="Court 1", is_available=True)
        court2 = Court.objects.create(tournament=tournament, name="Court 2", is_available=True)
        team9 = self._create_team(tournament, "Team 9", username="same_day_team9")
        team10 = self._create_team(tournament, "Team 10", username="same_day_team10")
        other_team = self._create_team(tournament, "Other Team", username="same_day_other")
        self._create_team(tournament, "Third Team", username="same_day_third")
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

        self.client.force_login(_captain_user(team10))
        response = self.client.post(
            reverse("request_reschedule", kwargs={"pk": match.pk}),
            {"open_slot": str(slot.pk), "reason": "Later the same day"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(RescheduleRequest.objects.filter(match=match, requested_by=_captain_user(team10)).exists())
        self.assertFalse(any("already has another match scheduled on that day" in str(m).lower() for m in response.context["messages"]))

    def test_request_reschedule_allows_individual_participant_without_captain_role(self):
        tournament = self._create_tournament(name="Individual Reschedule Permission")
        tournament.registration_mode = "individual"
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["registration_mode", "status", "started_at"])

        court_a = Court.objects.create(tournament=tournament, name="Court A", is_available=True)
        court_b = Court.objects.create(tournament=tournament, name="Court B", is_available=True)

        player1 = User.objects.create_user(username="individual_reschedule_p1", password="pass123")
        player2 = User.objects.create_user(username="individual_reschedule_p2", password="pass123")

        shadow1 = Team.objects.create(name="__tm_shadow_ind_rs_1", sport_type=tournament.sport_type, is_internal=True)
        shadow2 = Team.objects.create(name="__tm_shadow_ind_rs_2", sport_type=tournament.sport_type, is_internal=True)

        TeamTournamentParticipation.objects.create(team=shadow1, tournament=tournament, status="active", seed=1)
        TeamTournamentParticipation.objects.create(team=shadow2, tournament=tournament, status="active", seed=2)

        TournamentIndividualRegistration.objects.create(
            tournament=tournament,
            user=player1,
            display_name="Ind Player 1",
            shadow_team=shadow1,
            status="active",
        )
        TournamentIndividualRegistration.objects.create(
            tournament=tournament,
            user=player2,
            display_name="Ind Player 2",
            shadow_team=shadow2,
            status="active",
        )

        match = Match.objects.create(
            tournament=tournament,
            match_number=71,
            team1=shadow1,
            team2=shadow2,
            court=court_a,
            scheduled_time=timezone.now() + timedelta(days=1),
            scheduled_end_time=timezone.now() + timedelta(days=1, minutes=30),
            status="upcoming",
        )
        slot = OpenSlot.objects.create(
            tournament=tournament,
            court=court_b,
            start_time=timezone.now() + timedelta(days=3),
            end_time=timezone.now() + timedelta(days=3, minutes=30),
            reason="Individual free slot",
        )

        self.client.force_login(player1)
        response = self.client.post(
            reverse("request_reschedule", kwargs={"pk": match.pk}),
            {"open_slot": str(slot.pk), "reason": "Need to move"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(RescheduleRequest.objects.filter(match=match, requested_by=player1).exists())

    def test_request_reschedule_team_tournament_requires_captain(self):
        tournament = self._create_tournament(name="Team Captain Reschedule Gate")
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["status", "started_at"])

        court_a = Court.objects.create(tournament=tournament, name="Court A", is_available=True)
        court_b = Court.objects.create(tournament=tournament, name="Court B", is_available=True)

        team1 = self._create_team(tournament, "Gate Team 1", username="gate_team1_captain")
        team2 = self._create_team(tournament, "Gate Team 2", username="gate_team2_captain")
        non_captain = User.objects.create_user(username="gate_team1_member", password="pass123")
        TeamMembership.objects.create(team=team1, user=non_captain, role="member")

        match = Match.objects.create(
            tournament=tournament,
            match_number=72,
            team1=team1,
            team2=team2,
            court=court_a,
            scheduled_time=timezone.now() + timedelta(days=1),
            scheduled_end_time=timezone.now() + timedelta(days=1, minutes=30),
            status="upcoming",
        )
        slot = OpenSlot.objects.create(
            tournament=tournament,
            court=court_b,
            start_time=timezone.now() + timedelta(days=3),
            end_time=timezone.now() + timedelta(days=3, minutes=30),
            reason="Team free slot",
        )

        self.client.force_login(non_captain)
        response = self.client.post(
            reverse("request_reschedule", kwargs={"pk": match.pk}),
            {"open_slot": str(slot.pk), "reason": "Trying as member"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(RescheduleRequest.objects.filter(match=match, requested_by=non_captain).exists())
        self.assertTrue(any("team captain" in str(m).lower() for m in response.context["messages"]))
