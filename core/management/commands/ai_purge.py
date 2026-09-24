"""Delete AI analytics questions older than AI_RETENTION_DAYS (D-4). Run daily:

    python manage.py ai_purge
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from core.ai.jobs import purge_old


class Command(BaseCommand):
    help = "Delete AI questions and answers older than the retention period."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int,
                            help=f"Override AI_RETENTION_DAYS ({settings.AI_RETENTION_DAYS}).")
        parser.add_argument("--dry-run", action="store_true", help="Only report the count.")

    def handle(self, *args, **options):
        days = options["days"] if options["days"] is not None else settings.AI_RETENTION_DAYS
        count = purge_old(days=days, dry_run=options["dry_run"])
        verb = "Would delete" if options["dry_run"] else "Deleted"
        self.stdout.write(f"{verb} {count} AI question(s) older than {days} days.")
