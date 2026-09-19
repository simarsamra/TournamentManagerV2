"""Individual-mode registrations and their shadow-team participations must not
drift apart: the registration drives the UI and participant counts, the shadow
participation drives the match engine and standings."""
from django.contrib.auth.models import User
from django.test import TestCase

from core.models import (
    OrganizerProfile, TeamTournamentParticipation, Tournament,
    TournamentIndividualRegistration,
)
from core.services.enrollment import active_participant_count
from core.views import _ensure_shadow_team_for_registration


class RegistrationShadowSyncTests(TestCase):
    def setUp(self):
        self.organizer = User.objects.create_user(
            username="org", password="Regression-Pass-1"
        )
        OrganizerProfile.objects.filter(user=self.organizer).update(verified=True)
        self.tournament = Tournament.objects.create(
            name="IND", format="round_robin", status="registration_open",
            players_per_team=1, registration_mode="individual",
            created_by=self.organizer,
        )
        self.player = User.objects.create_user(
            username="player", password="Regression-Pass-1"
        )
        self.registration = TournamentIndividualRegistration.objects.create(
            tournament=self.tournament, user=self.player,
            display_name="Player One", status="active",
        )
        _ensure_shadow_team_for_registration(
            self.registration, self.tournament.sport_type
        )
        self.registration.refresh_from_db()
        self.client.force_login(self.organizer)

    def _participation(self):
        return TeamTournamentParticipation.objects.get(
            team=self.registration.shadow_team, tournament=self.tournament
        )

    def test_rejecting_a_registration_withdraws_the_shadow_participation(self):
        self.assertEqual(self._participation().status, "active")

        self.client.post(
            f"/tournament/{self.tournament.pk}/registrations/"
            f"{self.registration.pk}/reject/",
            {"reason": "no show"},
        )

        self.registration.refresh_from_db()
        self.assertEqual(self.registration.status, "withdrawn")
        self.assertIsNotNone(self.registration.withdrawn_at)
        self.assertEqual(
            self._participation().status, "withdrawn",
            "a rejected player must not remain schedulable",
        )
        self.assertEqual(active_participant_count(self.tournament), 0)

    def test_approving_a_registration_activates_the_shadow_participation(self):
        self.registration.status = "withdrawn"
        self.registration.save(update_fields=["status"])
        TeamTournamentParticipation.objects.filter(
            pk=self._participation().pk
        ).update(status="withdrawn")

        self.client.post(
            f"/tournament/{self.tournament.pk}/registrations/"
            f"{self.registration.pk}/approve/"
        )

        self.registration.refresh_from_db()
        self.assertEqual(self.registration.status, "active")
        self.assertEqual(self._participation().status, "active")

    def test_disqualifying_the_participation_withdraws_the_registration(self):
        participation = self._participation()
        self.client.post(
            f"/tournament/{self.tournament.pk}/disqualify/{participation.pk}/",
            {"reason": "conduct"},
        )

        self.registration.refresh_from_db()
        self.assertEqual(
            self.registration.status, "withdrawn",
            "a disqualified competitor must not still appear as a participant",
        )
        self.assertEqual(active_participant_count(self.tournament), 0)


class SeedingTests(TestCase):
    def setUp(self):
        self.organizer = User.objects.create_user(
            username="org2", password="Regression-Pass-1"
        )
        OrganizerProfile.objects.filter(user=self.organizer).update(verified=True)
        self.tournament = Tournament.objects.create(
            name="SEED", format="knockout", status="registration_open",
            players_per_team=1, registration_mode="individual",
            created_by=self.organizer,
        )
        self.registration = TournamentIndividualRegistration.objects.create(
            tournament=self.tournament,
            user=User.objects.create_user(
                username="seeded", password="Regression-Pass-1"
            ),
            display_name="Seeded", status="active",
        )
        _ensure_shadow_team_for_registration(
            self.registration, self.tournament.sport_type
        )
        self.client.force_login(self.organizer)

    def test_json_seeds_are_applied(self):
        """JSON object keys are strings; the apply loop looked them up by int pk,
        so no seed ever matched and the view still said 'Seeds saved.'"""
        import json

        self.client.post(
            f"/tournament/{self.tournament.pk}/seed/",
            data=json.dumps({"seeds": {str(self.registration.pk): 7}}),
            content_type="application/json",
        )
        self.registration.refresh_from_db()
        self.assertEqual(self.registration.seed, 7)

    def test_non_numeric_json_seed_is_rejected(self):
        import json

        response = self.client.post(
            f"/tournament/{self.tournament.pk}/seed/",
            data=json.dumps({"seeds": {str(self.registration.pk): "abc"}}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_form_seeds_still_work(self):
        self.client.post(
            f"/tournament/{self.tournament.pk}/seed/",
            {f"seed_{self.registration.pk}": "3"},
        )
        self.registration.refresh_from_db()
        self.assertEqual(self.registration.seed, 3)
