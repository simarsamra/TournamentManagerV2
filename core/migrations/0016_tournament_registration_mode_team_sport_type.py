from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0015_global_team_schema"),
    ]

    operations = [
        migrations.AddField(
            model_name="team",
            name="sport_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("badminton", "Badminton"),
                    ("tennis", "Tennis"),
                    ("volleyball", "Volleyball"),
                    ("basketball", "Basketball"),
                    ("soccer", "Soccer"),
                    ("cricket", "Cricket"),
                    ("table_tennis", "Table Tennis"),
                    ("other", "Other"),
                ],
                default="other",
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="tournament",
            name="registration_mode",
            field=models.CharField(
                choices=[
                    ("team", "Register Teams"),
                    ("individual", "Register Individuals"),
                ],
                default="team",
                help_text="Choose whether this tournament registers full teams or individual players.",
                max_length=20,
            ),
        ),
    ]
