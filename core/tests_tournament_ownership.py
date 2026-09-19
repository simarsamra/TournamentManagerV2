"""Tournament ownership and the organizer / site-admin split.

Tournament had no owner field, so every organizer-gated view authorized with
_is_organizer alone: any verified organizer could configure, start, delete or
disqualify from any other organizer's tournament. Account management sat behind
the same organizer-level check.
"""
from django.contrib.auth.models import User
from django.test import TestCase

from core.models import OrganizerProfile, Tournament
from core.views import _can_manage_tournament, _is_site_admin


def make_organizer(username):
    user = User.objects.create_user(username=username, password="Regression-Pass-1")
    OrganizerProfile.objects.filter(user=user).update(verified=True)
    user.refresh_from_db()
    return user


class OwnershipHelperTests(TestCase):
    def setUp(self):
        self.owner = make_organizer("owner")
        self.other = make_organizer("other")
        self.admin = User.objects.create_superuser(
            username="root", password="Regression-Pass-1", email=""
        )
        self.plain = User.objects.create_user(
            username="plain", password="Regression-Pass-1"
        )
        self.tournament = Tournament.objects.create(
            name="Owned", format="round_robin", players_per_team=1,
            created_by=self.owner,
        )

    def test_owner_can_manage(self):
        self.assertTrue(_can_manage_tournament(self.owner, self.tournament))

    def test_other_organizer_cannot_manage(self):
        self.assertFalse(_can_manage_tournament(self.other, self.tournament))

    def test_site_admin_can_manage_anything(self):
        self.assertTrue(_can_manage_tournament(self.admin, self.tournament))
        self.assertTrue(_is_site_admin(self.admin))

    def test_non_organizer_cannot_manage(self):
        self.assertFalse(_can_manage_tournament(self.plain, self.tournament))

    def test_legacy_tournament_is_not_orphaned(self):
        legacy = Tournament.objects.create(
            name="Legacy", format="round_robin", players_per_team=1, created_by=None
        )
        self.assertTrue(_can_manage_tournament(self.other, legacy))


class OwnershipEnforcementTests(TestCase):
    def setUp(self):
        self.owner = make_organizer("owner")
        self.other = make_organizer("other")
        self.admin = User.objects.create_superuser(
            username="root", password="Regression-Pass-1", email=""
        )
        self.tournament = Tournament.objects.create(
            name="Owned", format="round_robin", status="setup",
            players_per_team=1, created_by=self.owner,
        )

    def test_owner_can_open_config(self):
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.get(f"/tournament/{self.tournament.pk}/config/").status_code, 200
        )

    def test_other_organizer_is_redirected_from_config(self):
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.get(f"/tournament/{self.tournament.pk}/config/").status_code, 302
        )

    def test_other_organizer_cannot_delete(self):
        self.client.force_login(self.other)
        self.client.post(
            f"/tournament/{self.tournament.pk}/delete/", {"confirm_delete": "DELETE"}
        )
        self.assertTrue(Tournament.objects.filter(pk=self.tournament.pk).exists())

    def test_owner_can_delete(self):
        self.client.force_login(self.owner)
        self.client.post(
            f"/tournament/{self.tournament.pk}/delete/", {"confirm_delete": "DELETE"}
        )
        self.assertFalse(Tournament.objects.filter(pk=self.tournament.pk).exists())

    def test_site_admin_can_delete_anyones(self):
        self.client.force_login(self.admin)
        self.client.post(
            f"/tournament/{self.tournament.pk}/delete/", {"confirm_delete": "DELETE"}
        )
        self.assertFalse(Tournament.objects.filter(pk=self.tournament.pk).exists())

    def test_other_organizer_cannot_cancel(self):
        self.tournament.status = "active"
        self.tournament.save(update_fields=["status"])
        self.client.force_login(self.other)
        self.client.post(
            f"/tournament/{self.tournament.pk}/cancel/", {"confirm_cancel": "CANCEL"}
        )
        self.tournament.refresh_from_db()
        self.assertEqual(self.tournament.status, "active")

    def test_other_organizer_cannot_open_registration(self):
        self.client.force_login(self.other)
        self.client.post(f"/tournament/{self.tournament.pk}/open-registration/")
        self.tournament.refresh_from_db()
        self.assertEqual(self.tournament.status, "setup")

    def test_json_estimate_endpoint_returns_403_for_non_owner(self):
        self.client.force_login(self.other)
        response = self.client.get(
            f"/tournament/{self.tournament.pk}/estimate-end-date/"
        )
        self.assertEqual(response.status_code, 403)

    def test_creating_a_tournament_records_the_creator(self):
        self.client.force_login(self.owner)
        self.client.post(
            "/tournament/setup/",
            {
                "name": "Brand New", "sport_type": "table_tennis", "format": "round_robin",
                "players_per_team": "1", "expected_teams_count": "0",
                "points_per_win": "3", "points_per_loss": "0", "points_per_draw": "1",
                "num_groups": "2", "teams_per_group_advance": "2",
                "withdrawal_policy": "forfeit", "default_match_duration": "30",
                "matches_per_court_per_day": "0",
            },
            follow=True,
        )
        created = Tournament.objects.filter(name="Brand New").first()
        self.assertIsNotNone(created)
        self.assertEqual(created.created_by, self.owner)

    def test_switcher_lists_only_manageable_tournaments(self):
        other_owned = Tournament.objects.create(
            name="TheirOwn", format="round_robin", players_per_team=1,
            created_by=self.other,
        )
        self.client.force_login(self.owner)
        response = self.client.get("/dashboard/")
        listed = {t.pk for t in response.context["available_tournaments"]}
        self.assertIn(self.tournament.pk, listed)
        self.assertNotIn(other_owned.pk, listed)


