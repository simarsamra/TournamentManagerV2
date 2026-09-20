"""Coverage for the dual-role view toggle (T-6.2).

DUAL_ROLE_TOGGLE_FEATURE.md documents this feature in detail and it had no
tests at all, which is how the document drifted into describing a redirect the
code does not perform. These pin the behaviour the rewritten document claims.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import AuditLog, OrganizerProfile, Team, TeamMembership
from core.views import _has_dual_roles


def _make_organizer(username):
    user = User.objects.create_user(username=username, password="Dual-Role-Pass-1")
    OrganizerProfile.objects.filter(user=user).update(verified=True)
    # The post_save signal caches organizer_profile on this instance, so the
    # queryset .update() above is invisible to it. Re-fetch.
    return User.objects.get(pk=user.pk)


def _give_team(user, team_name):
    team = Team.objects.create(name=team_name)
    TeamMembership.objects.create(team=team, user=user, role="captain")
    return team


class DualRoleDetectionTests(TestCase):
    def test_verified_organizer_with_a_team_is_dual_role(self):
        user = _make_organizer("dual")
        _give_team(user, "Dual Team")
        self.assertTrue(_has_dual_roles(user))

    def test_organizer_without_a_team_is_not_dual_role(self):
        self.assertFalse(_has_dual_roles(_make_organizer("org_only")))

    def test_team_member_who_is_not_an_organizer_is_not_dual_role(self):
        user = User.objects.create_user("team_only", password="Dual-Role-Pass-1")
        _give_team(user, "Team Only")
        self.assertFalse(_has_dual_roles(user))

    def test_is_staff_still_counts_as_organizer(self):
        """The legacy promotion route. The document used to claim this was the
        only signal; it is one of three."""
        user = User.objects.create_user(
            "staffer", password="Dual-Role-Pass-1", is_staff=True
        )
        _give_team(user, "Staff Team")
        self.assertTrue(_has_dual_roles(user))

    def test_an_unverified_organizer_profile_does_not_count(self):
        user = User.objects.create_user("applicant", password="Dual-Role-Pass-1")
        OrganizerProfile.objects.filter(user=user).update(verified=False)
        _give_team(user, "Applicant Team")
        self.assertFalse(_has_dual_roles(user))


class DashboardViewModeTests(TestCase):
    def setUp(self):
        self.user = _make_organizer("dual")
        _give_team(self.user, "Dual Team")
        self.client.force_login(self.user)

    def test_dual_role_user_defaults_to_the_team_view(self):
        response = self.client.get(reverse("dashboard"), follow=True)
        self.assertTrue(response.context["has_dual_roles"])
        self.assertEqual(response.context["effective_view"], "team")

    def test_toggling_switches_to_the_organizer_view(self):
        self.client.get(reverse("toggle_view_preference"))
        response = self.client.get(reverse("dashboard"), follow=True)
        self.assertEqual(response.context["effective_view"], "organizer")

    def test_toggling_twice_returns_to_the_team_view(self):
        self.client.get(reverse("toggle_view_preference"))
        self.client.get(reverse("toggle_view_preference"))
        response = self.client.get(reverse("dashboard"), follow=True)
        self.assertEqual(response.context["effective_view"], "team")

    def test_the_toggle_redirects_to_the_dashboard_rather_than_tournament_setup(self):
        """The old document described a redirect to tournament_setup. There
        isn't one -- the dashboard renders both modes itself."""
        response = self.client.get(reverse("toggle_view_preference"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("dashboard"))

    def test_every_toggle_is_audited(self):
        self.client.get(reverse("toggle_view_preference"))
        entry = AuditLog.objects.filter(action="view_mode_toggled").latest("id")
        self.assertIn("organizer", entry.details)

    def test_the_ribbon_offers_the_toggle(self):
        response = self.client.get(reverse("dashboard"), follow=True)
        self.assertContains(response, reverse("toggle_view_preference"))


class NonDualRoleTests(TestCase):
    def test_organizer_without_a_team_always_sees_the_organizer_view(self):
        user = _make_organizer("org_only")
        self.client.force_login(user)
        response = self.client.get(reverse("dashboard"), follow=True)
        self.assertFalse(response.context["has_dual_roles"])
        self.assertEqual(response.context["effective_view"], "organizer")
        self.assertNotContains(response, reverse("toggle_view_preference"))

    def test_the_toggle_rejects_a_non_dual_role_user(self):
        user = _make_organizer("org_only")
        self.client.force_login(user)
        response = self.client.get(reverse("toggle_view_preference"))

        self.assertEqual(response["Location"], reverse("dashboard"))
        self.assertNotIn("view_mode", self.client.session)

    def test_the_toggle_requires_a_login(self):
        response = self.client.get(reverse("toggle_view_preference"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])


class RevokedOrganizerTests(TestCase):
    def test_a_stale_organizer_view_mode_does_not_survive_losing_the_role(self):
        """Edge case 6 in DUAL_ROLE_TOGGLE_FEATURE.md: the session key is left
        behind, but effective_view is recomputed from real roles."""
        user = _make_organizer("dual")
        _give_team(user, "Dual Team")
        self.client.force_login(user)
        self.client.get(reverse("toggle_view_preference"))
        self.assertEqual(self.client.session["view_mode"], "organizer")

        OrganizerProfile.objects.filter(user=user).update(verified=False)

        response = self.client.get(reverse("dashboard"), follow=True)
        self.assertEqual(self.client.session["view_mode"], "organizer")  # stale key remains
        self.assertFalse(response.context["has_dual_roles"])
        self.assertEqual(response.context["effective_view"], "team")
