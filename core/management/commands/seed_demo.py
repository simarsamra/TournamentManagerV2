from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from core.models import Team, TeamMembership, TeamTournamentParticipation, Tournament


class Command(BaseCommand):
    """Seed a demo tournament with N teams for local development.

    Promoted from scripts/seed_tt1.py (F-6): that script hardcoded
    Tournament.objects.get(pk=12) and Team.objects.get_or_create(tournament=t,
    ...) -- fields Team hasn't had since the Team/TeamTournamentParticipation
    split (see backfill_team_participations.py). It would raise on the
    current schema, not just on someone else's database. This command keeps
    the same intent -- N teams, each a captain ("t<N>p1") and a second
    player ("t<N>p2"), idempotent so it's safe to re-run -- against the
    schema as it actually is today.
    """

    help = "Seed a demo tournament with N teams (a captain + one member each). Idempotent."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tournament", default="Demo Tournament",
            help="Tournament name to seed into; created if it doesn't exist.",
        )
        parser.add_argument(
            "--teams", type=int, default=8,
            help="Number of teams to create (default: 8).",
        )
        parser.add_argument(
            "--password", default="pass123",
            help="Password set on every seeded user (default: pass123).",
        )

    def handle(self, *args, **options):
        name = options["tournament"]
        team_count = options["teams"]
        password = options["password"]

        tournament, created = Tournament.objects.get_or_create(
            name=name,
            defaults={
                "format": "round_robin",
                "sport_type": "table_tennis",
                "registration_mode": "team",
                "status": "registration_open",
                "players_per_team": 2,
                "points_per_win": 3,
                "points_per_loss": 0,
                "points_per_draw": 1,
            },
        )
        self.stdout.write(
            f"Tournament: {tournament.name!r} (pk={tournament.pk}) "
            f"{'created' if created else 'found'}, status={tournament.status}"
        )

        for n in range(1, team_count + 1):
            team_name = f"Team {n}"
            cap_username = f"t{n}p1"
            p2_username = f"t{n}p2"

            captain, cap_created = User.objects.get_or_create(
                username=cap_username, defaults={"first_name": f"Player T{n}P1"}
            )
            captain.set_password(password)
            captain.save()

            player2, p2_created = User.objects.get_or_create(
                username=p2_username, defaults={"first_name": f"Player T{n}P2"}
            )
            player2.set_password(password)
            player2.save()

            team, team_created = Team.objects.get_or_create(
                name=team_name, defaults={"sport_type": tournament.sport_type}
            )
            TeamTournamentParticipation.objects.get_or_create(
                team=team, tournament=tournament, defaults={"status": "active", "seed": n},
            )
            TeamMembership.objects.get_or_create(
                team=team, user=captain, defaults={"role": "captain"}
            )
            TeamMembership.objects.get_or_create(
                team=team, user=player2, defaults={"role": "member"}
            )

            self.stdout.write(
                f"  {team_name}: captain={cap_username} "
                f"({'created' if cap_created else 'found'}), "
                f"member={p2_username} ({'created' if p2_created else 'found'}), "
                f"team {'created' if team_created else 'found'}"
            )

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {team_count} teams into {tournament.name!r} (pk={tournament.pk})."
        ))
