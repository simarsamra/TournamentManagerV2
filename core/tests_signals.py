"""Guards for the User post_save handlers in core/signals.py."""
from django.contrib.auth.models import User
from django.test import TestCase

from core.models import OrganizerProfile, UserTeamAssignment
from core.views import _is_organizer


class OrganizerProfileSignalTests(TestCase):
    def test_profile_and_assignment_are_created_for_new_users(self):
        user = User.objects.create_user(username="fresh", password="Regression-Pass-1")
        self.assertTrue(OrganizerProfile.objects.filter(user=user).exists())
        self.assertTrue(UserTeamAssignment.objects.filter(user=user).exists())

    def test_staff_users_are_seeded_verified(self):
        user = User.objects.create_user(
            username="staffer", password="Regression-Pass-1", is_staff=True
        )
        self.assertTrue(user.organizer_profile.verified)

    def test_verified_grant_survives_login(self):
        """A grant must not be revoked by update_last_login's User.save()."""
        user = User.objects.create_user(username="orgy", password="Regression-Pass-1")
        OrganizerProfile.objects.filter(user=user).update(verified=True)

        self.assertTrue(self.client.login(username="orgy", password="Regression-Pass-1"))

        user.refresh_from_db()
        user.organizer_profile.refresh_from_db()
        self.assertTrue(user.organizer_profile.verified)
        self.assertTrue(_is_organizer(user))

    def test_verified_grant_survives_profile_update(self):
        user = User.objects.create_user(username="orgy2", password="Regression-Pass-1")
        OrganizerProfile.objects.filter(user=user).update(verified=True)

        user.first_name = "Renamed"
        user.save(update_fields=["first_name"])

        user.organizer_profile.refresh_from_db()
        self.assertTrue(user.organizer_profile.verified)

    def test_saving_a_user_without_a_profile_does_not_raise(self):
        user = User.objects.create_user(username="orphan", password="Regression-Pass-1")
        OrganizerProfile.objects.filter(user=user).delete()
        user.first_name = "Still fine"
        user.save(update_fields=["first_name"])   # must not raise DoesNotExist

    def test_suppression_context_manager_restores_handlers(self):
        from core.signals import suppress_user_autocreate

        with suppress_user_autocreate():
            quiet = User.objects.create_user(
                username="quiet", password="Regression-Pass-1"
            )
            self.assertFalse(OrganizerProfile.objects.filter(user=quiet).exists())

        loud = User.objects.create_user(username="loud", password="Regression-Pass-1")
        self.assertTrue(OrganizerProfile.objects.filter(user=loud).exists())
