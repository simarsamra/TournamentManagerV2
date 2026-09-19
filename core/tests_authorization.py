"""Authorization guards for views that were reachable by the wrong people."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from core.models import (
    Court, Match, Team, TeamMembership, TeamTournamentParticipation, Tournament,
    TournamentIndividualRegistration,
)


class AnalyticsAndAuditAccessTests(TestCase):
    """Both views carried only @login_required, and _get_tournament falls back
    to an arbitrary tournament for a user enrolled in nothing."""

    def setUp(self):
        self.tournament = Tournament.objects.create(
            name="A", format="round_robin", status="active", players_per_team=1
        )
        self.team = Team.objects.create(name="Alpha")
        TeamTournamentParticipation.objects.create(
            team=self.team, tournament=self.tournament, status="active"
        )
        self.player = User.objects.create_user(
            username="player", password="Regression-Pass-1"
        )
        TeamMembership.objects.create(team=self.team, user=self.player, role="captain")
        self.outsider = User.objects.create_user(
            username="nobody", password="Regression-Pass-1"
        )
        self.org = User.objects.create_user(
            username="org", password="Regression-Pass-1", is_staff=True
        )

    def test_outsider_cannot_read_analytics(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get("/analytics/").status_code, 302)

    def test_outsider_cannot_read_audit_log(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get("/audit-log/").status_code, 302)

    def test_participant_can_read_analytics(self):
        self.client.force_login(self.player)
        self.assertEqual(self.client.get("/analytics/").status_code, 200)

    def test_participant_cannot_read_audit_log(self):
        self.client.force_login(self.player)
        self.assertEqual(self.client.get("/audit-log/").status_code, 302)

    def test_participant_does_not_see_the_audit_trail_on_analytics(self):
        self.client.force_login(self.player)
        response = self.client.get("/analytics/")
        self.assertNotContains(response, "Recent Activity")

    def test_organizer_can_read_both(self):
        self.client.force_login(self.org)
        self.assertEqual(self.client.get("/analytics/").status_code, 200)
        self.assertEqual(self.client.get("/audit-log/").status_code, 200)


class DisputeAuthorizationTests(TestCase):
    """dispute_score checked that the user had a team and had not submitted the
    score, but never that their team was in the match."""

    def setUp(self):
        self.tournament = Tournament.objects.create(
            name="D", format="round_robin", status="active",
            players_per_team=1, default_match_duration=30,
        )
        self.court = Court.objects.create(tournament=self.tournament, name="C1")
        self.users, self.teams = [], []
        for i in range(3):
            team = Team.objects.create(name=f"T{i}")
            TeamTournamentParticipation.objects.create(
                team=team, tournament=self.tournament, status="active"
            )
            user = User.objects.create_user(
                username=f"u{i}", password="Regression-Pass-1"
            )
            TeamMembership.objects.create(team=team, user=user, role="captain")
            self.teams.append(team)
            self.users.append(user)
        self.match = Match.objects.create(
            tournament=self.tournament, match_number=1,
            team1=self.teams[0], team2=self.teams[1], court=self.court,
            scheduled_time=timezone.now() + timedelta(days=1),
            scheduled_end_time=timezone.now() + timedelta(days=1, minutes=30),
            status="upcoming",
        )

    def _submit_score(self):
        self.client.force_login(self.users[0])
        self.client.post(
            f"/match/{self.match.pk}/submit-score/",
            {"score_team1": 3, "score_team2": 1, "notes": ""},
        )
        self.client.logout()
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, "pending_confirmation")

    def test_non_participant_cannot_dispute(self):
        self._submit_score()
        self.client.force_login(self.users[2])   # in the tournament, not the match
        self.client.post(
            f"/match/{self.match.pk}/dispute-score/", {"dispute_notes": "x"}
        )
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, "pending_confirmation")
        self.assertIsNone(self.match.disputed_by)

    def test_opponent_can_still_dispute(self):
        self._submit_score()
        self.client.force_login(self.users[1])   # the actual opponent
        self.client.post(
            f"/match/{self.match.pk}/dispute-score/", {"dispute_notes": "x"}
        )
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, "disputed")
        self.assertEqual(self.match.disputed_by, self.users[1])


class OpenRedirectTests(TestCase):
    """select_tournament passed a POSTed 'next' straight to redirect()."""

    def setUp(self):
        self.tournament = Tournament.objects.create(
            name="R", format="round_robin", status="active", players_per_team=1
        )
        self.team = Team.objects.create(name="Alpha")
        TeamTournamentParticipation.objects.create(
            team=self.team, tournament=self.tournament, status="active"
        )
        self.user = User.objects.create_user(
            username="u", password="Regression-Pass-1"
        )
        TeamMembership.objects.create(team=self.team, user=self.user, role="captain")
        self.client.force_login(self.user)

    def test_external_next_is_rejected(self):
        response = self.client.post(
            "/tournament/select/",
            {"tournament_id": self.tournament.pk, "next": "https://evil.example/pwn"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("evil.example", response.headers["Location"])

    def test_protocol_relative_next_is_rejected(self):
        response = self.client.post(
            "/tournament/select/",
            {"tournament_id": self.tournament.pk, "next": "//evil.example/pwn"},
        )
        self.assertNotIn("evil.example", response.headers["Location"])

    def test_local_next_is_honoured(self):
        response = self.client.post(
            "/tournament/select/",
            {"tournament_id": self.tournament.pk, "next": "/fixtures/"},
        )
        self.assertEqual(response.headers["Location"], "/fixtures/")


class RescheduleHelperTests(TestCase):
    """_can_manage_reschedule returned True for any authenticated user in an
    individual-mode tournament, without checking they were the competitor."""

    def test_individual_mode_requires_owning_the_registration(self):
        from core.views import _can_manage_reschedule, _ensure_shadow_team_for_registration

        tournament = Tournament.objects.create(
            name="I", format="round_robin", status="active",
            players_per_team=1, registration_mode="individual",
        )
        alice = User.objects.create_user(username="alice", password="Regression-Pass-1")
        bob = User.objects.create_user(username="bob", password="Regression-Pass-1")
        reg_a = TournamentIndividualRegistration.objects.create(
            tournament=tournament, user=alice, display_name="Alice", status="active"
        )
        reg_b = TournamentIndividualRegistration.objects.create(
            tournament=tournament, user=bob, display_name="Bob", status="active"
        )
        team_a = _ensure_shadow_team_for_registration(reg_a, tournament.sport_type)
        team_b = _ensure_shadow_team_for_registration(reg_b, tournament.sport_type)

        self.assertTrue(_can_manage_reschedule(alice, tournament, team_a))
        self.assertFalse(_can_manage_reschedule(alice, tournament, team_b))
