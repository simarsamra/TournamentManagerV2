"""Backup and restore functionality."""
import json
import os
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core import serializers
from django.db import connection, transaction

from .models import (
    Tournament, Court, TimeSlot, Team, Match, Player,
    RescheduleRequest, OpenSlot, AuditLog, BackupRecord, CourtAvailability,
    TeamMembership, TeamTournamentParticipation, TeamTournamentCourtPreference,
    TournamentIndividualRegistration, UserTeamAssignment, TeamInvite,
    Notification, OrganizerProfile, OrganizerApplication,
    TeamRegistration, IndividualRegistration, NoShowReport,
)
from .signals import suppress_user_autocreate


# Order matters on restore: parents before children. Circular and
# self-referential FKs (Tournament.champion -> Team, Match.next_match -> Match)
# are handled by deferring constraint checks for the duration of the restore.
#
# Every model with a FK into this set MUST be listed. restore_backup() deletes
# each of these tables, and a delete cascades into unlisted children that the
# backup never captured.
BACKUP_MODELS = [
    User,
    Team,
    Tournament,
    Court,
    CourtAvailability,
    TimeSlot,
    Player,
    OrganizerProfile,
    OrganizerApplication,
    TeamMembership,
    UserTeamAssignment,
    TeamInvite,
    TeamTournamentParticipation,
    TeamTournamentCourtPreference,
    TournamentIndividualRegistration,
    TeamRegistration,
    IndividualRegistration,
    Match,
    RescheduleRequest,
    NoShowReport,
    OpenSlot,
    Notification,
    AuditLog,
    BackupRecord,
]

BACKUP_FORMAT_VERSION = 2


def create_backup(user=None, is_auto=False, notes=""):
    """Create a JSON backup of all data."""
    backup_dir = Path(settings.BACKUP_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = "auto" if is_auto else "manual"
    filename = f"backup_{prefix}_{timestamp}.json"
    filepath = backup_dir / filename

    data = {}
    for model in BACKUP_MODELS:
        model_name = f"{model._meta.app_label}.{model._meta.model_name}"
        data[model_name] = json.loads(serializers.serialize("json", model.objects.all()))

    data["_meta"] = {
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": datetime.now().isoformat(),
        "models": [f"{m._meta.app_label}.{m._meta.model_name}" for m in BACKUP_MODELS],
    }

    content = json.dumps(data, indent=2, default=str)
    filepath.write_text(content)

    size = filepath.stat().st_size
    record = BackupRecord.objects.create(
        filename=filename,
        created_by=user,
        size_bytes=size,
        is_auto=is_auto,
        notes=notes,
    )
    return record


def validate_backup(filepath):
    """Validate a backup file before restore."""
    try:
        with open(filepath, "r") as f:
            data = json.load(f)

        if "_m2m_team_preferred_courts" in data and "_meta" not in data:
            return False, (
                "This backup predates the global-team schema change and cannot be "
                "restored safely: it contains no roster, membership or registration "
                "data, so restoring it would delete all of yours."
            )

        meta = data.get("_meta") or {}
        version = meta.get("format_version")
        if version != BACKUP_FORMAT_VERSION:
            return False, (
                f"Unsupported backup format version {version!r} "
                f"(this server reads version {BACKUP_FORMAT_VERSION})."
            )

        expected = {f"{m._meta.app_label}.{m._meta.model_name}" for m in BACKUP_MODELS}
        missing = sorted(expected - set(data))
        if missing:
            return False, "Backup is missing required data: " + ", ".join(missing)

        return True, "Backup is valid"
    except json.JSONDecodeError:
        return False, "Invalid JSON file"
    except Exception as e:
        return False, str(e)


def restore_backup(filepath):
    """Restore data from a backup file.

    Runs inside a single transaction with FK constraint checks deferred, so a
    partial failure rolls back rather than leaving the database half-wiped.
    Refuses outright to restore a backup that does not validate.
    """
    valid, message = validate_backup(filepath)
    if not valid:
        raise ValueError(f"Refusing to restore an invalid backup: {message}")

    with open(filepath, "r") as f:
        data = json.load(f)

    with transaction.atomic(), suppress_user_autocreate():
        with connection.constraint_checks_disabled():
            for model in reversed(BACKUP_MODELS):
                model.objects.all().delete()

            for model in BACKUP_MODELS:
                model_name = f"{model._meta.app_label}.{model._meta.model_name}"
                if model_name not in data:
                    continue
                for obj in serializers.deserialize("json", json.dumps(data[model_name])):
                    obj.save()

        # Re-assert every constraint now that all rows are present.
        connection.check_constraints(
            table_names=[model._meta.db_table for model in BACKUP_MODELS]
        )

    return True


def list_backups():
    """List all available backup files."""
    backup_dir = Path(settings.BACKUP_DIR)
    if not backup_dir.exists():
        return []

    files = []
    for f in sorted(backup_dir.glob("backup_*.json"), reverse=True):
        files.append({
            "filename": f.name,
            "path": str(f),
            "size": f.stat().st_size,
            "modified": datetime.fromtimestamp(f.stat().st_mtime),
        })
    return files


def delete_backup(filename):
    """Delete a backup file."""
    backup_dir = Path(settings.BACKUP_DIR).resolve()
    filepath = (backup_dir / filename).resolve()
    # Guard against path traversal
    if not str(filepath).startswith(str(backup_dir) + os.sep):
        return False
    if filepath.exists() and filepath.suffix == ".json":
        filepath.unlink()
        BackupRecord.objects.filter(filename=filename).delete()
        return True
    return False
