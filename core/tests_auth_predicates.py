"""_is_organizer, _is_captain and _get_active_team.

All three used a bare `except:` that returned False/None for *anything* that
went wrong. They failed closed, so this was never an access-control hole -- but
a database fault came back as "not an organizer", which would strip every
organizer of their tools with nothing in the logs saying why.

These pin both halves of the narrowing: the shapes that must still be denied
quietly, and the faults that must now surface.
"""
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, User
from django.db import DatabaseError
from django.test import TestCase

from core.models import (
    OrganizerProfile, Team, TeamMembership, UserTeamAssignment,
)
from core.views import _get_active_team, _is_captain, _is_organizer


class IsOrganizerTests(TestCase):
    def setUp(self):
        self.plain = User.objects.create_user("pred_plain", password="Predicate-Pass-1")

    def _verified(self, username):
        user = User.objects.create_user(username, password="Predicate-Pass-1")
        OrganizerProfile.objects.filter(user=user).update(verified=True)
        return User.objects.get(pk=user.pk)

    # --- still denied, quietly ---------------------------------------------

    def test_none_is_not_an_organizer(self):
        self.assertFalse(_is_organizer(None))

    def test_anonymous_is_not_an_organizer(self):
        self.assertFalse(_is_organizer(AnonymousUser()))

    def test_an_object_that_is_not_a_user_is_not_an_organizer(self):
        """The one case the bare except was really covering."""
        self.assertFalse(_is_organizer(object()))

    def test_a_user_without_a_profile_is_not_an_organizer(self):
        OrganizerProfile.objects.filter(user=self.plain).delete()
        self.assertFalse(_is_organizer(User.objects.get(pk=self.plain.pk)))

    def test_an_unverified_profile_is_not_an_organizer(self):
        OrganizerProfile.objects.filter(user=self.plain).update(verified=False)
        self.assertFalse(_is_organizer(User.objects.get(pk=self.plain.pk)))

    # --- still allowed ------------------------------------------------------

    def test_a_verified_profile_is_an_organizer(self):
        self.assertTrue(_is_organizer(self._verified("pred_org")))

    def test_staff_is_an_organizer(self):
        staff = User.objects.create_user(
            "pred_staff", password="Predicate-Pass-1", is_staff=True
        )
        self.assertTrue(_is_organizer(staff))

    def test_superuser_is_an_organizer(self):
        admin = User.objects.create_superuser(
            "pred_admin", "a@example.com", "Predicate-Pass-1"
        )
        self.assertTrue(_is_organizer(admin))

    # --- no longer swallowed ------------------------------------------------

    def test_a_database_failure_is_not_reported_as_not_an_organizer(self):
        """The point of the change. This used to return False."""
        fresh = self._verified("pred_dbfail")

        # The reverse one-to-one accessor loads the profile through
        # QuerySet.get, so that is what has to fail. The user is fetched before
        # the patch so only the profile lookup is affected.
        with patch(
            "django.db.models.query.QuerySet.get",
            side_effect=DatabaseError("connection lost"),
        ):
            # hasattr() swallows AttributeError only, so a DatabaseError raised
            # while loading the profile must escape _is_organizer.
            with self.assertRaises(DatabaseError):
                _is_organizer(fresh)


class IsCaptainTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("cap_user", password="Predicate-Pass-1")
        self.team = Team.objects.create(name="Cap Team")
        self.other = Team.objects.create(name="Other Team")

    def test_anonymous_is_not_a_captain(self):
        self.assertFalse(_is_captain(AnonymousUser(), self.team))

    def test_a_user_with_no_assignment_and_no_team_argument_is_not_a_captain(self):
        UserTeamAssignment.objects.filter(user=self.user).delete()
        self.assertFalse(_is_captain(User.objects.get(pk=self.user.pk)))

    def test_captain_of_the_named_team(self):
        TeamMembership.objects.create(team=self.team, user=self.user, role="captain")
        self.assertTrue(_is_captain(self.user, self.team))

    def test_member_is_not_captain(self):
        TeamMembership.objects.create(team=self.team, user=self.user, role="member")
        self.assertFalse(_is_captain(self.user, self.team))

    def test_captain_of_one_team_is_not_captain_of_another(self):
        TeamMembership.objects.create(team=self.team, user=self.user, role="captain")
        self.assertFalse(_is_captain(self.user, self.other))

    def test_falls_back_to_the_active_team(self):
        TeamMembership.objects.create(team=self.team, user=self.user, role="captain")
        UserTeamAssignment.objects.update_or_create(
            user=self.user, defaults={"active_team": self.team}
        )
        self.assertTrue(_is_captain(User.objects.get(pk=self.user.pk)))

    def test_a_database_failure_is_not_reported_as_not_a_captain(self):
        TeamMembership.objects.create(team=self.team, user=self.user, role="captain")
        with patch.object(
            TeamMembership.objects, "filter", side_effect=DatabaseError("connection lost")
        ):
            with self.assertRaises(DatabaseError):
                _is_captain(self.user, self.team)


class GetActiveTeamTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("act_user", password="Predicate-Pass-1")
        self.team = Team.objects.create(name="Active Team")

    def test_anonymous_has_no_active_team(self):
        self.assertIsNone(_get_active_team(AnonymousUser()))

    def test_no_assignment_row_means_no_active_team(self):
        UserTeamAssignment.objects.filter(user=self.user).delete()
        self.assertIsNone(_get_active_team(User.objects.get(pk=self.user.pk)))

    def test_an_assignment_with_no_team_means_no_active_team(self):
        UserTeamAssignment.objects.update_or_create(
            user=self.user, defaults={"active_team": None}
        )
        self.assertIsNone(_get_active_team(User.objects.get(pk=self.user.pk)))

    def test_returns_the_assigned_team(self):
        UserTeamAssignment.objects.update_or_create(
            user=self.user, defaults={"active_team": self.team}
        )
        self.assertEqual(_get_active_team(User.objects.get(pk=self.user.pk)), self.team)


class NoBareExceptsTests(TestCase):
    """A bare `except:` in the request path hides the next bug like this one."""

    def test_core_has_no_bare_except(self):
        from pathlib import Path
        import re

        core = Path(__file__).resolve().parent
        offenders = []
        for path in sorted(core.rglob("*.py")):
            if "migrations" in path.parts or path.name.startswith("test"):
                continue
            for index, line in enumerate(path.read_text().splitlines(), start=1):
                if re.match(r"\s*except\s*:", line):
                    offenders.append(f"{path.relative_to(core)}:{index}")

        self.assertEqual(offenders, [], f"bare except at {offenders}")

    def test_the_scan_actually_reads_the_view_modules(self):
        """Guard against the scan silently covering nothing, which is how it
        would rot after core/views.py became core/views/."""
        from pathlib import Path

        views_pkg = Path(__file__).resolve().parent / "views"
        self.assertTrue(views_pkg.is_dir())
        modules = {p.name for p in views_pkg.glob("*.py")}
        self.assertIn("helpers.py", modules)
        self.assertGreaterEqual(len(modules), 8)
