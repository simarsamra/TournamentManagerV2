from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def create_captain_memberships(apps, schema_editor):
    """Backfill a captain TeamMembership for every existing Team."""
    Team = apps.get_model("core", "Team")
    TeamMembership = apps.get_model("core", "TeamMembership")
    for team in Team.objects.select_related("user"):
        TeamMembership.objects.get_or_create(
            user=team.user,
            defaults={"team": team, "role": "captain"},
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_noshowreport"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="TeamMembership",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role", models.CharField(
                    choices=[("captain", "Captain"), ("member", "Member")],
                    default="member",
                    max_length=20,
                )),
                ("joined_at", models.DateTimeField(auto_now_add=True)),
                (
                    "team",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="memberships",
                        to="core.team",
                    ),
                ),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="team_membership",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["role", "joined_at"],
            },
        ),
        migrations.RunPython(create_captain_memberships, reverse_code=noop),
    ]
