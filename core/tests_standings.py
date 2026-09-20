"""Head-to-head tiebreaker and standings determinism (T-4.5).

tiebreaker_order defaults to ["game_diff", "games_won", "head_to_head"], so
every tournament shipped with a configured tiebreaker that did nothing:

    elif tb == "head_to_head":
        key.append(0)  # Simplified; would need pairwise comparison

Teams level on points, game difference and games won therefore came out in
dict-iteration order.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from core.models import (
    Match, OrganizerProfile, Team, TeamTournamentParticipation, Tournament,
)
from core.standings import (
    _head_to_head_points, calculate_standings,
)


class HeadToHeadTests(TestCase):
    def setUp(self):
        organizer = User.objects.create_user("h2h_org", password="Standings-Pass-1")
        OrganizerProfile.objects.filter(user=organizer).update(verified=True)
        self.tournament = Tournament.objects.create(
            name="H2H Open", format="round_robin", players_per_team=1,
            start_date=timezone.localdate(), created_by=organizer,
            points_per_win=3, points_per_draw=1, points_per_loss=0,
        )
        self.teams = {}
        for name in ("Alpha", "Bravo", "Charlie"):
            team = Team.objects.create(name=name)
            TeamTournamentParticipation.objects.create(
                team=team, tournament=self.tournament, status="active"
            )
            self.teams[name] = team

    def _play(self, home, away, score_home, score_away, status="confirmed"):
        self._match_number = getattr(self, "_match_number", 0) + 1
        match = Match.objects.create(
            tournament=self.tournament, match_number=self._match_number,
            team1=self.teams[home], team2=self.teams[away],
            score_team1=score_home, score_team2=score_away,
            status=status,
        )
        if score_home > score_away:
            match.winner = self.teams[home]
        elif score_away > score_home:
            match.winner = self.teams[away]
        match.save()
        return match

    def _ranked_names(self):
        return [row["team"].name for row in calculate_standings(self.tournament)]

    def test_the_winner_of_the_direct_match_ranks_higher(self):
        """Alpha and Bravo finish level on points, game difference and games
        won. Alpha beat Bravo, so Alpha must rank first."""
        self._play("Alpha", "Bravo", 3, 1)      # Alpha +2
        self._play("Bravo", "Charlie", 3, 0)    # Bravo +3
        self._play("Alpha", "Charlie", 1, 2)    # Alpha -1  -> Alpha +1 total
        # Alpha: 3 pts, won 4 lost 3, diff +1
        # Bravo: 3 pts, won 4 lost 3, diff +1
        standings = {row["team"].name: row for row in calculate_standings(self.tournament)}
        self.assertEqual(standings["Alpha"]["points"], standings["Bravo"]["points"])
        self.assertEqual(standings["Alpha"]["game_diff"], standings["Bravo"]["game_diff"])
        self.assertEqual(standings["Alpha"]["games_won"], standings["Bravo"]["games_won"])

        self.assertLess(standings["Alpha"]["rank"], standings["Bravo"]["rank"])

    def test_the_tiebreak_follows_the_result_not_the_row_order(self):
        """Same fixtures, reversed direct result. The ranking must flip."""
        self._play("Bravo", "Alpha", 3, 1)
        self._play("Alpha", "Charlie", 3, 0)
        self._play("Bravo", "Charlie", 1, 2)

        standings = {row["team"].name: row for row in calculate_standings(self.tournament)}
        self.assertEqual(standings["Alpha"]["points"], standings["Bravo"]["points"])
        self.assertLess(standings["Bravo"]["rank"], standings["Alpha"]["rank"])

    def test_scalar_tiebreakers_still_win_over_head_to_head(self):
        """Head-to-head applies only after game_diff and games_won, which is
        what the configured tiebreaker_order means."""
        # All three end on 3 points (one win, one loss each).
        self._play("Bravo", "Alpha", 3, 2)      # Alpha lost the direct match
        self._play("Alpha", "Charlie", 9, 0)    # but has a far better diff
        self._play("Charlie", "Bravo", 3, 2)
        # Alpha  diff +8, Bravo diff 0, Charlie diff -8

        standings = {row["team"].name: row for row in calculate_standings(self.tournament)}
        self.assertGreater(standings["Alpha"]["game_diff"], standings["Bravo"]["game_diff"])
        self.assertLess(standings["Alpha"]["rank"], standings["Bravo"]["rank"])

    def test_a_drawn_direct_match_leaves_the_order_deterministic(self):
        self._play("Alpha", "Bravo", 2, 2)
        first = self._ranked_names()
        self.assertEqual(first, self._ranked_names())

    def test_standings_are_deterministic_across_calls(self):
        """With nothing to separate them at all, repeated calls must agree."""
        order = self._ranked_names()
        for _ in range(5):
            self.assertEqual(self._ranked_names(), order)

    def test_ranks_are_a_dense_sequence(self):
        self._play("Alpha", "Bravo", 3, 1)
        ranks = [row["rank"] for row in calculate_standings(self.tournament)]
        self.assertEqual(ranks, list(range(1, len(ranks) + 1)))


class HeadToHeadPointsTests(TestCase):
    def setUp(self):
        self.tournament = Tournament.objects.create(
            name="H2H Points", format="round_robin", players_per_team=1,
            start_date=timezone.localdate(),
            points_per_win=3, points_per_draw=1, points_per_loss=0,
        )
        self.teams = {}
        for name in ("A", "B", "C"):
            team = Team.objects.create(name=name)
            TeamTournamentParticipation.objects.create(
                team=team, tournament=self.tournament, status="active"
            )
            self.teams[name] = team

    def _ids(self, *names):
        return [self.teams[n].id for n in names]

    def test_only_matches_between_the_named_teams_are_counted(self):
        Match.objects.create(
            tournament=self.tournament, match_number=1, team1=self.teams["A"], team2=self.teams["B"],
            score_team1=3, score_team2=0, status="confirmed", winner=self.teams["A"],
        )
        Match.objects.create(
            tournament=self.tournament, match_number=2, team1=self.teams["A"], team2=self.teams["C"],
            score_team1=3, score_team2=0, status="confirmed", winner=self.teams["A"],
        )

        points = _head_to_head_points(self.tournament, self._ids("A", "B"))
        self.assertEqual(points[self.teams["A"].id], 3)  # the C win is excluded
        self.assertEqual(points[self.teams["B"].id], 0)

    def test_a_draw_awards_both_teams_the_draw_points(self):
        Match.objects.create(
            tournament=self.tournament, match_number=3, team1=self.teams["A"], team2=self.teams["B"],
            score_team1=2, score_team2=2, status="confirmed",
        )
        points = _head_to_head_points(self.tournament, self._ids("A", "B"))
        self.assertEqual(points[self.teams["A"].id], 1)
        self.assertEqual(points[self.teams["B"].id], 1)

    def test_forfeits_count_for_the_winner(self):
        Match.objects.create(
            tournament=self.tournament, match_number=4, team1=self.teams["A"], team2=self.teams["B"],
            status="forfeited", winner=self.teams["B"],
        )
        points = _head_to_head_points(self.tournament, self._ids("A", "B"))
        self.assertEqual(points[self.teams["B"].id], 3)
        self.assertEqual(points[self.teams["A"].id], 0)

    def test_unscored_confirmed_matches_are_ignored(self):
        Match.objects.create(
            tournament=self.tournament, match_number=5, team1=self.teams["A"], team2=self.teams["B"],
            status="confirmed",
        )
        points = _head_to_head_points(self.tournament, self._ids("A", "B"))
        self.assertEqual(set(points.values()), {0})

    def test_upcoming_matches_are_ignored(self):
        Match.objects.create(
            tournament=self.tournament, match_number=6, team1=self.teams["A"], team2=self.teams["B"],
            score_team1=3, score_team2=0, status="upcoming", winner=self.teams["A"],
        )
        points = _head_to_head_points(self.tournament, self._ids("A", "B"))
        self.assertEqual(set(points.values()), {0})
