"""Format-behaviour guards.

These pin what each format actually generates, so the generator, the slot
estimate and the user-facing label cannot drift apart again.
"""
from django.contrib.auth.models import User
from django.test import TestCase

from core.models import Team, TeamMembership, TeamTournamentParticipation, Tournament
from core.scheduling import estimate_required_matches, generate_fixtures


def build_tournament(fmt, team_count, name="F"):
    tournament = Tournament.objects.create(
        name=name, format=fmt, players_per_team=1, default_match_duration=30
    )
    for i in range(1, team_count + 1):
        team = Team.objects.create(name=f"{name}-T{i}")
        TeamTournamentParticipation.objects.create(
            team=team, tournament=tournament, status="active", seed=i
        )
        user = User.objects.create_user(
            username=f"{name}-u{i}", password="Regression-Pass-1"
        )
        TeamMembership.objects.create(team=team, user=user, role="captain")
    return tournament


class DoubleEliminationHonestyTests(TestCase):
    """The format generates a winners bracket only. Until a real losers bracket
    exists, every part of the system must say so consistently."""

    def test_no_losers_bracket_is_generated(self):
        tournament = build_tournament("double_elimination", 8, name="DE")
        generate_fixtures(tournament)

        self.assertEqual(
            tournament.matches.filter(bracket_type="losers").count(), 0,
            "if a losers bracket is now generated, T-4.4 Option B has landed — "
            "update this test, estimate_required_matches and the format label",
        )
        self.assertEqual(tournament.matches.filter(bracket_type="winners").count(), 7)

    def test_slot_estimate_matches_what_is_generated(self):
        """Reserving 2n-2 made _validate_tournament_ready demand roughly double
        the availability that would ever be used."""
        tournament = build_tournament("double_elimination", 8, name="DE2")
        generate_fixtures(tournament)

        self.assertEqual(
            estimate_required_matches(tournament, team_count=8),
            tournament.matches.exclude(bracket_type="third_place").count(),
        )

    def test_label_does_not_promise_a_losers_bracket(self):
        tournament = build_tournament("double_elimination", 2, name="DE3")
        label = tournament.get_format_display().lower()
        self.assertIn("not yet implemented", label)


class SingleEliminationEstimateTests(TestCase):
    def test_knockout_estimate_matches_generation(self):
        tournament = build_tournament("knockout", 8, name="KO")
        generate_fixtures(tournament)
        self.assertEqual(
            estimate_required_matches(tournament, team_count=8),
            tournament.matches.exclude(bracket_type="third_place").count(),
        )
