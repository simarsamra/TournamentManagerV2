import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0010_update_user_team_relationships"),
    ]

    operations = [
        migrations.AddField(
            model_name="match",
            name="critical_dispute",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="match",
            name="dispute_deadline_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="match",
            name="dispute_resolution_notes",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="match",
            name="dispute_resolved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="match",
            name="disputed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="raised_disputes",
                to="core.team",
            ),
        ),
        migrations.AddField(
            model_name="match",
            name="score_locked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="match",
            name="score_submitted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
