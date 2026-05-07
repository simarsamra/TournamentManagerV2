from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0027_add_notification_types"),
    ]

    operations = [
        migrations.AddField(
            model_name="tournament",
            name="enable_third_place_match",
            field=models.BooleanField(
                default=False,
                help_text="Generate a 3rd-place match between semi-final losers (knockout & hybrid only)",
            ),
        ),
    ]
