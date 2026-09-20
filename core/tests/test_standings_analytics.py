"""Public standings/knockout display and the analytics/what-if views.

Split out of the old core/tests.py (UXAndLogicRegressionTests) — see
FOLLOWUP_PLAN.md F-3.
"""
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from ..models import Match, Player
from ..scheduling import generate_fixtures

from .helpers import UXRegressionTestCase


class PublicStandingsAndAnalyticsTests(UXRegressionTestCase):

    def test_public_hybrid_standings_includes_bracket_after_group_stage(self):
        tournament = self._create_tournament(fmt="hybrid")
        [self._create_team(tournament, f"Team {i}", seed=i) for i in range(1, 5)]
        generate_fixtures(tournament)

        # Complete group stage with non-draw confirmed scores.
        for match in tournament.matches.filter(group__gt=""):
            match.status = "confirmed"
            match.score_team1 = 3
            match.score_team2 = 1
            match.winner = match.team1
            match.save(update_fields=["status", "score_team1", "score_team2", "winner"])

        # Trigger knockout generation from completed group stage.
        from ..standings import check_group_stage_complete

        check_group_stage_complete(tournament)

        response = self.client.get(reverse("public_standings"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("bracket", response.context)
        self.assertTrue(response.context["bracket"])

    def test_knockout_standings_uses_quarter_semi_final_labels(self):
        tournament = self._create_tournament(fmt="knockout", name="Knockout Labels")
        for i in range(1, 9):
            self._create_team(tournament, f"Team {i}", seed=i)
        generate_fixtures(tournament)

        self.client.force_login(self.organizer)
        session = self.client.session
        session["selected_tournament_id"] = tournament.pk
        session.save()

        response = self.client.get(reverse("standings"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Quarter-finals")
        self.assertContains(response, "Semi-finals")
        self.assertContains(response, "Final")

    def test_knockout_bracket_shows_zero_score_in_standings(self):
        tournament = self._create_tournament(fmt="knockout", name="Knockout Zero Score")
        team1 = self._create_team(tournament, "Team 1", seed=1)
        team2 = self._create_team(tournament, "Team 2", seed=2)
        generate_fixtures(tournament)
        match = tournament.matches.get(team1=team1, team2=team2)
        match.status = "confirmed"
        match.score_team1 = 0
        match.score_team2 = 2
        match.winner = team2
        match.save(update_fields=["status", "score_team1", "score_team2", "winner"])

        self.client.force_login(self.organizer)
        session = self.client.session
        session["selected_tournament_id"] = tournament.pk
        session.save()

        response = self.client.get(reverse("standings"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="bracket-score">0</span>', html=False)
        self.assertContains(response, 'class="bracket-score">2</span>', html=False)

    def test_public_knockout_bracket_shows_zero_score(self):
        tournament = self._create_tournament(fmt="knockout", name="Public Knockout Zero Score")
        team1 = self._create_team(tournament, "Public Team 1", seed=1)
        team2 = self._create_team(tournament, "Public Team 2", seed=2)
        generate_fixtures(tournament)
        match = tournament.matches.get(team1=team1, team2=team2)
        match.status = "confirmed"
        match.score_team1 = 0
        match.score_team2 = 3
        match.winner = team2
        match.save(update_fields=["status", "score_team1", "score_team2", "winner"])

        response = self.client.get(reverse("public_standings"), {"tournament": tournament.pk})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="bracket-score">0</span>', html=False)
        self.assertContains(response, 'class="bracket-score">3</span>', html=False)

    def test_analytics_exposes_head_to_head_form_and_prep_context(self):
        tournament = self._create_tournament(fmt="round_robin", name="Analytics Context")
        tournament.status = "active"
        tournament.save(update_fields=["status"])
        team_a = self._create_team(tournament, "Alpha", seed=1)
        team_b = self._create_team(tournament, "Beta", seed=2)
        team_c = self._create_team(tournament, "Gamma", seed=3)
        Player.objects.create(team=team_c, name="Gamma Player 1")
        Player.objects.create(team=team_c, name="Gamma Player 2")
        Player.objects.create(team=team_c, name="Gamma Player 3")

        Match.objects.create(
            tournament=tournament,
            match_number=1,
            team1=team_a,
            team2=team_b,
            status="confirmed",
            score_team1=3,
            score_team2=1,
            winner=team_a,
        )
        Match.objects.create(
            tournament=tournament,
            match_number=2,
            team1=team_b,
            team2=team_a,
            status="confirmed",
            score_team1=0,
            score_team2=2,
            winner=team_a,
        )
        Match.objects.create(
            tournament=tournament,
            match_number=3,
            team1=team_c,
            team2=team_b,
            status="confirmed",
            score_team1=2,
            score_team2=1,
            winner=team_c,
        )
        Match.objects.create(
            tournament=tournament,
            match_number=4,
            team1=team_a,
            team2=team_c,
            status="upcoming",
            scheduled_time=timezone.now() + timedelta(days=1),
        )

        self.client.force_login(self.organizer)
        response = self.client.get(reverse("analytics"), {
            "h2h_team1": team_a.pk,
            "h2h_team2": team_b.pk,
            "form_team": team_a.pk,
            "form_window": 3,
            "prep_team": team_a.pk,
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["h2h_card"]["total_matches"], 2)
        self.assertEqual(response.context["h2h_card"]["team1_wins"], 2)
        self.assertEqual(response.context["h2h_card"]["team2_wins"], 0)
        self.assertEqual(len(response.context["rolling_form_rows"]), 2)
        self.assertEqual(response.context["rolling_form_rows"][-1]["result"], "W")
        self.assertEqual(response.context["next_opponent_prep"]["opponent"], team_c)
        self.assertEqual(len(response.context["next_opponent_prep"]["opponent_key_players"]), 3)

    def test_analytics_what_if_simulator_projects_points(self):
        tournament = self._create_tournament(fmt="round_robin", name="What If")
        tournament.status = "active"
        tournament.save(update_fields=["status"])
        team_a = self._create_team(tournament, "Delta", seed=1)
        team_b = self._create_team(tournament, "Epsilon", seed=2)

        upcoming = Match.objects.create(
            tournament=tournament,
            match_number=1,
            team1=team_a,
            team2=team_b,
            status="upcoming",
            scheduled_time=timezone.now() + timedelta(days=1),
        )

        self.client.force_login(self.organizer)
        response = self.client.get(reverse("analytics"), {
            f"sim_{upcoming.pk}": "team1",
        })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["simulator_enabled"])
        sim_rows = response.context["simulated_standings"]
        row_a = next(r for r in sim_rows if r["team"] == team_a)
        row_b = next(r for r in sim_rows if r["team"] == team_b)
        self.assertEqual(row_a["point_change"], tournament.points_per_win)
        self.assertEqual(row_b["point_change"], tournament.points_per_loss)
        self.assertEqual(
            next(m for m in response.context["simulator_matches"] if m.pk == upcoming.pk).selected_outcome,
            "team1",
        )
