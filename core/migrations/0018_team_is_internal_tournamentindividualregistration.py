# Generated manually for individual/team decoupling

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("core", "0017_tournament_end_date"),
    ]

    operations = [
        migrations.AddField(
            model_name="team",
            name="is_internal",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text="Hidden shadow competitor for individual-mode tournaments; exclude from normal team UX.",
            ),
        ),
        migrations.CreateModel(
            name="TournamentIndividualRegistration",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("display_name", models.CharField(max_length=100)),
                ("status", models.CharField(choices=[("active", "Active"), ("withdrawn", "Withdrawn")], default="active", max_length=20)),
                ("withdrawn_at", models.DateTimeField(blank=True, null=True)),
                ("group", models.CharField(blank=True, default="", max_length=5)),
                ("seed", models.IntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "shadow_team",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="individual_registration_shadows",
                        to="core.team",
                    ),
                ),
                (
                    "tournament",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="individual_registrations",
                        to="core.tournament",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="individual_registrations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["tournament_id", "seed", "display_name", "id"],
            },
        ),
        migrations.AddConstraint(
            model_name="tournamentindividualregistration",
            constraint=models.UniqueConstraint(
                fields=("tournament", "user"),
                name="uniq_tournamentindividualregistration_tournament_user",
            ),
        ),
        migrations.AddConstraint(
            model_name="tournamentindividualregistration",
            constraint=models.UniqueConstraint(
                fields=("tournament", "display_name"),
                name="uniq_tournamentindividualregistration_tournament_display_name",
            ),
        ),
        migrations.AddConstraint(
            model_name="tournamentindividualregistration",
            constraint=models.UniqueConstraint(
                fields=("tournament", "shadow_team"),
                name="uniq_tournamentindividualregistration_tournament_shadow_team",
            ),
        ),
    ]
