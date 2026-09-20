"""A GET smoke test over every no-argument route, for three personas (T-6.2).

This exists because T-2.1 was a view that raised a 500 for an entire class of
user and nothing caught it. The bar here is deliberately low -- no route may
return a 5xx -- but it is a bar that walks the whole URLconf, so a new view
added without a test is still covered against crashing outright.

Routes that take arguments are covered by the targeted suites; this module
enumerates the rest from core.urls rather than from a hand-written list, so it
picks up new routes automatically.
"""
import re

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from core import urls as core_urls
from core.models import (
    Court, OrganizerProfile, Team, TeamMembership,
    TeamTournamentParticipation, Tournament,
)

# POST-only endpoints. A GET returns 405, which is correct behaviour, not a
# crash -- but asserting "not 5xx" on them would be vacuous, so they get their
# own assertion below instead.
POST_ONLY = {
    "logout",
    "create_backup",
    "restore_backup",
    "delete_backup",
    "mark_notifications_read",
    "stop_impersonating",
}


def _no_argument_route_names():
    names = []
    for pattern in core_urls.urlpatterns:
        name = getattr(pattern, "name", None)
        if not name:
            continue
        if re.search(r"<", str(pattern.pattern)):
            continue  # takes kwargs
        names.append(name)
    return sorted(set(names))


class UrlSmokeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.route_names = _no_argument_route_names()

        cls.organizer = User.objects.create_user(
            username="smoke_org", password="Smoke-Pass-1"
        )
        OrganizerProfile.objects.filter(user=cls.organizer).update(verified=True)

        cls.plain = User.objects.create_user(
            username="smoke_user", password="Smoke-Pass-1"
        )

        # Enough of a world that the pages have something to render.
        cls.tournament = Tournament.objects.create(
            name="Smoke Open", format="round_robin", players_per_team=1,
            start_date=timezone.localdate(), status="registration_open",
            created_by=cls.organizer,
        )
        Court.objects.create(tournament=cls.tournament, name="Court 1")
        team = Team.objects.create(name="Smoke Team")
        TeamMembership.objects.create(team=team, user=cls.plain, role="captain")
        TeamTournamentParticipation.objects.create(
            team=team, tournament=cls.tournament, status="active"
        )

    def test_the_enumeration_actually_found_routes(self):
        """Guard against this whole module silently passing on an empty list."""
        self.assertGreaterEqual(len(self.route_names), 30)
        self.assertIn("dashboard", self.route_names)
        self.assertIn("settings", self.route_names)

    def _walk(self, persona):
        for name in self.route_names:
            if name in POST_ONLY:
                continue
            try:
                path = reverse(name)
            except NoReverseMatch:
                continue
            with self.subTest(persona=persona, route=name):
                response = self.client.get(path, follow=True)
                self.assertLess(
                    response.status_code, 500,
                    f"{persona}: GET {name} ({path}) returned "
                    f"{response.status_code}",
                )

    def test_anonymous_gets_no_server_error(self):
        self._walk("anonymous")

    def test_plain_user_gets_no_server_error(self):
        self.client.force_login(self.plain)
        self._walk("plain user")

    def test_organizer_gets_no_server_error(self):
        self.client.force_login(self.organizer)
        self._walk("organizer")

    def test_superuser_gets_no_server_error(self):
        admin = User.objects.create_superuser(
            "smoke_admin", "s@example.com", "Smoke-Pass-1"
        )
        self.client.force_login(admin)
        self._walk("superuser")

    def test_post_only_routes_reject_get_rather_than_crashing(self):
        self.client.force_login(self.organizer)
        for name in sorted(POST_ONLY):
            with self.subTest(route=name):
                response = self.client.get(reverse(name))
                self.assertEqual(
                    response.status_code, 405,
                    f"{name} should be POST-only",
                )

    def test_post_only_list_is_still_accurate(self):
        """If a route stops being POST-only, the walk above should start
        covering it instead of skipping it forever."""
        self.assertTrue(POST_ONLY.issubset(set(self.route_names)))