class AdminPowerSeparationTests(TestCase):
    """set_user_organizer, delete_user_account, toggle_user_suspension and
    review_organizer_application sat behind _is_organizer, so any organizer
    could promote, suspend or delete any other."""

    def setUp(self):
        self.organizer = make_organizer("org")
        self.admin = User.objects.create_superuser(
            username="root", password="Regression-Pass-1", email=""
        )
        self.victim = User.objects.create_user(
            username="victim", password="Regression-Pass-1"
        )

    def test_organizer_cannot_promote_users(self):
        self.client.force_login(self.organizer)
        self.client.post(
            f"/settings/users/{self.victim.pk}/organizer/", {"is_organizer": "1"}
        )
        self.victim.organizer_profile.refresh_from_db()
        self.assertFalse(self.victim.organizer_profile.verified)

    def test_organizer_cannot_delete_accounts(self):
        self.client.force_login(self.organizer)
        self.client.post(f"/settings/users/{self.victim.pk}/delete/")
        self.assertTrue(User.objects.filter(pk=self.victim.pk).exists())

    def test_organizer_cannot_suspend_accounts(self):
        self.client.force_login(self.organizer)
        self.client.post(f"/settings/users/{self.victim.pk}/suspend/")
        self.victim.refresh_from_db()
        self.assertTrue(self.victim.is_active)

    def test_admin_can_promote(self):
        self.client.force_login(self.admin)
        self.client.post(
            f"/settings/users/{self.victim.pk}/organizer/", {"is_organizer": "1"}
        )
        self.victim.organizer_profile.refresh_from_db()
        self.assertTrue(self.victim.organizer_profile.verified)

    def test_organizer_does_not_receive_the_user_list(self):
        tournament = Tournament.objects.create(
            name="S", format="round_robin", players_per_team=1,
            created_by=self.organizer,
        )
        self.client.force_login(self.organizer)
        session = self.client.session
        session["selected_tournament_id"] = tournament.pk
        session.save()
        response = self.client.get("/settings/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["users"]), [])
        self.assertFalse(response.context["is_site_admin"])
