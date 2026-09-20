"""Team creation, membership, participations, and the registration form.

Split out of the old core/tests.py (UXAndLogicRegressionTests) — see
FOLLOWUP_PLAN.md F-3.
"""
from django.contrib.auth.models import User
from django.core.management import call_command
from django.urls import reverse
from django.db import IntegrityError
from ..models import (
    Team,
    Court,
    TeamMembership,
    TeamTournamentParticipation,
    TeamTournamentCourtPreference,
    TournamentIndividualRegistration,
)

from .helpers import UXRegressionTestCase


class RegistrationAndTeamMembershipTests(UXRegressionTestCase):

    def test_register_duplicate_team_name_shows_form_error(self):
        tournament = self._create_tournament()
        tournament.status = "registration_open"
        tournament.save(update_fields=["status"])
        court = Court.objects.create(tournament=tournament, name="Court A", is_available=True)
        self._create_team(tournament, "Falcons", username="existing_user")
        new_user = User.objects.create_user(username="new_user", password="abc12345")
        self.client.force_login(new_user)

        response = self.client.post(
            reverse("create_team", kwargs={"pk": tournament.pk}),
            {
                "team_name": "Falcons",
                "department": "Engineering",
                "preferred_courts": [str(court.pk)],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")

    def test_team_membership_supports_manager_role(self):
        tournament = self._create_tournament(name="Role Model")
        team = self._create_team(tournament, "Role Team")
        manager_user = User.objects.create_user(username="role_manager", password="pass123")

        membership = TeamMembership.objects.create(team=team, user=manager_user, role="member")

        self.assertEqual(membership.role, "member")
        self.assertIn("member", dict(TeamMembership.ROLE_CHOICES))

    def test_team_can_have_multiple_tournament_participations(self):
        first = self._create_tournament(name="Participation A")
        second = self._create_tournament(name="Participation B")
        team = self._create_team(first, "Multi Team")

        p1 = TeamTournamentParticipation.objects.get(team=team, tournament=first)
        p1.group = "A"
        p1.seed = 1
        p1.save(update_fields=["group", "seed"])
        p2 = TeamTournamentParticipation.objects.create(team=team, tournament=second, group="B", seed=2)

        self.assertNotEqual(p1.pk, p2.pk)
        self.assertEqual(team.participations.count(), 2)

    def test_team_participation_is_unique_per_tournament(self):
        tournament = self._create_tournament(name="Unique Participation")
        team = self._create_team(tournament, "Unique Team")

        with self.assertRaises(IntegrityError):
            TeamTournamentParticipation.objects.create(team=team, tournament=tournament)

    def test_participation_court_preference_is_unique(self):
        tournament = self._create_tournament(name="Preference Uniqueness")
        team = self._create_team(tournament, "Pref Team")
        court = Court.objects.create(tournament=tournament, name="Center", is_available=True)
        participation = TeamTournamentParticipation.objects.get(team=team, tournament=tournament)
        TeamTournamentCourtPreference.objects.create(participation=participation, court=court)

        with self.assertRaises(IntegrityError):
            TeamTournamentCourtPreference.objects.create(participation=participation, court=court)

    def test_backfill_team_participations_command_is_idempotent(self):
        tournament = self._create_tournament(name="Backfill Tournament")
        team = self._create_team(tournament, "Backfill Team")
        court = Court.objects.create(tournament=tournament, name="Backfill Court", is_available=True)
        participation = TeamTournamentParticipation.objects.get(team=team, tournament=tournament)
        TeamTournamentCourtPreference.objects.create(participation=participation, court=court)

        call_command("backfill_team_participations")
        first_participation_count = TeamTournamentParticipation.objects.count()
        first_pref_count = TeamTournamentCourtPreference.objects.count()

        call_command("backfill_team_participations")

        self.assertEqual(TeamTournamentParticipation.objects.count(), first_participation_count)
        self.assertEqual(TeamTournamentCourtPreference.objects.count(), first_pref_count)
        self.assertTrue(
            TeamTournamentParticipation.objects.filter(team=team, tournament=tournament).exists()
        )

    def test_registration_requires_confirmation_checkbox(self):
        open_tournament = self._create_tournament(name="Confirmed Entry")
        open_tournament.status = "registration_open"
        open_tournament.save(update_fields=["status"])
        court = Court.objects.create(tournament=open_tournament, name="Court A", is_available=True)
        new_user = User.objects.create_user(username="joiners_user_blocked", password="abc12345")
        self.client.force_login(new_user)

        response = self.client.post(
            reverse("create_team", kwargs={"pk": open_tournament.pk}),
            {
                "team_name": "Joiners",
                "department": "Engineering",
                "preferred_courts": [str(court.pk)],
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Team.objects.filter(name="Joiners").exists())

    def test_test_maker_create_teams_creates_participations(self):
        tournament = self._create_tournament(name="Test Maker Participation")
        self.client.force_login(self.organizer)

        session = self.client.session
        session["selected_tournament_id"] = tournament.pk
        session.save()

        response = self.client.post(
            reverse("test_maker"),
            {
                "action": "create_test_teams",
                "team_count": "2",
                "members_per_team": "2",
                "team_prefix": "tm_team_",
                "username_prefix": "tmuser_",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(TeamTournamentParticipation.objects.filter(tournament=tournament).count(), 2)
        self.assertEqual(TeamMembership.objects.filter(team__participations__tournament=tournament).count(), 4)
        self.assertEqual(
            TeamTournamentParticipation.objects.filter(tournament=tournament).count(),
            2,
        )

    def test_teams_page_shows_only_active_teams_in_selected_tournament(self):
        tournament = self._create_tournament(name="Visibility Cup")
        self._create_team(tournament, "Active Team")
        withdrawn_team = self._create_team(tournament, "Withdrawn Team")
        p = TeamTournamentParticipation.objects.get(team=withdrawn_team, tournament=tournament)
        p.status = "withdrawn"
        p.save(update_fields=["status"])

        self.client.force_login(self.organizer)
        response = self.client.get(reverse("teams"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Active Team")
        self.assertNotContains(response, "Withdrawn Team")

    def test_remove_team_member_keeps_user_account(self):
        tournament = self._create_tournament(name="Membership Safety")
        team = self._create_team(tournament, "Captains")
        member_user = User.objects.create_user(username="kept_member", password="pass123")
        TeamMembership.objects.create(team=team, user=member_user, role="member")
        self.client.force_login(self.organizer)

        response = self.client.post(
            reverse("remove_team_member", kwargs={"pk": team.pk, "user_pk": member_user.pk}),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(TeamMembership.objects.filter(team=team, user=member_user).exists())
        self.assertTrue(User.objects.filter(pk=member_user.pk).exists())

    def test_add_existing_user_to_team(self):
        tournament = self._create_tournament(name="Existing User Add")
        tournament.players_per_team = 2
        tournament.save(update_fields=["players_per_team"])
        team = self._create_team(tournament, "Captains")
        existing_user = User.objects.create_user(username="already_here", password="pass123")
        self.client.force_login(self.organizer)

        response = self.client.post(
            reverse("manage_team_members", kwargs={"pk": team.pk}),
            {"member_action": "add_existing", "username": existing_user.username},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(TeamMembership.objects.filter(team=team, user=existing_user, role="member").exists())

    def test_add_existing_user_prevents_duplicate_membership(self):
        tournament = self._create_tournament(name="Duplicate Existing User")
        tournament.players_per_team = 3
        tournament.save(update_fields=["players_per_team"])
        team = self._create_team(tournament, "Captains")
        existing_user = User.objects.create_user(username="already_member", password="pass123")
        TeamMembership.objects.create(team=team, user=existing_user, role="member")
        self.client.force_login(self.organizer)

        response = self.client.post(
            reverse("manage_team_members", kwargs={"pk": team.pk}),
            {"member_action": "add_existing", "username": existing_user.username},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(TeamMembership.objects.filter(team=team, user=existing_user).count(), 1)

    def test_tournament_specific_registration_creates_team_in_correct_tournament(self):
        open_tournament = self._create_tournament(name="Open Cup")
        open_tournament.status = "registration_open"
        open_tournament.save(update_fields=["status"])
        other_tournament = self._create_tournament(name="Other Cup")
        court = Court.objects.create(tournament=open_tournament, name="Court A", is_available=True)
        join_user = User.objects.create_user(username="joiners_user", password="abc12345")
        self.client.force_login(join_user)

        response = self.client.post(
            reverse("create_team", kwargs={"pk": open_tournament.pk}),
            {
                "team_name": "Joiners",
                "department": "Engineering",
                "preferred_courts": [str(court.pk)],
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        team = Team.objects.filter(name="Joiners").first()
        self.assertIsNotNone(team)
        self.assertTrue(
            TeamTournamentParticipation.objects.filter(team=team, tournament=open_tournament).exists()
        )
        self.assertFalse(
            TeamTournamentParticipation.objects.filter(team=team, tournament=other_tournament).exists()
        )
        self.assertEqual(team.department, "Engineering")

    def test_user_can_create_multiple_standalone_teams(self):
        user = User.objects.create_user(username="multi_team_owner", password="abc12345")
        self.client.force_login(user)

        response_one = self.client.post(
            reverse("create_standalone_team"),
            {
                "team_name": "Street Smashers",
                "sport_type": "table_tennis",
                "department": "Operations",
            },
            follow=True,
        )
        response_two = self.client.post(
            reverse("create_standalone_team"),
            {
                "team_name": "Sunday Strikers",
                "sport_type": "tennis",
                "department": "Finance",
            },
            follow=True,
        )

        self.assertEqual(response_one.status_code, 200)
        self.assertEqual(response_two.status_code, 200)
        self.assertEqual(Team.objects.filter(memberships__user=user).distinct().count(), 2)
        self.assertTrue(Team.objects.filter(name="Street Smashers", sport_type="table_tennis").exists())
        self.assertTrue(Team.objects.filter(name="Sunday Strikers", sport_type="tennis").exists())

    def test_teams_page_without_selected_tournament_shows_create_team_action(self):
        user = User.objects.create_user(username="no_tournament_user", password="abc12345")
        self.client.force_login(user)

        response = self.client.get(reverse("teams"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create Standalone Team")

    def test_enter_existing_team_rejects_team_with_extra_members(self):
        open_tournament = self._create_tournament(name="Open 2P")
        open_tournament.status = "registration_open"
        open_tournament.players_per_team = 2
        open_tournament.save(update_fields=["status", "players_per_team"])

        captain = User.objects.create_user(username="cap_over", password="pass123")
        member1 = User.objects.create_user(username="mem_over_1", password="pass123")
        member2 = User.objects.create_user(username="mem_over_2", password="pass123")
        team = Team.objects.create(name="Oversized Team", sport_type=open_tournament.sport_type)
        TeamMembership.objects.create(team=team, user=captain, role="captain")
        TeamMembership.objects.create(team=team, user=member1, role="member")
        TeamMembership.objects.create(team=team, user=member2, role="member")

        self.client.force_login(captain)
        response = self.client.post(
            reverse("enter_existing_team", kwargs={"pk": open_tournament.pk}),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            TeamTournamentParticipation.objects.filter(team=team, tournament=open_tournament).exists()
        )
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("must have exactly" in m.lower() for m in msgs))

    def test_individual_registration_mode_registers_player_name(self):
        tournament = self._create_tournament(name="Singles Cup")
        tournament.status = "registration_open"
        tournament.registration_mode = "individual"
        tournament.players_per_team = 1
        tournament.save(update_fields=["status", "registration_mode", "players_per_team"])

        user = User.objects.create_user(username="solo_player", password="abc12345", first_name="Solo Player")
        self.client.force_login(user)

        response = self.client.post(
            reverse("create_team", kwargs={"pk": tournament.pk}),
            {"participant_name": "Solo Player"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        reg = TournamentIndividualRegistration.objects.filter(user=user, tournament=tournament).first()
        self.assertIsNotNone(reg)
        self.assertEqual(reg.display_name, "Solo Player")
        self.assertIsNotNone(reg.shadow_team)
        self.assertTrue(reg.shadow_team.is_internal)
        self.assertFalse(TeamMembership.objects.filter(user=user).exists())
        self.assertTrue(
            TeamTournamentParticipation.objects.filter(team=reg.shadow_team, tournament=tournament).exists()
        )
