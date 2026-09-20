"""Phase 5 hardening guards."""
from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from core.models import OrganizerProfile, Tournament
from core.views.helpers import LOGIN_ATTEMPTS_PER_IP

LOCMEM = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "hardening-tests",
    }
}


class PasswordPolicyTests(TestCase):
    """AUTH_PASSWORD_VALIDATORS was configured but never applied anywhere:
    Django only runs validators where you ask it to, and create_user does not."""

    def test_registration_rejects_a_short_password(self):
        response = self.client.post(
            "/register/",
            {
                "full_name": "Ada L", "username": "ada",
                "password": "x", "password_confirm": "x",
            },
        )
        self.assertEqual(response.status_code, 200)   # re-rendered with errors
        self.assertFalse(User.objects.filter(username="ada").exists())

    def test_registration_rejects_a_common_password(self):
        self.client.post(
            "/register/",
            {
                "full_name": "Ada L", "username": "ada2",
                "password": "password123", "password_confirm": "password123",
            },
        )
        self.assertFalse(User.objects.filter(username="ada2").exists())

    def test_registration_accepts_a_strong_password(self):
        response = self.client.post(
            "/register/",
            {
                "full_name": "Ada L", "username": "ada3",
                "password": "Regression-Pass-1", "password_confirm": "Regression-Pass-1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(username="ada3").exists())


class TestMakerGateTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="root", password="Regression-Pass-1", email=""
        )
        self.organizer = User.objects.create_user(
            username="org", password="Regression-Pass-1"
        )
        OrganizerProfile.objects.filter(user=self.organizer).update(verified=True)

    @override_settings(ENABLE_TEST_MAKER=False)
    def test_disabled_returns_404_even_for_an_admin(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/testing/").status_code, 404)

    @override_settings(ENABLE_TEST_MAKER=True)
    def test_enabled_still_refuses_a_plain_organizer(self):
        self.client.force_login(self.organizer)
        self.assertEqual(self.client.get("/testing/").status_code, 302)

    @override_settings(ENABLE_TEST_MAKER=True)
    def test_enabled_allows_a_site_admin(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/testing/").status_code, 200)


class DisputeWindowPersistenceTests(TestCase):
    """set_dispute_window mutated module globals: per-process, lost on restart,
    and invisible as configuration."""

    @override_settings(ENABLE_TEST_MAKER=True)
    def test_window_is_stored_on_the_tournament(self):
        admin = User.objects.create_superuser(
            username="root2", password="Regression-Pass-1", email=""
        )
        tournament = Tournament.objects.create(
            name="DW", format="round_robin", players_per_team=1, created_by=admin
        )
        self.client.force_login(admin)
        session = self.client.session
        session["selected_tournament_id"] = tournament.pk
        session.save()

        self.client.post(
            "/testing/",
            {"action": "set_dispute_window", "dispute_window_minutes": "25"},
        )

        tournament.refresh_from_db()
        self.assertEqual(tournament.dispute_window_minutes, 25)


class ClientIpTests(TestCase):
    """X-Forwarded-For is attacker-controlled without a trusted proxy count."""

    def _log_and_read_ip(self, **extra):
        from django.test import RequestFactory

        from core.audit import log_action
        from core.models import AuditLog

        request = RequestFactory().get("/", REMOTE_ADDR="10.0.0.1", **extra)
        request.user = User.objects.create_user(
            username=f"ipuser{AuditLog.objects.count()}", password="Regression-Pass-1"
        )
        log_action(request, "test_action")
        return AuditLog.objects.order_by("-timestamp").first().ip_address

    @override_settings(TRUSTED_PROXY_COUNT=0)
    def test_forwarded_header_is_ignored_without_trusted_proxies(self):
        ip = self._log_and_read_ip(HTTP_X_FORWARDED_FOR="1.2.3.4")
        self.assertEqual(ip, "10.0.0.1")

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_spoofed_left_most_entry_is_not_used(self):
        ip = self._log_and_read_ip(HTTP_X_FORWARDED_FOR="1.2.3.4, 203.0.113.9")
        self.assertEqual(ip, "203.0.113.9")


class LogoutMethodTests(TestCase):
    def setUp(self):
        User.objects.create_user(username="lo", password="Regression-Pass-1")

    def test_get_logout_is_rejected_and_session_survives(self):
        self.client.login(username="lo", password="Regression-Pass-1")
        response = self.client.get("/logout/")
        self.assertEqual(response.status_code, 405)
        self.assertIn("_auth_user_id", self.client.session)

    def test_post_logout_works(self):
        self.client.login(username="lo", password="Regression-Pass-1")
        response = self.client.post("/logout/")
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)


@override_settings(CACHES=LOCMEM)
class LoginThrottleTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        User.objects.create_user(username="target", password="Regression-Pass-1")

    def test_account_is_locked_after_five_failures(self):
        for _ in range(5):
            self.client.post("/login/", {"username": "target", "password": "wrong"})

        # Even the correct password is refused while the window holds.
        self.client.post(
            "/login/", {"username": "target", "password": "Regression-Pass-1"}
        )
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_different_account_is_unaffected_below_the_ip_limit(self):
        User.objects.create_user(username="other", password="Regression-Pass-1")
        for _ in range(4):
            self.client.post("/login/", {"username": "target", "password": "wrong"})

        self.client.post(
            "/login/", {"username": "other", "password": "Regression-Pass-1"}
        )
        self.assertIn("_auth_user_id", self.client.session)

    def test_login_still_works_when_the_cache_backend_is_broken(self):
        """DatabaseCache raises if createcachetable was never run. Losing
        throttling is a degradation; refusing every login is an outage."""
        from unittest import mock

        # The throttle helpers live in core.views.helpers, so that is where the
        # cache has to be replaced -- patching the package re-export would not
        # reach the name they actually call.
        with mock.patch("core.views.helpers.django_cache") as broken:
            broken.get.side_effect = Exception("no such table: tm_cache_table")
            broken.set.side_effect = Exception("no such table: tm_cache_table")
            broken.incr.side_effect = Exception("no such table: tm_cache_table")
            broken.delete.side_effect = Exception("no such table: tm_cache_table")

            self.client.post(
                "/login/", {"username": "target", "password": "Regression-Pass-1"}
            )
        self.assertIn("_auth_user_id", self.client.session)

    def test_successful_login_clears_the_counters(self):
        self.client.post("/login/", {"username": "target", "password": "wrong"})
        self.client.post(
            "/login/", {"username": "target", "password": "Regression-Pass-1"}
        )
        self.assertIn("_auth_user_id", self.client.session)

    def test_forwarded_header_cannot_bypass_the_ip_limit_without_a_trusted_proxy(self):
        """TRUSTED_PROXY_COUNT defaults to 0: an attacker must not be able to
        dodge the IP throttle by sending a new X-Forwarded-For on every
        request. Without a real proxy in front, the header is just ignored --
        everything lands on REMOTE_ADDR's single counter."""
        # Blank username: only the IP counter is touched, not a per-account
        # one, so this isolates the IP limit rather than the account limit
        # that's already covered above.
        for i in range(LOGIN_ATTEMPTS_PER_IP):
            self.client.post(
                "/login/",
                {"username": "", "password": "wrong"},
                HTTP_X_FORWARDED_FOR=f"203.0.113.{i}",
            )

        self.client.post(
            "/login/",
            {"username": "target", "password": "Regression-Pass-1"},
            HTTP_X_FORWARDED_FOR="203.0.113.250",
        )

        self.assertNotIn("_auth_user_id", self.client.session)

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_ip_counter_is_keyed_on_the_forwarded_client_behind_a_trusted_proxy(self):
        """Behind a real, configured proxy, the counter must follow the
        client X-Forwarded-For names, not the proxy's own REMOTE_ADDR --
        otherwise every visitor shares one site-wide budget (the bug this
        test pins: login_view used to read REMOTE_ADDR directly instead of
        the proxy-aware core.audit._client_ip)."""
        for i in range(LOGIN_ATTEMPTS_PER_IP):
            self.client.post(
                "/login/",
                {"username": "", "password": "wrong"},
                HTTP_X_FORWARDED_FOR="203.0.113.1",
            )

        # That client is now IP-throttled...
        self.client.post(
            "/login/",
            {"username": "target", "password": "Regression-Pass-1"},
            HTTP_X_FORWARDED_FOR="203.0.113.1",
        )
        self.assertNotIn("_auth_user_id", self.client.session)

        # ...but a second client behind the same proxy, forwarding a
        # different address, has its own, untouched budget.
        self.client.post(
            "/login/",
            {"username": "target", "password": "Regression-Pass-1"},
            HTTP_X_FORWARDED_FOR="203.0.113.2",
        )
        self.assertIn("_auth_user_id", self.client.session)


class TestOnlyPasswordHasherTests(TestCase):
    """settings.py swaps in MD5 for the test suite, because PBKDF2 was ~95% of
    the runtime. That is only ever acceptable under `manage.py test`, so pin
    the guard that decides it."""

    def test_the_guard_matches_only_an_exact_test_invocation(self):
        import tournament_manager.settings as app_settings

        decide = lambda argv: argv[1:2] == ["test"]  # noqa: E731 - mirrors settings.py

        self.assertTrue(decide(["manage.py", "test"]))
        self.assertTrue(decide(["manage.py", "test", "core.tests_hardening"]))

        # Things that must NOT weaken hashing:
        self.assertFalse(decide(["manage.py", "runserver"]))
        self.assertFalse(decide(["manage.py", "testmaker"]))
        self.assertFalse(decide(["manage.py", "migrate", "test"]))
        self.assertFalse(decide(["gunicorn", "tournament_manager.wsgi"]))
        self.assertFalse(decide(["gunicorn"]))
        self.assertFalse(decide([]))

        # And the module really does expose the flag it gates on.
        self.assertTrue(hasattr(app_settings, "RUNNING_TESTS"))

    def test_a_non_test_process_keeps_the_default_hashers(self):
        """Execute settings.py in a fresh namespace with a production-shaped
        argv and confirm it never defines the weak hasher list.

        A fresh namespace rather than importlib.reload: reload re-executes into
        the existing module dict, where PASSWORD_HASHERS would still be set
        from this very test run.
        """
        import sys as real_sys
        from pathlib import Path

        source = Path(__file__).resolve().parent.parent / "tournament_manager" / "settings.py"
        namespace = {"__file__": str(source), "__name__": "settings_under_test"}

        original = real_sys.argv
        try:
            real_sys.argv = ["gunicorn", "tournament_manager.wsgi"]
            exec(compile(source.read_text(), str(source), "exec"), namespace)
        finally:
            real_sys.argv = original

        self.assertFalse(namespace["RUNNING_TESTS"])
        self.assertNotIn("PASSWORD_HASHERS", namespace)

    def test_a_test_process_does_swap_the_hasher(self):
        """The other half of the same check, so a broken guard fails loudly
        rather than silently leaving the suite slow."""
        import sys as real_sys
        from pathlib import Path

        source = Path(__file__).resolve().parent.parent / "tournament_manager" / "settings.py"
        namespace = {"__file__": str(source), "__name__": "settings_under_test"}

        original = real_sys.argv
        try:
            real_sys.argv = ["manage.py", "test"]
            exec(compile(source.read_text(), str(source), "exec"), namespace)
        finally:
            real_sys.argv = original

        self.assertTrue(namespace["RUNNING_TESTS"])
        self.assertEqual(
            namespace["PASSWORD_HASHERS"],
            ["django.contrib.auth.hashers.MD5PasswordHasher"],
        )
