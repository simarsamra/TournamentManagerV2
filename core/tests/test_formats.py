"""Formats beyond round robin and single-elimination knockout.

Split out of the old core/tests.py — see FOLLOWUP_PLAN.md F-3.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from ..models import Team, Tournament, TeamMembership, TeamTournamentParticipation
from ..scheduling import generate_fixtures
from ..scheduling import generate_consolation_if_ready
from ..forms import TournamentForm


class AdditionalFormatSupportTests(TestCase):
    def _create_tournament(self, fmt, name="Format Test"):
        return Tournament.objects.create(
            name=name,
            format=fmt,
            sport_type="table_tennis",
            points_per_win=3,
            points_per_loss=0,
            points_per_draw=1,
            teams_per_group_advance=1,
            num_groups=2,
            default_match_duration=30,
        )

    def _create_team(self, tournament, team_name, username=None, seed=0):
        username = username or team_name.lower().replace(" ", "_")
        user = User.objects.create_user(username=username, password="pass123")
        team, _ = Team.objects.get_or_create(name=team_name)
        TeamTournamentParticipation.objects.get_or_create(
            team=team, tournament=tournament, defaults={"status": "active", "seed": seed}
        )
        TeamMembership.objects.get_or_create(team=team, user=user, defaults={"role": "captain"})
        return team

    def test_new_format_choices_are_valid_in_form(self):
        base = {
            "name": "F",
            "sport_type": "table_tennis",
            "players_per_team": 1,
            "points_per_win": 3,
            "points_per_loss": 0,
            "points_per_draw": 1,
            "num_groups": 2,
            "teams_per_group_advance": 1,
            "withdrawal_policy": "forfeit",
            "default_match_duration": 30,
        }

        for fmt in ("double_round_robin", "consolation"):
            data = dict(base)
            data["name"] = fmt
            data["format"] = fmt
            form = TournamentForm(data=data)
            self.assertTrue(form.is_valid(), f"Expected format '{fmt}' to be valid")

    def test_double_round_robin_generates_home_and_away_fixtures(self):
        tournament = self._create_tournament(fmt="double_round_robin", name="DRR")
        for i in range(1, 5):
            self._create_team(tournament, f"Team {i}", seed=i)

        generate_fixtures(tournament)

        # For 4 teams, each pair plays twice => 4 * 3 = 12 matches.
        self.assertEqual(tournament.matches.count(), 12)

        # Ensure each pairing appears in both directions.
        team1 = Team.objects.get(name="Team 1")
        team2 = Team.objects.get(name="Team 2")
        self.assertTrue(tournament.matches.filter(team1=team1, team2=team2).exists())
        self.assertTrue(tournament.matches.filter(team1=team2, team2=team1).exists())

    def test_consolation_generates_main_bracket_on_start(self):
        tournament = self._create_tournament(fmt="consolation", name="Consolation Main")
        for i in range(1, 5):
            self._create_team(tournament, f"Team {i}", seed=i)

        generate_fixtures(tournament)

        # Main single-elim bracket exists immediately.
        self.assertEqual(tournament.matches.filter(bracket_type="winners").count(), 3)
        # Consolation bracket should not exist before round 1 completes.
        self.assertFalse(tournament.matches.filter(bracket_type="consolation").exists())

    def test_consolation_generated_after_first_round_completion(self):
        tournament = self._create_tournament(fmt="consolation", name="Consolation Dynamic")
        for i in range(1, 5):
            self._create_team(tournament, f"Team {i}", seed=i)

        generate_fixtures(tournament)

        first_round = tournament.matches.filter(bracket_type="winners", round_number=1)
        self.assertEqual(first_round.count(), 2)

        for match in first_round:
            match.status = "confirmed"
            match.score_team1 = 2
            match.score_team2 = 1
            match.winner = match.team1
            match.save(update_fields=["status", "score_team1", "score_team2", "winner"])

        generated = generate_consolation_if_ready(tournament)
        self.assertTrue(generated)
        self.assertTrue(tournament.matches.filter(bracket_type="consolation").exists())


# ---------------------------------------------------------------------------
# Tournament Completion Tests
# ---------------------------------------------------------------------------

