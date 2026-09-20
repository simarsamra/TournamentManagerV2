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
