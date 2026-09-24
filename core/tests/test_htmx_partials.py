"""HTMX requests get a partial back instead of a full page.

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
    TeamMembership,
    TeamTournamentParticipation,
    Notification,
    TeamInvite,
)

from .helpers import UXRegressionTestCase, _captain_user


class HtmxPartialRefreshTests(UXRegressionTestCase):

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

    def test_dashboard_htmx_request_returns_section_only(self):
        tournament = self._create_tournament(name="Live Dashboard HTMX")
        self._create_team(tournament, "Alpha Live HTMX", username="alpha_live_dashboard_htmx")
        self.client.force_login(self.organizer)

        response = self.client.get(
            reverse("dashboard"),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tournament Overview")
        self.assertNotContains(response, "<!DOCTYPE html>")

    def test_match_detail_partial_refresh_returns_section_only(self):
        tournament = self._create_tournament(name="Live Match Detail")
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["status", "started_at"])
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

        self.client.force_login(_captain_user(team1))
        response = self.client.get(
            reverse("match_detail", kwargs={"pk": match.pk}),
            {"partial": "1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Match Info")
        self.assertContains(response, "Request Reschedule")
        self.assertNotContains(response, "<!DOCTYPE html>")

    def test_team_detail_htmx_request_returns_section_only(self):
        tournament = self._create_tournament(name="HTMX Team Detail")
        team = self._create_team(tournament, "HTMX Team", username="htmx_team_captain")
        self.client.force_login(_captain_user(team))

        response = self.client.get(
            reverse("team_detail", kwargs={"pk": team.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Team Members")
        self.assertNotContains(response, "<!DOCTYPE html>")

    def test_join_tournament_htmx_request_returns_section_only(self):
        tournament = self._create_tournament(name="Join HTMX")
        tournament.status = "registration_open"
        tournament.save(update_fields=["status"])
        user = User.objects.create_user(username="join_htmx_user", password="pw12345")
        self.client.force_login(user)

        response = self.client.get(
            reverse("join_tournament", kwargs={"pk": tournament.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, tournament.name)
        self.assertNotContains(response, "<!DOCTYPE html>")

    def test_my_invites_htmx_decline_returns_partial(self):
        tournament = self._create_tournament(name="Invites HTMX")
        team = self._create_team(tournament, "Invite Team", username="invite_captain")
        invited_user = User.objects.create_user(username="invited_htmx", password="pw12345")
        invite = TeamInvite.objects.create(
            team=team,
            invited_user=invited_user,
            invited_by=_captain_user(team),
            status="pending",
        )

        self.client.force_login(invited_user)
        response = self.client.post(
            reverse("decline_team_invite", kwargs={"pk": invite.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no pending team invites")
        self.assertNotContains(response, "<!DOCTYPE html>")

    def test_registration_review_htmx_approve_returns_partial(self):
        tournament = self._create_tournament(name="Review HTMX")
        team_user = User.objects.create_user(username="review_team_captain", password="pw12345")
        team = Team.objects.create(name="Review Team")
        TeamMembership.objects.create(team=team, user=team_user, role="captain")
        part = TeamTournamentParticipation.objects.create(team=team, tournament=tournament, status="pending")

        self.client.force_login(self.organizer)
        response = self.client.post(
            reverse("approve_registration", kwargs={"tournament_pk": tournament.pk, "reg_pk": part.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Team Registrations")
        self.assertNotContains(response, "<!DOCTYPE html>")

    def test_notifications_htmx_and_mark_read_return_partial(self):
        self.client.force_login(self.organizer)
        Notification.objects.create(
            user=self.organizer,
            message="Check this alert",
            is_read=False,
        )

        response = self.client.get(
            reverse("notifications"),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Notifications")
        self.assertNotContains(response, "<!DOCTYPE html>")

        notif = Notification.objects.filter(user=self.organizer).first()
        post_response = self.client.post(
            reverse("mark_notification_read", kwargs={"pk": notif.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(post_response.status_code, 200)
        self.assertContains(post_response, "notification-badge-wrapper")
        self.assertNotContains(post_response, "<!DOCTYPE html>")
