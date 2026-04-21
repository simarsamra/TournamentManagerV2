"""Backup and restore functionality."""
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core import serializers

from .models import (
    Tournament, Court, TimeSlot, Team, Match,
    RescheduleRequest, OpenSlot, AuditLog, BackupRecord, CourtAvailability,
)


BACKUP_MODELS = [
    User, Tournament, Court, TimeSlot, CourtAvailability, Team, Match,
    RescheduleRequest, OpenSlot, AuditLog, BackupRecord,
]


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

    # Include M2M relationships
    m2m_data = {}
    for team in Team.objects.all():
        m2m_data[team.id] = list(team.preferred_courts.values_list("id", flat=True))
    data["_m2m_team_preferred_courts"] = m2m_data

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

        required_keys = ["auth.user", "core.tournament"]
        for key in required_keys:
            if key not in data:
                return False, f"Missing required data: {key}"

        return True, "Backup is valid"
    except json.JSONDecodeError:
        return False, "Invalid JSON file"
    except Exception as e:
        return False, str(e)


def restore_backup(filepath):
    """Restore data from a backup file."""
    with open(filepath, "r") as f:
        data = json.load(f)

    # Clear existing data in reverse dependency order
    for model in reversed(BACKUP_MODELS):
        model.objects.all().delete()

    # Restore in dependency order
    for model in BACKUP_MODELS:
        model_name = f"{model._meta.app_label}.{model._meta.model_name}"
        if model_name in data:
            objects = serializers.deserialize("json", json.dumps(data[model_name]))
            for obj in objects:
                obj.save()

    # Restore M2M
    m2m_data = data.get("_m2m_team_preferred_courts", {})
    for team_id_str, court_ids in m2m_data.items():
        try:
            team = Team.objects.get(id=int(team_id_str))
            team.preferred_courts.set(court_ids)
        except Team.DoesNotExist:
            pass

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
