"""Repository-hygiene and deployment-configuration guards.

These are not feature tests. They fail if something that must never be
committed becomes tracked again.
"""
import shutil
import subprocess
from pathlib import Path

from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git_tracked(pathspec):
    """Return tracked paths matching pathspec, or None when git is unavailable."""
    if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
        return None
    result = subprocess.run(
        ["git", "ls-files", pathspec],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line.strip()]


class RepositoryHygieneTests(SimpleTestCase):
    def test_backups_directory_is_not_tracked_by_git(self):
        tracked = _git_tracked("backups")
        if tracked is None:
            self.skipTest("not a git checkout")
        self.assertEqual(
            tracked,
            [],
            "backups/ must not be tracked: these files serialize auth.User "
            "including password hashes. Found: " + ", ".join(tracked),
        )

    def test_backup_dir_is_outside_the_working_tree(self):
        from django.conf import settings

        backup_dir = Path(settings.BACKUP_DIR).resolve()
        self.assertFalse(
            str(backup_dir).startswith(str(REPO_ROOT.resolve()) + "/")
            or backup_dir == REPO_ROOT.resolve(),
            f"BACKUP_DIR ({backup_dir}) must not sit inside the git working tree "
            f"({REPO_ROOT.resolve()}).",
        )

    def test_no_test_modules_in_project_root(self):
        stray = sorted(p.name for p in REPO_ROOT.glob("test*.py"))
        self.assertEqual(
            stray,
            [],
            "Modules matching test*.py in the project root are picked up by "
            "Django's test discovery. Move them to scripts/. Found: "
            + ", ".join(stray),
        )
