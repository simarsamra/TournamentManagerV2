"""manage.py seed_demo -- promoted from scripts/seed_tt1.py (F-6): that
script hardcoded Tournament.objects.get(pk=12) and a Team.tournament kwarg
that hasn't existed since the Team/TeamTournamentParticipation split, so it
no longer ran at all. This command keeps its intent (N teams, each a
captain + one member, idempotent) against the current schema.
"""
from io import StringIO

from django.contrib.auth.models import User
from django.contrib.auth.hashers import check_password
from django.core.management import call_command
from django.test import TestCase

from core.models import Team, TeamMembership, TeamTournamentParticipation, Tournament


class SeedDemoCommandTests(TestCase):
    def test_creates_the_tournament_and_requested_number_of_teams(self):
        out = StringIO()
        call_command("seed_demo", teams=3, stdout=out)

        tournament = Tournament.objects.get(name="Demo Tournament")
        self.assertEqual(tournament.status, "registration_open")
        self.assertEqual(
            TeamTournamentParticipation.objects.filter(tournament=tournament).count(), 3
        )
        for n in (1, 2, 3):
            captain = User.objects.get(username=f"t{n}p1")
            member = User.objects.get(username=f"t{n}p2")
            team = Team.objects.get(name=f"Team {n}")
            self.assertTrue(
                TeamMembership.objects.filter(team=team, user=captain, role="captain").exists()
            )
            self.assertTrue(
                TeamMembership.objects.filter(team=team, user=member, role="member").exists()
            )

    def test_running_it_twice_does_not_duplicate_anything(self):
        call_command("seed_demo", teams=3, stdout=StringIO())
        first_team_count = Team.objects.count()
        first_user_count = User.objects.count()
        first_participation_count = TeamTournamentParticipation.objects.count()

        call_command("seed_demo", teams=3, stdout=StringIO())

        self.assertEqual(Team.objects.count(), first_team_count)
        self.assertEqual(User.objects.count(), first_user_count)
        self.assertEqual(TeamTournamentParticipation.objects.count(), first_participation_count)

    def test_respects_the_tournament_and_teams_and_password_options(self):
        call_command("seed_demo", tournament="Custom Cup", teams=1, password="custom-pass-1", stdout=StringIO())

        tournament = Tournament.objects.get(name="Custom Cup")
        self.assertEqual(TeamTournamentParticipation.objects.filter(tournament=tournament).count(), 1)
        captain = User.objects.get(username="t1p1")
        self.assertTrue(check_password("custom-pass-1", captain.password))

    def test_re_running_resets_the_password_to_the_given_default(self):
        """set_password/save run unconditionally on every user, found or
        created -- inherited from the original script. That's the point of a
        demo seeder: the known credentials always work, even if someone
        changed them poking around in a local dev database."""
        call_command("seed_demo", teams=1, password="first-pass-1", stdout=StringIO())
        captain = User.objects.get(username="t1p1")
        captain.set_password("changed-by-someone-else-1")
        captain.save()

        call_command("seed_demo", teams=1, password="first-pass-1", stdout=StringIO())

        captain.refresh_from_db()
        self.assertTrue(check_password("first-pass-1", captain.password))
