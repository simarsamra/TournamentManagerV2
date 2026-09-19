"""Backup/restore round-trip guards.

These pin two properties that were both broken at once: create_backup() read a
field the global-team migration removed, and restore_backup() deleted 13 models
that BACKUP_MODELS never captured.
"""
import tempfile
from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from core.backup import create_backup, restore_backup, validate_backup
from core.models import (
    Court, Notification, Team, TeamMembership, TeamTournamentCourtPreference,
    TeamTournamentParticipation, Tournament, TournamentIndividualRegistration,
)


class BackupRoundTripTests(TestCase):
    def _populate(self):
        tournament = Tournament.objects.create(
            name="RT", format="round_robin", players_per_team=2
        )
        court = Court.objects.create(tournament=tournament, name="C1")
        team = Team.objects.create(name="Alpha")
        participation = TeamTournamentParticipation.objects.create(
            team=team, tournament=tournament, status="active"
        )
        TeamTournamentCourtPreference.objects.create(
            participation=participation, court=court
        )
        user = User.objects.create_user(username="alice", password="Regression-Pass-1")
        TeamMembership.objects.create(team=team, user=user, role="captain")
        Notification.objects.create(
            user=user, notification_type="general", message="hello",
            tournament=tournament,
        )
        shadow = Team.objects.create(name="__tm_shadow_rt", is_internal=True)
        TournamentIndividualRegistration.objects.create(
            tournament=tournament, user=user, display_name="Alice", shadow_team=shadow
        )
        return tournament

    def test_create_backup_succeeds_with_teams_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(BACKUP_DIR=Path(tmp)):
                self._populate()
                record = create_backup(notes="round-trip")
                self.assertTrue((Path(tmp) / record.filename).exists())

    def test_restore_preserves_rosters_and_registrations(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(BACKUP_DIR=Path(tmp)):
                self._populate()
                record = create_backup(notes="round-trip")
                path = Path(tmp) / record.filename

                # Wipe the tables the old BACKUP_MODELS never captured.
                Notification.objects.all().delete()
                TeamMembership.objects.all().delete()
                TeamTournamentCourtPreference.objects.all().delete()
                TournamentIndividualRegistration.objects.all().delete()

                restore_backup(path)

                self.assertEqual(TeamMembership.objects.count(), 1)
                self.assertEqual(TeamTournamentParticipation.objects.count(), 1)
                self.assertEqual(TeamTournamentCourtPreference.objects.count(), 1)
                self.assertEqual(TournamentIndividualRegistration.objects.count(), 1)
                self.assertEqual(Notification.objects.count(), 1)
                self.assertTrue(User.objects.filter(username="alice").exists())

    def test_legacy_backup_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "backup_manual_legacy.json"
            legacy.write_text(
                '{"auth.user": [], "core.tournament": [], '
                '"_m2m_team_preferred_courts": {}}'
            )
            valid, message = validate_backup(legacy)
            self.assertFalse(valid)
            self.assertIn("predates", message)

    def test_restore_refuses_an_invalid_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "backup_manual_bad.json"
            bad.write_text('{"auth.user": []}')
            with self.assertRaises(ValueError):
                restore_backup(bad)

    def test_every_model_with_a_backed_up_parent_is_itself_backed_up(self):
        """A delete on a backed-up table must not cascade into an uncaptured one."""
        from django.apps import apps
        from core.backup import BACKUP_MODELS

        backed_up = set(BACKUP_MODELS)
        missing = []
        for model in apps.get_app_config("core").get_models():
            if model in backed_up:
                continue
            for field in model._meta.get_fields():
                if field.is_relation and getattr(field, "many_to_one", False):
                    if field.related_model in backed_up:
                        missing.append(model.__name__)
                        break
        self.assertEqual(
            missing, [],
            "These models hold a FK into a backed-up table but are not in "
            "BACKUP_MODELS, so a restore would delete them without restoring "
            "them: " + ", ".join(missing),
        )
