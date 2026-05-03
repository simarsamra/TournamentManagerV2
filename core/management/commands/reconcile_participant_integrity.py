"""Reconcile safe participant integrity mismatches for individual-mode tournaments."""

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import TeamTournamentParticipation, Tournament, TournamentIndividualRegistration


class Command(BaseCommand):
    help = (
        "Reconcile safe integrity issues for individual tournaments: enforce internal shadow teams, "
        "create missing shadow participations, and sync status/group/seed/withdrawn fields."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply fixes. Default is dry-run.",
        )
        parser.add_argument(
            "--tournament-id",
            type=int,
            default=0,
            help="Only process one tournament id.",
        )
        parser.add_argument(
            "--name",
            type=str,
            default="",
            help="Case-insensitive tournament name filter.",
        )

    def handle(self, *args, **options):
        apply_changes = options.get("apply", False)
        tournament_id = options.get("tournament_id") or 0
        name_filter = (options.get("name") or "").strip()

        tournaments = Tournament.objects.filter(registration_mode="individual").order_by("id")
        if tournament_id:
            tournaments = tournaments.filter(pk=tournament_id)
        if name_filter:
            tournaments = tournaments.filter(name__icontains=name_filter)

        if not tournaments.exists():
            self.stdout.write(self.style.WARNING("No individual-mode tournaments found for given filters."))
            return

        checked = 0
        skipped_missing_shadow = 0
        fixed_internal_flag = 0
        fixed_participation_created = 0
        fixed_participation_synced = 0

        mode = "APPLY" if apply_changes else "DRY-RUN"
        self.stdout.write(f"[{mode}] Reconciliation starting...")

        for tournament in tournaments:
            regs = TournamentIndividualRegistration.objects.filter(tournament=tournament)
            self.stdout.write(
                f"Tournament #{tournament.pk} '{tournament.name}': {regs.count()} registration(s)"
            )

            for reg in regs.select_related("shadow_team"):
                checked += 1

                if not reg.shadow_team_id:
                    skipped_missing_shadow += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"  - Skipped reg#{reg.pk}: missing shadow_team link (not auto-created)."
                        )
                    )
                    continue

                shadow_team = reg.shadow_team

                if not shadow_team.is_internal:
                    if apply_changes:
                        shadow_team.is_internal = True
                        shadow_team.save(update_fields=["is_internal"])
                    fixed_internal_flag += 1
                    self.stdout.write(
                        f"  - {'Fixed' if apply_changes else 'Would fix'} team#{shadow_team.pk} is_internal=True"
                    )

                with transaction.atomic():
                    participation, created = TeamTournamentParticipation.objects.get_or_create(
                        team=shadow_team,
                        tournament=tournament,
                        defaults={
                            "status": reg.status,
                            "withdrawn_at": reg.withdrawn_at,
                            "group": reg.group or "",
                            "seed": reg.seed,
                        },
                    )

                    if created:
                        if apply_changes:
                            fixed_participation_created += 1
                        else:
                            participation.delete()
                            fixed_participation_created += 1
                        self.stdout.write(
                            f"  - {'Created' if apply_changes else 'Would create'} missing participation for reg#{reg.pk}"
                        )
                        continue

                    dirty = False
                    if participation.status != reg.status:
                        participation.status = reg.status
                        dirty = True
                    if (participation.group or "") != (reg.group or ""):
                        participation.group = reg.group or ""
                        dirty = True
                    if participation.seed != reg.seed:
                        participation.seed = reg.seed
                        dirty = True
                    if participation.withdrawn_at != reg.withdrawn_at:
                        participation.withdrawn_at = reg.withdrawn_at
                        dirty = True

                    if dirty:
                        fixed_participation_synced += 1
                        self.stdout.write(
                            f"  - {'Synced' if apply_changes else 'Would sync'} participation fields for reg#{reg.pk}"
                        )
                        if apply_changes:
                            participation.save(
                                update_fields=["status", "group", "seed", "withdrawn_at", "updated_at"]
                            )

        self.stdout.write(
            self.style.SUCCESS(
                "Reconciliation complete: "
                f"checked={checked}, "
                f"skipped_missing_shadow={skipped_missing_shadow}, "
                f"fixed_internal_flag={fixed_internal_flag}, "
                f"fixed_participation_created={fixed_participation_created}, "
                f"fixed_participation_synced={fixed_participation_synced}."
            )
        )
