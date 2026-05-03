"""
Data migration: deduplicate Team rows so each team name is globally unique.

Strategy:
  - For each group of Team rows sharing the same name, keep the one with the
    lowest pk (canonical) and merge all others into it.
  - All FK / M2M references that point to a duplicate team are rewritten to
    point to the canonical team before the duplicates are deleted.
  - A captain TeamMembership is ensured for every canonical team (using
    Team.user while that field still exists on the model).
"""

from django.db import migrations


def _deduplicate_teams(apps, schema_editor):
    Team = apps.get_model("core", "Team")
    TeamMembership = apps.get_model("core", "TeamMembership")
    TeamTournamentParticipation = apps.get_model("core", "TeamTournamentParticipation")
    Tournament = apps.get_model("core", "Tournament")
    Match = apps.get_model("core", "Match")
    Player = apps.get_model("core", "Player")
    RescheduleRequest = apps.get_model("core", "RescheduleRequest")
    NoShowReport = apps.get_model("core", "NoShowReport")

    # Build a mapping: name (lower) -> canonical team (lowest pk)
    seen = {}  # name_lower -> canonical Team instance
    for team in Team.objects.order_by("id"):
        key = team.name.strip().lower()
        if key not in seen:
            seen[key] = team
            # Ensure a captain membership exists for the canonical team
            if not TeamMembership.objects.filter(team=team, role="captain").exists():
                # Use Team.user (still present at this migration point)
                TeamMembership.objects.get_or_create(
                    team=team,
                    user=team.user,
                    defaults={"role": "captain"},
                )
        else:
            canonical = seen[key]
            duplicate = team

            # --- Remap TeamTournamentParticipation ---
            for part in TeamTournamentParticipation.objects.filter(team=duplicate):
                if not TeamTournamentParticipation.objects.filter(
                    team=canonical, tournament=part.tournament
                ).exists():
                    part.team = canonical
                    part.save(update_fields=["team"])
                else:
                    # Canonical already has a participation for this tournament
                    # — discard the duplicate participation (its court prefs cascade away)
                    part.delete()

            # --- Remap Match FK fields ---
            Match.objects.filter(team1=duplicate).update(team1=canonical)
            Match.objects.filter(team2=duplicate).update(team2=canonical)
            Match.objects.filter(winner=duplicate).update(winner=canonical)
            Match.objects.filter(submitted_by=duplicate).update(submitted_by=canonical)
            Match.objects.filter(confirmed_by=duplicate).update(confirmed_by=canonical)
            Match.objects.filter(disputed_by=duplicate).update(disputed_by=canonical)

            # --- Remap RescheduleRequest ---
            RescheduleRequest.objects.filter(requested_by=duplicate).update(requested_by=canonical)

            # --- Remap NoShowReport ---
            NoShowReport.objects.filter(reported_by=duplicate).update(reported_by=canonical)
            NoShowReport.objects.filter(absent_team=duplicate).update(absent_team=canonical)
            NoShowReport.objects.filter(present_team=duplicate).update(present_team=canonical)

            # --- Remap Player ---
            Player.objects.filter(team=duplicate).update(team=canonical)

            # --- Remap TeamMembership (avoid unique_together conflicts) ---
            for membership in TeamMembership.objects.filter(team=duplicate):
                if not TeamMembership.objects.filter(team=canonical, user=membership.user).exists():
                    membership.team = canonical
                    membership.save(update_fields=["team"])
                else:
                    membership.delete()

            # --- Remap Tournament.champion ---
            Tournament.objects.filter(champion=duplicate).update(champion=canonical)

            # Delete the now-orphaned duplicate
            duplicate.delete()


def _noop(apps, schema_editor):
    pass  # Data migration — reverse is intentionally a no-op


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0013_backfill_team_participations"),
    ]

    operations = [
        migrations.RunPython(_deduplicate_teams, _noop),
    ]
