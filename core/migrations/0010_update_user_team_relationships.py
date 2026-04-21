from django.conf import settings
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0009_team_department'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Step 1: Change Team.user from OneToOneField to ForeignKey
        migrations.AlterField(
            model_name='team',
            name='user',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='captained_teams',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        # Step 2: Add new unique_together for Team (tournament, user) — (tournament, name) already exists
        migrations.AlterUniqueTogether(
            name='team',
            unique_together={('tournament', 'name'), ('tournament', 'user')},
        ),
        # Step 3: Change TeamMembership.user from OneToOneField to ForeignKey
        migrations.AlterField(
            model_name='teammembership',
            name='user',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='memberships',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        # Step 4: Add unique_together for TeamMembership (team, user)
        migrations.AlterUniqueTogether(
            name='teammembership',
            unique_together={('team', 'user')},
        ),
    ]
