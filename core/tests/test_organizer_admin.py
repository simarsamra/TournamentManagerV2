"""Organizer/user-account management, the tournament switcher, and public/profile pages.

Split out of the old core/tests.py (UXAndLogicRegressionTests) — see
FOLLOWUP_PLAN.md F-3.
"""
from django.contrib.auth.models import User
from django.urls import reverse
from ..models import Tournament
from ..forms import TournamentForm

from .helpers import UXRegressionTestCase, _captain_user


class OrganizerAdminAndAccountTests(UXRegressionTestCase):

    def test_audit_log_invalid_page_query_does_not_crash(self):
        self._create_tournament()
        self.client.force_login(self.organizer)

        response = self.client.get(reverse("audit_log"), {"page": "bad"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("logs", response.context)

    def test_organizer_can_delete_tournament(self):
        tournament = self._create_tournament(name="Delete Me")
        self.client.force_login(self.organizer)

        response = self.client.post(
            reverse("delete_tournament", kwargs={"pk": tournament.pk}),
            {"confirm_delete": "DELETE"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Tournament.objects.filter(pk=tournament.pk).exists())
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("deleted" in m.lower() for m in msgs))

    def test_non_organizer_cannot_delete_tournament(self):
        tournament = self._create_tournament(name="Keep Me")
        team = self._create_team(tournament, "Falcons")
        self.client.force_login(_captain_user(team))

        response = self.client.post(
            reverse("delete_tournament", kwargs={"pk": tournament.pk}),
            {"confirm_delete": "DELETE"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Tournament.objects.filter(pk=tournament.pk).exists())

    def test_organizer_can_promote_user_to_organizer(self):
        user = User.objects.create_user(username="regular_user", password="pass123")
        self.client.force_login(self.organizer)

        response = self.client.post(
            reverse("set_user_organizer", kwargs={"user_pk": user.pk}),
            {"is_organizer": "1"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.organizer_profile.verified)

    def test_non_organizer_cannot_promote_user_to_organizer(self):
        tournament = self._create_tournament(name="Role Guard")
        team = self._create_team(tournament, "Falcons")
        target = User.objects.create_user(username="target_regular", password="pass123")
        self.client.force_login(_captain_user(team))

        response = self.client.post(
            reverse("set_user_organizer", kwargs={"user_pk": target.pk}),
            {"is_organizer": "1"},
        )

        self.assertEqual(response.status_code, 302)
        target.refresh_from_db()
        self.assertFalse(target.organizer_profile.verified)

    def test_organizer_can_demote_another_organizer_if_one_remains(self):
        other_organizer = User.objects.create_user(
            username="other_organizer", password="pass123", is_staff=True
        )
        self.client.force_login(self.organizer)

        response = self.client.post(
            reverse("set_user_organizer", kwargs={"user_pk": other_organizer.pk}),
            {"is_organizer": "0"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        other_organizer.refresh_from_db()
        self.assertFalse(other_organizer.organizer_profile.verified)

    def test_cannot_demote_last_organizer(self):
        self.client.force_login(self.organizer)

        response = self.client.post(
            reverse("set_user_organizer", kwargs={"user_pk": self.organizer.pk}),
            {"is_organizer": "0"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.organizer.refresh_from_db()
        self.assertTrue(self.organizer.organizer_profile.verified)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("at least one organizer" in m.lower() for m in msgs))

    def test_set_user_organizer_rejects_invalid_role_value(self):
        target = User.objects.create_user(
            username="invalid_role_target", password="pass123", is_staff=True
        )
        self.client.force_login(self.organizer)

        response = self.client.post(
            reverse("set_user_organizer", kwargs={"user_pk": target.pk}),
            {},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        target.refresh_from_db()
        self.assertTrue(target.organizer_profile.verified)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("invalid organizer role update request" in m.lower() for m in msgs))

    def test_organizer_can_delete_user_account(self):
        target = User.objects.create_user(username="delete_me", password="pass123")
        self.client.force_login(self.organizer)

        response = self.client.post(
            reverse("delete_user_account", kwargs={"user_pk": target.pk}),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(pk=target.pk).exists())

    def test_organizer_cannot_delete_own_account(self):
        self.client.force_login(self.organizer)

        response = self.client.post(
            reverse("delete_user_account", kwargs={"user_pk": self.organizer.pk}),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(User.objects.filter(pk=self.organizer.pk).exists())
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("cannot delete your own account" in m.lower() for m in msgs))

    def test_dashboard_shows_multiple_tournaments_to_organizer(self):
        first = self._create_tournament(name="Spring Cup")
        second = self._create_tournament(name="Summer Cup")
        self._create_team(first, "Alpha")
        self._create_team(second, "Beta")
        self.client.force_login(self.organizer)

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("all_tournaments", response.context)
        self.assertEqual(response.context["all_tournaments"].count(), 2)
        self.assertContains(response, "Spring Cup")
        self.assertContains(response, "Summer Cup")

    def test_organizer_can_switch_selected_tournament_across_pages(self):
        first = self._create_tournament(name="Spring Cup")
        second = self._create_tournament(name="Summer Cup")
        self._create_team(first, "Alpha")
        self._create_team(second, "Beta")
        self.client.force_login(self.organizer)

        select_response = self.client.post(
            reverse("select_tournament"),
            {"tournament_id": first.pk},
            follow=True,
        )
        teams_response = self.client.get(reverse("teams"))

        self.assertEqual(select_response.status_code, 200)
        self.assertEqual(self.client.session.get("selected_tournament_id"), first.pk)
        self.assertEqual(teams_response.context["tournament"].pk, first.pk)
        self.assertContains(teams_response, "Alpha")
        self.assertNotContains(teams_response, "Beta")

    def test_home_route_is_public_for_anonymous_users(self):
        tournament = self._create_tournament(name="Public Main")
        tournament.status = "active"
        tournament.save(update_fields=["status"])

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tournament Manager")
        self.assertContains(response, "Login")
        self.assertEqual(response.context.get("tournament"), tournament)

    def test_public_views_support_tournament_query_selection(self):
        self._create_tournament(name="Public A")
        second = self._create_tournament(name="Public B")

        response = self.client.get(reverse("public_standings"), {"tournament": second.pk})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["tournament"], second)
        self.assertEqual(self.client.session.get("selected_tournament_id"), second.pk)

        fixtures_response = self.client.get(reverse("public_fixtures"))
        self.assertEqual(fixtures_response.status_code, 200)
        self.assertEqual(fixtures_response.context["tournament"], second)

    def test_user_can_update_own_profile_fields(self):
        user = User.objects.create_user(
            username="profile_user",
            password="pass123",
            first_name="Old",
            last_name="Name",
            email="old@example.com",
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("profile"),
            {
                "action": "update_profile",
                "first_name": "New",
                "last_name": "User",
                "email": "new@example.com",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.first_name, "New")
        self.assertEqual(user.last_name, "User")
        self.assertEqual(user.email, "new@example.com")

    def test_user_can_change_own_password(self):
        user = User.objects.create_user(username="pw_user", password="pass123")
        self.client.force_login(user)

        response = self.client.post(
            reverse("profile"),
            {
                "action": "change_password",
                "current_password": "pass123",
                "new_password": "pass12345",
                "confirm_password": "pass12345",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.check_password("pass12345"))

        self.client.logout()
        login_ok = self.client.login(username="pw_user", password="pass12345")
        self.assertTrue(login_ok)

    def test_tournament_form_saves_start_date_and_expected_teams(self):
        form = TournamentForm(data={
            "name": "Planned Event",
            "format": "round_robin",
            "sport_type": "table_tennis",
            "players_per_team": 2,
            "points_per_win": 3,
            "points_per_loss": 0,
            "points_per_draw": 1,
            "num_groups": 2,
            "teams_per_group_advance": 1,
            "withdrawal_policy": "forfeit",
            "default_match_duration": 35,
            "start_date": "2026-05-01",
            "expected_teams_count": 4,
        })

        self.assertTrue(form.is_valid(), form.errors)
        tournament = form.save()
        self.assertEqual(str(tournament.start_date), "2026-05-01")
        self.assertEqual(tournament.expected_teams_count, 4)
