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


class DoubleEliminationTests(TestCase):
    """The format generates a winners bracket, a losers bracket and a grand
    final. Generator, slot estimate and user-facing label must stay in step.

    These replace the guards that pinned the honest single-elimination
    downgrade of T-4.4. That downgrade existed because no losers bracket was
    generated; now one is, and the failure message those guards carried said
    exactly this should happen.
    """

    def test_a_losers_bracket_is_generated(self):
        tournament = build_tournament("double_elimination", 8, name="DE")
        generate_fixtures(tournament)

        self.assertEqual(tournament.matches.filter(bracket_type="winners").count(), 7)
        self.assertEqual(tournament.matches.filter(bracket_type="losers").count(), 6)
        self.assertEqual(tournament.matches.filter(bracket_type="grand_final").count(), 2)

    def test_slot_estimate_matches_what_is_generated(self):
        """The estimate and the generator drifting apart is what made
        _validate_tournament_ready falsely block organizers before."""
        tournament = build_tournament("double_elimination", 8, name="DE2")
        generate_fixtures(tournament)

        self.assertEqual(
            estimate_required_matches(tournament, team_count=8),
            tournament.matches.exclude(bracket_type="third_place").count(),
        )

    def test_slot_estimate_matches_generation_without_bracket_reset(self):
        tournament = build_tournament("double_elimination", 8, name="DE4")
        tournament.enable_bracket_reset = False
        tournament.save(update_fields=["enable_bracket_reset"])
        generate_fixtures(tournament)

        self.assertEqual(
            estimate_required_matches(tournament, team_count=8),
            tournament.matches.exclude(bracket_type="third_place").count(),
        )

    def test_label_no_longer_disclaims_the_losers_bracket(self):
        tournament = build_tournament("double_elimination", 2, name="DE3")
        label = tournament.get_format_display().lower()
        self.assertEqual(label, "double elimination")
        self.assertNotIn("not yet implemented", label)


class SingleEliminationEstimateTests(TestCase):
    def test_knockout_estimate_matches_generation(self):
        tournament = build_tournament("knockout", 8, name="KO")
        generate_fixtures(tournament)
        self.assertEqual(
            estimate_required_matches(tournament, team_count=8),
            tournament.matches.exclude(bracket_type="third_place").count(),
        )
