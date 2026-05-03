from django.db import migrations


def backfill_team_participations(apps, schema_editor):
    Team = apps.get_model("core", "Team")
    TeamTournamentParticipation = apps.get_model("core", "TeamTournamentParticipation")
    TeamTournamentCourtPreference = apps.get_model("core", "TeamTournamentCourtPreference")

    for team in Team.objects.select_related("tournament").prefetch_related("preferred_courts").all():
        participation, _ = TeamTournamentParticipation.objects.get_or_create(
            team=team,
            tournament=team.tournament,
            defaults={
                "status": team.status,
                "withdrawn_at": team.withdrawn_at,
                "group": team.group,
                "seed": team.seed,
                "availability_notes": team.availability_notes,
            },
        )

        for court in team.preferred_courts.all():
            TeamTournamentCourtPreference.objects.get_or_create(
                participation=participation,
                court=court,
            )


def noop_reverse(apps, schema_editor):
    # Keep reverse migration safe and non-destructive.
    return


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0012_alter_teammembership_role_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_team_participations, noop_reverse),
    ]
