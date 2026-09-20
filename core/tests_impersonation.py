"""Coverage for impersonate_user / stop_impersonating (T-6.2).

These two views hand-edit Django's session auth keys, and until now had no
tests at all. What matters here is that only a superuser can start an
impersonation, that it cannot be aimed at another superuser, and that getting
back out always works — including when the admin's own password changed while
they were impersonating someone.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import OrganizerProfile


class ImpersonationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="admin", email="a@example.com", password="Impersonate-Pass-1"
        )
        self.other_admin = User.objects.create_superuser(
            username="admin2", email="a2@example.com", password="Impersonate-Pass-1"
        )
        self.target = User.objects.create_user(
            username="target", password="Impersonate-Pass-1"
        )
        self.organizer = User.objects.create_user(
            username="org", password="Impersonate-Pass-1"
        )
        OrganizerProfile.objects.filter(user=self.organizer).update(verified=True)

    def _start(self, target=None):
        return self.client.post(
            reverse("impersonate_user", args=[(target or self.target).pk])
        )

    # --- starting ----------------------------------------------------------

    def test_superuser_can_impersonate_a_plain_user(self):
        self.client.force_login(self.admin)
        response = self._start()

        self.assertRedirects(response, reverse("dashboard"))
        self.assertEqual(self.client.session["_auth_user_id"], str(self.target.pk))
        self.assertEqual(
            self.client.session["impersonating_original_user_pk"], self.admin.pk
        )

    def test_impersonated_session_renders_as_the_target(self):
        self.client.force_login(self.admin)
        self._start()

        response = self.client.get(reverse("dashboard"), follow=True)
        self.assertEqual(response.context["user"].pk, self.target.pk)

    def test_a_verified_organizer_cannot_impersonate(self):
        """Organizer is not admin. This is the privilege boundary."""
        self.client.force_login(self.organizer)
        response = self._start()

        self.assertRedirects(response, reverse("settings"))
        self.assertEqual(self.client.session["_auth_user_id"], str(self.organizer.pk))
        self.assertNotIn("impersonating_original_user_pk", self.client.session)

    def test_a_plain_user_cannot_impersonate(self):
        self.client.force_login(self.target)
        response = self.client.post(
            reverse("impersonate_user", args=[self.organizer.pk])
        )

        # /settings/ itself redirects a plain user, so don't follow the chain.
        self.assertRedirects(response, reverse("settings"), fetch_redirect_response=False)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.target.pk))

    def test_anonymous_cannot_impersonate(self):
        response = self._start()
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])
        self.assertNotIn("impersonating_original_user_pk", self.client.session)

    def test_a_superuser_cannot_be_impersonated(self):
        self.client.force_login(self.admin)
        response = self._start(self.other_admin)

        self.assertRedirects(response, reverse("settings"))
        self.assertEqual(self.client.session["_auth_user_id"], str(self.admin.pk))

    def test_impersonation_rejects_get(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("impersonate_user", args=[self.target.pk]))

        self.assertEqual(response.status_code, 405)
        self.assertNotIn("impersonating_original_user_pk", self.client.session)

    # --- stopping ----------------------------------------------------------

    def test_stop_restores_the_original_admin(self):
        self.client.force_login(self.admin)
        self._start()

        response = self.client.post(reverse("stop_impersonating"))

        self.assertRedirects(response, reverse("settings"))
        self.assertEqual(self.client.session["_auth_user_id"], str(self.admin.pk))
        self.assertNotIn("impersonating_original_user_pk", self.client.session)
        self.assertNotIn("impersonating_original_hash", self.client.session)

        page = self.client.get(reverse("settings"))
        self.assertEqual(page.context["user"].pk, self.admin.pk)

    def test_rotating_the_admin_password_mid_session_forces_a_fresh_login(self):
        """impersonate_user stores the admin's session auth hash as it was at
        the time. After a password rotation that stored hash no longer matches,
        so restoring it hands Django a session it must reject.

        This is the safe outcome and the reason the hash is stored rather than
        recomputed: a credential rotation must not be survivable by resuming a
        suspended session. It is the opposite of what the code comment used to
        claim, which is why it is pinned here.
        """
        self.client.force_login(self.admin)
        self._start()

        self.admin.set_password("Impersonate-Pass-2-Rotated")
        self.admin.save()

        self.client.post(reverse("stop_impersonating"))
        page = self.client.get(reverse("settings"))

        self.assertEqual(page.status_code, 302)
        self.assertIn(reverse("login"), page["Location"])
        self.assertEqual(dict(self.client.session), {})

    def test_rotating_the_target_password_mid_session_ends_the_impersonation(self):
        """Django invalidates the session as soon as the impersonated user's
        auth hash changes, before stop_impersonating can run. The admin loses
        the restore key with it and has to log in again."""
        self.client.force_login(self.admin)
        self._start()

        self.target.set_password("Impersonate-Pass-3-Rotated")
        self.target.save()

        page = self.client.get(reverse("dashboard"))

        self.assertEqual(page.status_code, 302)
        self.assertNotIn("impersonating_original_user_pk", self.client.session)

    def test_stop_without_an_impersonation_is_a_safe_no_op(self):
        self.client.force_login(self.target)
        response = self.client.post(reverse("stop_impersonating"))

        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.target.pk))

    def test_stop_cannot_escalate_an_un_impersonated_session(self):
        """No session key means no switch — a plain user cannot ride this view
        into someone else's account."""
        self.client.force_login(self.target)
        self.client.post(reverse("stop_impersonating"))

        page = self.client.get(reverse("dashboard"), follow=True)
        self.assertEqual(page.context["user"].pk, self.target.pk)

    def test_stop_rejects_get(self):
        """A third-party page must not be able to end an impersonation."""
        self.client.force_login(self.admin)
        self._start()

        response = self.client.get(reverse("stop_impersonating"))

        self.assertEqual(response.status_code, 405)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.target.pk))

    def test_the_banner_offers_a_post_form_not_a_link(self):
        self.client.force_login(self.admin)
        self._start()

        page = self.client.get(reverse("dashboard"), follow=True)
        body = page.content.decode()
        self.assertIn('action="/settings/stop-impersonating/"', body)
        self.assertIn("csrfmiddlewaretoken", body)
