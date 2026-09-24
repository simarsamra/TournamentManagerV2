"""Analytics page regressions (ANALYTICS_PLAN.md)."""
from django.contrib.auth.models import User
from django.test import TestCase

from core.models import (
    AuditLog, OrganizerProfile, Team, TeamMembership, TeamTournamentParticipation,
    Tournament,
)


def _make_organizer(username):
    user = User.objects.create_user(username=username, password="Regression-Pass-1")
    OrganizerProfile.objects.filter(user=user).update(verified=True)
    user.refresh_from_db()
    return user


class AnalyticsAndAuditOwnershipTests(TestCase):
    """A-1: analytics_view and audit_log_view gated on _is_organizer alone, so
    any organizer could pick another organizer's tournament with
    ?tournament=<pk> and read its audit trail."""

    SECRET = "owner-only audit detail 7f3a"
    GLOBAL_SECRET = "site-wide login detail 91c2"

    def setUp(self):
        self.owner = _make_organizer("owner")
        self.other = _make_organizer("other")
        self.admin = User.objects.create_superuser(
            username="root", password="Regression-Pass-1", email=""
        )
        self.tournament = Tournament.objects.create(
            name="Owned", format="round_robin", status="active",
            players_per_team=1, created_by=self.owner,
        )
        self.other_tournament = Tournament.objects.create(
            name="Other's", format="round_robin", status="active",
            players_per_team=1, created_by=self.other,
        )
        AuditLog.objects.create(
            user=self.owner, action="tournament_updated", details=self.SECRET,
            tournament=self.tournament,
        )
        AuditLog.objects.create(
            user=self.owner, action="login", details=self.GLOBAL_SECRET,
        )

    def _analytics(self, user, tournament=None):
        self.client.force_login(user)
        return self.client.get(
            "/analytics/", {"tournament": (tournament or self.tournament).pk}
        )

    def _audit(self, user, tournament=None):
        self.client.force_login(user)
        return self.client.get(
            "/audit-log/", {"tournament": (tournament or self.tournament).pk}
        )

    def _enroll(self, user, tournament):
        team = Team.objects.create(name=f"{user.username} team")
        TeamTournamentParticipation.objects.create(
            team=team, tournament=tournament, status="active"
        )
        TeamMembership.objects.create(team=team, user=user, role="captain")

    # -- analytics --

    def test_non_owner_organizer_is_redirected_from_analytics(self):
        response = self._analytics(self.other)
        self.assertEqual(response.status_code, 302)
        self.assertNotContains(self.client.get(response.url), self.SECRET)

    def test_owner_sees_recent_activity(self):
        response = self._analytics(self.owner)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Recent Activity")
        self.assertContains(response, self.SECRET)

    def test_site_admin_sees_recent_activity_for_any_tournament(self):
        response = self._analytics(self.admin)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.SECRET)

    def test_enrolled_non_owner_organizer_sees_analytics_but_not_activity(self):
        self._enroll(self.other, self.tournament)
        response = self._analytics(self.other)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Recent Activity")
        self.assertNotContains(response, self.SECRET)

    def test_legacy_tournament_without_creator_is_managed_by_any_organizer(self):
        legacy = Tournament.objects.create(
            name="Legacy", format="round_robin", status="active",
            players_per_team=1, created_by=None,
        )
        AuditLog.objects.create(action="legacy_event", details="legacy detail", tournament=legacy)
        response = self._analytics(self.other, legacy)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "legacy detail")

    # -- audit log --

    def test_non_owner_organizer_is_redirected_from_audit_log(self):
        response = self._audit(self.other)
        self.assertEqual(response.status_code, 302)

    def test_owner_sees_own_tournament_rows_but_not_global_rows(self):
        response = self._audit(self.owner)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.SECRET)
        self.assertNotContains(response, self.GLOBAL_SECRET)

    def test_owner_does_not_see_other_tournaments_rows(self):
        AuditLog.objects.create(
            user=self.other, action="other_event", details="other tournament detail",
            tournament=self.other_tournament,
        )
        response = self._audit(self.owner)
        self.assertNotContains(response, "other tournament detail")
        # The action filter dropdown is built from visible rows only.
        self.assertNotContains(response, "other_event")

    def test_site_admin_sees_tournament_and_global_rows(self):
        response = self._audit(self.admin)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.SECRET)
        self.assertContains(response, self.GLOBAL_SECRET)

    def test_enrolled_non_owner_organizer_cannot_read_audit_log(self):
        self._enroll(self.other, self.tournament)
        response = self._audit(self.other)
        self.assertEqual(response.status_code, 302)
