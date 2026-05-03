from django.core.management.base import BaseCommand

from core.models import TeamTournamentParticipation


class Command(BaseCommand):
	help = "Backfill command — now a no-op since Team is a global entity (teams carry no tournament/status fields)."

	def add_arguments(self, parser):
		parser.add_argument("--dry-run", action="store_true")

	def handle(self, *args, **options):
		count = TeamTournamentParticipation.objects.count()
		self.stdout.write(
			self.style.SUCCESS(
				f"Nothing to backfill. {count} TeamTournamentParticipation record(s) already exist."
			)
		)
