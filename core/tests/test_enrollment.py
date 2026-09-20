"""Regression coverage for the enrollment/capacity refactor.

Split out of the old core/tests.py — see FOLLOWUP_PLAN.md F-3.
"""
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from io import StringIO
from ..models import (
    Team,
    Tournament,
    TeamMembership,
    TeamTournamentParticipation,
    TournamentIndividualRegistration,
    TeamRegistration,
    IndividualRegistration,
    OrganizerProfile,
)
from ..services.enrollment import active_participant_count
from ..services.enrollment import is_registration_capacity_reached


class EnrollmentRefactorRegressionTests(TestCase):
    def setUp(self):
        self.organizer = User.objects.create_user(
            username="phase2_org", password="pass123", is_staff=True
        )
        OrganizerProfile.objects.update_or_create(
            user=self.organizer,
            defaults={"verified": True, "org_name": "QA"},
        )

    def _mk_tournament(self, name, mode="team"):
        return Tournament.objects.create(
            name=name,
            format="round_robin",
            sport_type="table_tennis",
            registration_mode=mode,
            status="registration_open",
            expected_teams_count=2,
        )

    def test_enrollment_service_count_and_capacity_for_both_modes(self):
        team_tournament = self._mk_tournament("Svc Team", mode="team")
        team = Team.objects.create(name="Svc Team A", sport_type=team_tournament.sport_type)
        TeamTournamentParticipation.objects.create(
            team=team,
            tournament=team_tournament,
            status="active",
        )

        self.assertEqual(active_participant_count(team_tournament), 1)
        self.assertFalse(is_registration_capacity_reached(team_tournament))

        individual_tournament = self._mk_tournament("Svc Individual", mode="individual")
        u1 = User.objects.create_user(username="svc_i1", password="pass123")
        u2 = User.objects.create_user(username="svc_i2", password="pass123")
        TournamentIndividualRegistration.objects.create(
            tournament=individual_tournament,
            user=u1,
            display_name="P1",
            status="active",
        )
        TournamentIndividualRegistration.objects.create(
            tournament=individual_tournament,
            user=u2,
            display_name="P2",
            status="active",
        )

        self.assertEqual(active_participant_count(individual_tournament), 2)
        self.assertTrue(is_registration_capacity_reached(individual_tournament))

    def test_test_maker_register_open_tournament_avoids_legacy_registration_models(self):
        team_tournament = self._mk_tournament("Legacy Team Flow", mode="team")
        individual_tournament = self._mk_tournament("Legacy Individual Flow", mode="individual")

        self.client.force_login(self.organizer)

        session = self.client.session
        session["selected_tournament_id"] = individual_tournament.pk
        session.save()

        response = self.client.post(
            reverse("test_maker"),
            {
                "action": "register_to_open_tournament",
                "reg_count": "2",
                "reg_prefix": "ind",
                "reg_username_prefix": "ind_u",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TournamentIndividualRegistration.objects.filter(tournament=individual_tournament).count(), 2)
        self.assertEqual(IndividualRegistration.objects.filter(tournament=individual_tournament).count(), 0)

        session = self.client.session
        session["selected_tournament_id"] = team_tournament.pk
        session.save()

        response = self.client.post(
            reverse("test_maker"),
            {
                "action": "register_to_open_tournament",
                "reg_count": "2",
                "reg_prefix": "team",
                "reg_username_prefix": "team_u",
                "reg_members_per_team": "1",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TeamTournamentParticipation.objects.filter(tournament=team_tournament).count(), 2)
        self.assertEqual(TeamRegistration.objects.filter(tournament=team_tournament).count(), 0)

    def test_test_maker_create_user_team_pool_reuses_existing_users(self):
        tournament = self._mk_tournament("Pool Tournament", mode="team")
        User.objects.create_user(username="pool_u_001", password="pass123")

        self.client.force_login(self.organizer)
        session = self.client.session
        session["selected_tournament_id"] = tournament.pk
        session.save()

        response = self.client.post(
            reverse("test_maker"),
            {
                "action": "create_user_team_pool",
                "pool_user_count": "4",
                "pool_team_count": "2",
                "pool_members_per_team": "2",
                "pool_user_prefix": "pool_u_",
                "pool_team_prefix": "pool_t_",
                "pool_password": "pass123",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.filter(username__startswith="pool_u_").count(), 4)
        self.assertEqual(Team.objects.filter(name__startswith="pool_t_").count(), 2)
        self.assertEqual(TeamMembership.objects.filter(team__name__startswith="pool_t_").count(), 4)

    def test_test_maker_register_existing_to_open_tournament_uses_existing_rows(self):
        team_tournament = self._mk_tournament("Existing Team Flow", mode="team")
        individual_tournament = self._mk_tournament("Existing Individual Flow", mode="individual")
        team_a = Team.objects.create(name="Existing A", sport_type=team_tournament.sport_type)
        team_b = Team.objects.create(name="Existing B", sport_type=team_tournament.sport_type)
        captain_a = User.objects.create_user(username="z_capt_a", password="pass123")
        captain_b = User.objects.create_user(username="z_capt_b", password="pass123")
        TeamMembership.objects.create(team=team_a, user=captain_a, role="captain")
        TeamMembership.objects.create(team=team_b, user=captain_b, role="captain")

        # Test Maker only sweeps up accounts it created (settings.TEST_MAKER_USER_PREFIX).
        # It used to register ANY existing user into a tournament without their
        # involvement; these fixtures previously had unprefixed names and this test
        # asserted that behaviour. See T-5.2.
        u1 = User.objects.create_user(username="tm_existing_i1", password="pass123", first_name="Existing One")
        u2 = User.objects.create_user(username="tm_existing_i2", password="pass123", first_name="Existing Two")
        bystander = User.objects.create_user(username="real_person", password="pass123", first_name="Real Person")

        self.client.force_login(self.organizer)

        session = self.client.session
        session["selected_tournament_id"] = team_tournament.pk
        session.save()

        response = self.client.post(
            reverse("test_maker"),
            {
                "action": "register_existing_to_open_tournament",
                "existing_count": "2",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TeamTournamentParticipation.objects.filter(tournament=team_tournament).count(), 2)

        session = self.client.session
        session["selected_tournament_id"] = individual_tournament.pk
        session.save()

        response = self.client.post(
            reverse("test_maker"),
            {
                "action": "register_existing_to_open_tournament",
                "existing_count": "2",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TournamentIndividualRegistration.objects.filter(tournament=individual_tournament).count(), 2)
        self.assertEqual(TournamentIndividualRegistration.objects.filter(tournament=individual_tournament, user__in=[u1, u2]).count(), 2)
        self.assertFalse(
            TournamentIndividualRegistration.objects.filter(
                tournament=individual_tournament, user=bystander
            ).exists(),
            "Test Maker must not register users it did not create",
        )
        self.assertEqual(IndividualRegistration.objects.filter(tournament=individual_tournament).count(), 0)

    def test_test_maker_register_first_n_individuals_for_selected_tournament(self):
        individual_tournament = self._mk_tournament("Selected Individual Flow", mode="individual")
        individual_tournament.status = "active"
        individual_tournament.save(update_fields=["status"])
        # Prefixed so Test Maker will pick them up; see the note in
        # test_test_maker_register_existing_to_open_tournament_uses_existing_rows.
        u1 = User.objects.create_user(username="tm_selected_i1", password="pass123", first_name="Selected One")
        u2 = User.objects.create_user(username="tm_selected_i2", password="pass123", first_name="Selected Two")
        bystander = User.objects.create_user(username="uninvolved_person", password="pass123", first_name="Uninvolved")

        self.client.force_login(self.organizer)
        session = self.client.session
        session["selected_tournament_id"] = individual_tournament.pk
        session.save()

        response = self.client.post(
            reverse("test_maker"),
            {
                "action": "register_existing_to_open_tournament",
                "existing_count": "2",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(TournamentIndividualRegistration.objects.filter(tournament=individual_tournament).count(), 2)
        self.assertEqual(TournamentIndividualRegistration.objects.filter(tournament=individual_tournament, user__in=[u1, u2]).count(), 2)
        self.assertFalse(
            TournamentIndividualRegistration.objects.filter(
                tournament=individual_tournament, user=bystander
            ).exists(),
            "Test Maker must not register users it did not create",
        )

    def test_tournament_config_hides_internal_shadow_team_names_for_individuals(self):
        tournament = self._mk_tournament("Organizer Individual Config", mode="individual")
        user = User.objects.create_user(username="config_individual", password="pass123")
        shadow = Team.objects.create(
            name="__tm_shadow_6_100_38",
            sport_type=tournament.sport_type,
            is_internal=True,
        )
        TeamTournamentParticipation.objects.create(
            team=shadow,
            tournament=tournament,
            status="active",
        )
        TournamentIndividualRegistration.objects.create(
            tournament=tournament,
            user=user,
            display_name="Player 100",
            shadow_team=shadow,
            status="active",
        )

        self.client.force_login(self.organizer)
        response = self.client.get(reverse("tournament_config", kwargs={"pk": tournament.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Player 100")
        self.assertNotContains(response, "__tm_shadow_6_100_38")

    def test_audit_participant_integrity_reports_missing_shadow_and_legacy_rows(self):
        tournament = self._mk_tournament("Audit Target", mode="individual")
        user = User.objects.create_user(username="audit_u1", password="pass123")
        TournamentIndividualRegistration.objects.create(
            tournament=tournament,
            user=user,
            display_name="Audit Player",
            status="active",
        )
        IndividualRegistration.objects.create(
            tournament=tournament,
            user=user,
            status="approved",
        )

        out = StringIO()
        call_command("audit_participant_integrity", tournament_id=tournament.pk, stdout=out)
        output = out.getvalue()

        self.assertIn("missing_shadow_team=1", output)
        self.assertIn("legacy_individual_regs=1", output)

    def test_reconcile_participant_integrity_applies_safe_sync_fixes(self):
        tournament = self._mk_tournament("Reconcile Target", mode="individual")
        user = User.objects.create_user(username="rec_u1", password="pass123")
        shadow = Team.objects.create(name="rec_shadow", sport_type=tournament.sport_type, is_internal=False)
        reg = TournamentIndividualRegistration.objects.create(
            tournament=tournament,
            user=user,
            display_name="Recon Player",
            shadow_team=shadow,
            status="active",
            group="A",
            seed=7,
        )
        part = TeamTournamentParticipation.objects.create(
            team=shadow,
            tournament=tournament,
            status="withdrawn",
            group="B",
            seed=2,
        )

        self.assertFalse(shadow.is_internal)
        self.assertNotEqual(part.status, reg.status)

        out = StringIO()
        call_command("reconcile_participant_integrity", apply=True, tournament_id=tournament.pk, stdout=out)

        shadow.refresh_from_db()
        part.refresh_from_db()

        self.assertTrue(shadow.is_internal)
        self.assertEqual(part.status, "active")
        self.assertEqual(part.group, "A")
        self.assertEqual(part.seed, 7)

