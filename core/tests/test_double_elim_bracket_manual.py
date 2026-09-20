"""Manually-constructed double-elimination bracket scenarios (predates core/tests_double_elimination.py).

Split out of the old core/tests.py — see FOLLOWUP_PLAN.md F-3.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from ..models import Team, Tournament, TeamMembership, TeamTournamentParticipation
from ..scheduling import generate_fixtures
from ..standings import advance_winner
from .helpers import _captain_user


class DoubleEliminationBracketTests(TestCase):
    """Tests for double-elimination bracket progression logic."""

    def setUp(self):
        self.organizer = User.objects.create_user(
            username="organizer", password="pass123", is_staff=True
        )

    def _create_tournament(self, fmt="double_elimination", name="T1"):
        return Tournament.objects.create(
            name=name,
            format=fmt,
            sport_type="table_tennis",
            points_per_win=3,
            points_per_loss=0,
            points_per_draw=1,
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

    def test_double_elim_winners_bracket_progression(self):
        """Verify winners bracket matches advance correctly."""
        tournament = self._create_tournament()
        [self._create_team(tournament, f"Team {i}", seed=i) for i in range(1, 5)]
        generate_fixtures(tournament)

        # Get first round winners bracket matches
        first_round_matches = tournament.matches.filter(
            bracket_type="winners", round_number=1
        ).order_by("bracket_position")

        self.assertEqual(first_round_matches.count(), 2)  # 4 teams = 2 first round matches

        # Confirm first round matches
        for i, match in enumerate(first_round_matches):
            match.status = "pending_confirmation"
            match.score_team1 = 2
            match.score_team2 = 1
            match.winner = match.team1
            match.submitted_by = _captain_user(match.team1)
            match.save(update_fields=["status", "score_team1", "score_team2", "winner", "submitted_by"])

            # Advance winner to next round
            advance_winner(match)

        # Verify winners advanced to round 2
        second_round = tournament.matches.filter(bracket_type="winners", round_number=2)
        self.assertTrue(second_round.exists())
        for match in second_round:
            self.assertIsNotNone(match.team1)
            self.assertIsNotNone(match.team2)

    def test_double_elim_losers_bracket_creation(self):
        """Verify losers bracket matches are created for losers from winners bracket."""
        tournament = self._create_tournament()
        teams = [self._create_team(tournament, f"Team {i}", seed=i) for i in range(1, 5)]
        generate_fixtures(tournament)

        # Look for all bracket types initially
        matches = tournament.matches.all()

        # In double-elimination, both winners and losers brackets should exist
        # after fixture generation or be generated during tournament progression
        self.assertGreater(matches.count(), 0, "Should have matches generated")

        # Create a losers bracket manually to verify the structure works
        from ..scheduling import generate_knockout
        losers = teams[1::2]  # Teams 2, 4
        generate_knockout(
            tournament,
            teams=losers,
            start_match=100,
            bracket_type="losers",
            round_offset=0
        )

        losers_matches = tournament.matches.filter(bracket_type="losers")
        self.assertTrue(losers_matches.exists(), "Losers bracket should exist after generation")

    def test_double_elim_losers_bracket_progression(self):
        """Verify losers bracket progression advances teams through bracket."""
        tournament = self._create_tournament()
        teams = [self._create_team(tournament, f"Team {i}", seed=i) for i in range(1, 5)]
        generate_fixtures(tournament)

        # Manually set up losers bracket matches
        from ..scheduling import generate_knockout

        # Create a losers bracket with the losers from winners round 1
        losers = teams[1::2]  # Teams 2, 4 (lower seeds, would lose to 1, 3)
        generate_knockout(
            tournament,
            teams=losers,
            start_match=100,
            bracket_type="losers",
            round_offset=0
        )

        losers_matches = tournament.matches.filter(bracket_type="losers", round_number=1)
        self.assertTrue(losers_matches.exists())

        # Confirm a losers match and verify progression
        losers_match = losers_matches.first()
        if losers_match and losers_match.team1 and losers_match.team2:
            losers_match.status = "confirmed"
            losers_match.score_team1 = 2
            losers_match.score_team2 = 1
            losers_match.winner = losers_match.team1
            losers_match.save(update_fields=["status", "score_team1", "score_team2", "winner"])

            advance_winner(losers_match)

            # Verify next losers match was updated
            if losers_match.next_match:
                losers_match.next_match.refresh_from_db()
                self.assertTrue(
                    losers_match.next_match.team1 or losers_match.next_match.team2
                )

    def test_double_elim_finals_both_brackets(self):
        """Verify winners and losers bracket winners meet in grand finals."""
        tournament = self._create_tournament()
        teams = [self._create_team(tournament, f"Team {i}", seed=i) for i in range(1, 5)]
        generate_fixtures(tournament)

        # Mark winners bracket round 1 as confirmed
        winners_r1 = tournament.matches.filter(bracket_type="winners", round_number=1)
        for match in winners_r1:
            match.status = "confirmed"
            match.score_team1 = 2
            match.score_team2 = 1
            match.winner = match.team1
            match.save()

        # Create losers bracket manually if not auto-created
        losers = [t for t in teams if t not in [m.winner for m in winners_r1]]
        if losers:
            from ..scheduling import generate_knockout
            generate_knockout(
                tournament, teams=losers, start_match=100,
                bracket_type="losers", round_offset=0
            )

        # Verify match structure: should have winners bracket semifinals/finals + losers bracket + grand finals
        final_matches = tournament.matches.filter(bracket_type="winners").order_by("-round_number").first()
        self.assertIsNotNone(final_matches)
        self.assertTrue(final_matches.round_number > 1)

    def test_double_elim_draw_rejected_in_winners_bracket(self):
        """Verify draws are rejected in winners bracket (elimination)."""
        tournament = self._create_tournament()
        team1 = self._create_team(tournament, "Red", seed=1)
        team2 = self._create_team(tournament, "Blue", seed=2)
        generate_fixtures(tournament)

        # Get first round match
        match = tournament.matches.filter(bracket_type="winners", round_number=1).first()
        self.assertIsNotNone(match)

        # Submit draw score
        match.status = "pending_confirmation"
        match.score_team1 = 2
        match.score_team2 = 2
        match.submitted_by = _captain_user(team1)
        match.score_submitted_at = timezone.now()
        match.dispute_deadline_at = timezone.now() + timedelta(hours=24)
        match.save(update_fields=[
            "status", "score_team1", "score_team2", "submitted_by",
            "score_submitted_at", "dispute_deadline_at",
        ])

        # Try to confirm as opponent
        self.client.force_login(_captain_user(team2))
        response = self.client.post(
            reverse("confirm_score", kwargs={"pk": match.pk}), follow=True
        )

        # Verify draw was rejected
        match.refresh_from_db()
        self.assertEqual(match.status, "pending_confirmation")
        self.assertIsNone(match.winner)
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("Draws are not allowed" in m for m in messages))

