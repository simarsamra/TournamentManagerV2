"""Audit participant integrity for team/individual tournament modes."""

from django.core.management.base import BaseCommand
from django.db.models import Q

from core.models import (
    IndividualRegistration,
    Team,
    TeamRegistration,
    TeamTournamentParticipation,
    Tournament,
    TournamentIndividualRegistration,
)


class Command(BaseCommand):
    help = (
        "Report participant integrity issues: missing shadow links, orphan internal teams, "
        "registration/participation mismatches, and legacy registration-model row counts."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tournament-id",
            type=int,
            default=0,
            help="Audit only a specific tournament id.",
        )
        parser.add_argument(
            "--name",
            type=str,
            default="",
            help="Case-insensitive tournament name contains filter.",
        )

    def handle(self, *args, **options):
        tournament_id = options.get("tournament_id") or 0
        name_filter = (options.get("name") or "").strip()

        tournaments = Tournament.objects.all().order_by("id")
        if tournament_id:
            tournaments = tournaments.filter(pk=tournament_id)
        if name_filter:
            tournaments = tournaments.filter(name__icontains=name_filter)

        if not tournaments.exists():
            self.stdout.write(self.style.WARNING("No tournaments found for given filters."))
            return

        global_issues = 0
        for tournament in tournaments:
            issues = []

            regs = TournamentIndividualRegistration.objects.filter(tournament=tournament)
            active_regs = regs.filter(status="active")
            parts = TeamTournamentParticipation.objects.filter(tournament=tournament)

            missing_shadow_team = regs.filter(shadow_team__isnull=True).count()
            if missing_shadow_team:
                issues.append(f"missing_shadow_team={missing_shadow_team}")

            missing_shadow_participation = 0
            mismatch_status = 0
            mismatch_group = 0
            mismatch_seed = 0
            mismatch_withdrawn = 0
            for reg in regs.exclude(shadow_team__isnull=True).select_related("shadow_team"):
                part = parts.filter(team=reg.shadow_team).first()
                if not part:
                    missing_shadow_participation += 1
                    continue
                if part.status != reg.status:
                    mismatch_status += 1
                if (part.group or "") != (reg.group or ""):
                    mismatch_group += 1
                if part.seed != reg.seed:
                    mismatch_seed += 1
                if bool(part.withdrawn_at) != bool(reg.withdrawn_at):
                    mismatch_withdrawn += 1

            if missing_shadow_participation:
                issues.append(f"missing_shadow_participation={missing_shadow_participation}")
            if mismatch_status:
                issues.append(f"mismatch_status={mismatch_status}")
            if mismatch_group:
                issues.append(f"mismatch_group={mismatch_group}")
            if mismatch_seed:
                issues.append(f"mismatch_seed={mismatch_seed}")
            if mismatch_withdrawn:
                issues.append(f"mismatch_withdrawn={mismatch_withdrawn}")

            orphan_internal = Team.objects.filter(
                is_internal=True,
                participations__tournament=tournament,
            ).exclude(
                individual_registration_shadows__tournament=tournament
            ).distinct().count()
            if orphan_internal:
                issues.append(f"orphan_internal_teams={orphan_internal}")

            unexpected_internal_in_team_mode = 0
            unexpected_external_in_individual_mode = 0
            if tournament.registration_mode == "team":
                unexpected_internal_in_team_mode = parts.filter(team__is_internal=True).count()
                if unexpected_internal_in_team_mode:
                    issues.append(
                        f"unexpected_internal_in_team_mode={unexpected_internal_in_team_mode}"
                    )
            else:
                unexpected_external_in_individual_mode = active_regs.filter(
                    Q(shadow_team__isnull=True) | Q(shadow_team__is_internal=False)
                ).count()
                if unexpected_external_in_individual_mode:
                    issues.append(
                        "invalid_individual_shadow_links="
                        f"{unexpected_external_in_individual_mode}"
                    )

            team_registration_rows = TeamRegistration.objects.filter(tournament=tournament).count()
            individual_registration_rows = IndividualRegistration.objects.filter(tournament=tournament).count()

            header = (
                f"Tournament #{tournament.pk} '{tournament.name}' mode={tournament.registration_mode} "
                f"| tir={regs.count()} active_tir={active_regs.count()} parts={parts.count()} "
                f"legacy_team_regs={team_registration_rows} legacy_individual_regs={individual_registration_rows}"
            )
            self.stdout.write(header)

            if issues:
                global_issues += len(issues)
                for item in issues:
                    self.stdout.write(self.style.WARNING(f"  - {item}"))
            else:
                self.stdout.write(self.style.SUCCESS("  - OK"))

        if global_issues:
            self.stdout.write(self.style.WARNING(f"Audit completed with {global_issues} issue marker(s)."))
        else:
            self.stdout.write(self.style.SUCCESS("Audit completed with no detected integrity issues."))
