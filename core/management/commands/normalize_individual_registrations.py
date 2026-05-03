"""Normalize legacy individual-mode tournaments to TournamentIndividualRegistration + internal shadow teams."""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import (
    Team,
    TeamMembership,
    TeamTournamentParticipation,
    Tournament,
    TournamentIndividualRegistration,
    Player,
)


class Command(BaseCommand):
    help = (
        "For individual-mode tournaments: create participant registrations from legacy team enrollments, "
        "mark shadow teams internal, and remove TeamMembership rows tied to those competitors."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Print actions without committing.")
        parser.add_argument(
            "--tournament-slug",
            type=str,
            default="",
            help="Optional tournament name substring filter (case-insensitive).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        name_sub = (options.get("tournament_slug") or "").strip().lower()

        qs = Tournament.objects.filter(registration_mode="individual")
        if name_sub:
            qs = qs.filter(name__icontains=name_sub)

        created_regs = 0
        updated_teams = 0
        removed_memberships = 0
        skipped = 0

        def alloc_display_name(tr, base, exclude_user_id=None):
            name = (base or "Player").strip()[:100] or "Player"
            candidate = name
            n = 0
            while TournamentIndividualRegistration.objects.filter(
                tournament=tr, display_name=candidate
            ).exclude(user_id=exclude_user_id).exists():
                n += 1
                suffix = f" ({n})"
                root = name[: max(1, 100 - len(suffix))]
                candidate = f"{root}{suffix}"
            return candidate

        for tournament in qs.order_by("id"):
            participations = TeamTournamentParticipation.objects.filter(
                tournament=tournament, status="active"
            ).select_related("team")

            for part in participations:
                team = part.team
                if team.is_internal:
                    if TournamentIndividualRegistration.objects.filter(
                        tournament=tournament, shadow_team=team
                    ).exists():
                        continue
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skip internal team={team.pk} ({team.name!r}): no registration row."
                        )
                    )
                    skipped += 1
                    continue

                memberships = list(team.memberships.select_related("user").all())
                if not memberships:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skip participation team={team.pk} ({team.name!r}): no memberships."
                        )
                    )
                    skipped += 1
                    continue

                if len(memberships) > 1:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skip team={team.pk} ({team.name!r}): {len(memberships)} members (ambiguous for individual)."
                        )
                    )
                    skipped += 1
                    continue

                user = memberships[0].user
                display_name = alloc_display_name(
                    tournament, team.name.strip() or user.username, exclude_user_id=user.pk
                )

                if dry_run:
                    self.stdout.write(
                        f"[dry-run] Would register user={user.username!r} display={display_name!r} "
                        f"shadow_team={team.pk} tournament={tournament.name!r}"
                    )
                    created_regs += 1
                    continue

                with transaction.atomic():
                    reg, reg_created = TournamentIndividualRegistration.objects.get_or_create(
                        tournament=tournament,
                        user=user,
                        defaults={
                            "display_name": display_name,
                            "shadow_team": team,
                            "status": part.status,
                            "withdrawn_at": part.withdrawn_at,
                            "group": part.group or "",
                            "seed": part.seed,
                        },
                    )
                    if reg_created:
                        created_regs += 1
                    else:
                        reg.shadow_team = team
                        reg.display_name = display_name
                        reg.status = part.status
                        reg.withdrawn_at = part.withdrawn_at
                        reg.group = part.group or ""
                        reg.seed = part.seed
                        reg.save(
                            update_fields=[
                                "shadow_team",
                                "display_name",
                                "status",
                                "withdrawn_at",
                                "group",
                                "seed",
                                "updated_at",
                            ]
                        )

                    if not team.is_internal:
                        team.is_internal = True
                        team.save(update_fields=["is_internal"])
                        updated_teams += 1

                    for m in memberships:
                        m.delete()
                        removed_memberships += 1

                    Player.objects.get_or_create(team=team, name=display_name)

        suffix = " (dry-run)" if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"Done{suffix}: registrations={created_regs}, teams_marked_internal={updated_teams}, "
                f"memberships_removed={removed_memberships}, skipped_participations={skipped}"
            )
        )
