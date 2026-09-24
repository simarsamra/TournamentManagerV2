"""The @throttled decorator (core.views.helpers.throttled).

Exercises the mechanism in isolation, against a bare view function, rather
than any one endpoint it's applied to -- those endpoints get their own
tests for their own behaviour. See FOLLOWUP_PLAN.md F-4 step 2.
"""
import time
from unittest import mock

from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings

from core.views.helpers import throttled

LOCMEM = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "throttling-tests",
    }
}


def _prepare(request):
    """A bare RequestFactory request has no session or messages backend;
    the decorator's block path needs both (messages.error, redirect)."""
    SessionMiddleware(lambda r: None).process_request(request)
    request.session.save()
    request._messages = FallbackStorage(request)


@override_settings(CACHES=LOCMEM)
class ThrottledDecoratorTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.factory = RequestFactory()
        self.calls = 0

        @throttled("test_scope", limit=3, window=60)
        def view(request):
            self.calls += 1
            return HttpResponse("ok")

        self.view = view

    def _post(self, ip="203.0.113.1"):
        request = self.factory.post("/whatever/", REMOTE_ADDR=ip)
        _prepare(request)
        return self.view(request)

    def test_limit_fires_after_the_configured_number_of_posts(self):
        for _ in range(3):
            self._post()
        response = self._post()

        self.assertEqual(self.calls, 3)
        self.assertEqual(response.status_code, 302)

    def test_blocked_htmx_request_navigates_instead_of_swapping_a_page(self):
        for _ in range(3):
            self._post()
        request = self.factory.post("/whatever/", REMOTE_ADDR="203.0.113.1", HTTP_HX_REQUEST="true")
        _prepare(request)
        response = self.view(request)
        self.assertEqual(self.calls, 3)
        self.assertEqual((response.status_code, response["HX-Redirect"]), (204, "/whatever/"))

    def test_get_requests_are_never_throttled(self):
        """Loading the form (a GET) must not spend the POST budget."""
        request = self.factory.get("/whatever/")
        _prepare(request)
        for _ in range(10):
            self.view(request)

        self.assertEqual(self.calls, 10)

    def test_different_ips_get_independent_counters(self):
        for _ in range(3):
            self._post(ip="203.0.113.1")
        blocked = self._post(ip="203.0.113.1")
        self.assertEqual(blocked.status_code, 302)

        allowed = self._post(ip="203.0.113.2")
        self.assertEqual(allowed.status_code, 200)

    def test_a_cache_failure_fails_open(self):
        """Same trade-off as the login throttle: a broken cache degrades
        throttling rather than taking the endpoint down."""
        with mock.patch("core.views.helpers.django_cache") as broken:
            broken.get.side_effect = Exception("no such table: tm_cache_table")
            broken.set.side_effect = Exception("no such table: tm_cache_table")
            broken.incr.side_effect = Exception("no such table: tm_cache_table")
            for _ in range(10):
                self._post()

        self.assertEqual(self.calls, 10)

    def test_the_window_expires(self):
        @throttled("short_window_scope", limit=1, window=1)
        def short_window_view(request):
            self.calls += 1
            return HttpResponse("ok")

        request = self.factory.post("/whatever/", REMOTE_ADDR="203.0.113.5")
        _prepare(request)
        short_window_view(request)  # uses up the one-request budget

        request = self.factory.post("/whatever/", REMOTE_ADDR="203.0.113.5")
        _prepare(request)
        blocked = short_window_view(request)
        self.assertEqual(blocked.status_code, 302)
        self.assertEqual(self.calls, 1)

        time.sleep(1.1)

        request = self.factory.post("/whatever/", REMOTE_ADDR="203.0.113.5")
        _prepare(request)
        allowed = short_window_view(request)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(self.calls, 2)


@override_settings(CACHES=LOCMEM)
class EndpointThrottleWiringTests(TestCase):
    """Confirms the decorator is actually applied where F-4 step 3 says it
    should be -- these are wiring smoke tests (wrong scope, wrong limit,
    wrong redirect target), not another copy of the decorator's own tests
    above."""

    def setUp(self):
        from django.contrib.auth.models import User
        from django.core.cache import cache

        from core.models import Court, Match, Team, TeamMembership, TeamTournamentParticipation, Tournament

        cache.clear()
        self.tournament = Tournament.objects.create(
            name="Throttle Wiring", format="round_robin", status="active",
            players_per_team=1, default_match_duration=30,
        )
        court = Court.objects.create(tournament=self.tournament, name="C1")
        self.teams = []
        self.users = []
        for i in range(2):
            team = Team.objects.create(name=f"ThrottleTeam{i}")
            TeamTournamentParticipation.objects.create(
                team=team, tournament=self.tournament, status="active"
            )
            user = User.objects.create_user(username=f"throttleplayer{i}", password="Regression-Pass-1")
            TeamMembership.objects.create(team=team, user=user, role="captain")
            self.teams.append(team)
            self.users.append(user)

        from datetime import timedelta

        from django.utils import timezone

        self.match = Match.objects.create(
            tournament=self.tournament, match_number=1,
            team1=self.teams[0], team2=self.teams[1], court=court,
            scheduled_time=timezone.now() + timedelta(days=1),
            scheduled_end_time=timezone.now() + timedelta(days=1, minutes=30),
            status="upcoming",
        )

    def test_submit_score_is_rate_limited_per_ip(self):
        self.client.force_login(self.users[0])
        for _ in range(30):
            self.client.post(
                f"/match/{self.match.pk}/submit-score/",
                {"score_team1": 3, "score_team2": 1, "notes": ""},
            )

        response = self.client.post(
            f"/match/{self.match.pk}/submit-score/",
            {"score_team1": 3, "score_team2": 1, "notes": ""},
        )

        self.assertRedirects(response, f"/match/{self.match.pk}/")
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any("Too many requests" in m for m in messages))

    def test_dispute_score_is_rate_limited_per_ip(self):
        self.client.force_login(self.users[0])
        self.client.post(
            f"/match/{self.match.pk}/submit-score/",
            {"score_team1": 3, "score_team2": 1, "notes": ""},
        )
        self.client.force_login(self.users[1])
        for _ in range(30):
            self.client.post(
                f"/match/{self.match.pk}/dispute-score/", {"dispute_notes": "x"}
            )

        response = self.client.post(
            f"/match/{self.match.pk}/dispute-score/", {"dispute_notes": "x"}
        )

        self.assertRedirects(response, f"/match/{self.match.pk}/")
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any("Too many requests" in m for m in messages))

    def test_team_invite_is_rate_limited_per_ip(self):
        self.client.force_login(self.users[0])
        for _ in range(20):
            self.client.post(
                f"/team/{self.teams[0].pk}/invite/", {"username": "does-not-exist"}
            )

        response = self.client.post(
            f"/team/{self.teams[0].pk}/invite/", {"username": "does-not-exist"}
        )

        self.assertRedirects(response, f"/team/{self.teams[0].pk}/invite/")
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any("Too many requests" in m for m in messages))
