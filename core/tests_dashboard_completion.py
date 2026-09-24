"""dashboard_view's post-completion context: tournament_champion and the two
runner-up slots it adds once a tournament is marked completed.

Converted from scripts/verify_completion_feature.py (F-6) -- that script
dumped this context to stdout for whatever completed tournaments happened to
exist in a real database; nothing here was previously asserted anywhere in
the suite.
"""
from django.contrib.auth.models import User
from django.test import TestCase

from core.models import Match, Team, TeamMembership, TeamTournamentParticipation, Tournament


class DashboardCompletionContextTests(TestCase):
    def _create_tournament(self, fmt, name):
        return Tournament.objects.create(
            name=name, format=fmt, sport_type="table_tennis", status="completed",
            points_per_win=3, points_per_loss=0, points_per_draw=1,
            players_per_team=1, default_match_duration=30,
        )

    def _add_team(self, tournament, name):
        team = Team.objects.create(name=name)
        TeamTournamentParticipation.objects.create(
            team=team, tournament=tournament, status="active"
        )
        user = User.objects.create_user(username=f"{name.lower()}_captain", password="pass123")
        TeamMembership.objects.create(team=team, user=user, role="captain")
        return team, user

    def _play(self, tournament, number, home, away, score_home, score_away):
        match = Match.objects.create(
            tournament=tournament, match_number=number,
            team1=home, team2=away, score_team1=score_home, score_team2=score_away,
            status="confirmed",
        )
        match.winner = home if score_home > score_away else away
        match.save(update_fields=["winner"])
        return match

    def _dashboard_context(self, user, tournament):
        self.client.force_login(user)
        session = self.client.session
        session["selected_tournament_id"] = tournament.pk
        session.save()
        return self.client.get("/dashboard/").context

    def test_round_robin_champion_and_runner_ups_come_from_standings(self):
        tournament = self._create_tournament("round_robin", "RR Completed")
        gold, gold_user = self._add_team(tournament, "Gold")
        silver, _ = self._add_team(tournament, "Silver")
        bronze, _ = self._add_team(tournament, "Bronze")

        self._play(tournament, 1, gold, silver, 3, 0)
        self._play(tournament, 2, gold, bronze, 3, 0)
        self._play(tournament, 3, silver, bronze, 3, 0)

        context = self._dashboard_context(gold_user, tournament)

        self.assertEqual(context["tournament_champion"], gold)
        self.assertEqual(context["tournament_runner_up_1"], silver)
        self.assertEqual(context["tournament_runner_up_2"], bronze)

    def test_hybrid_champion_comes_from_the_bracket_final_not_group_stage_standings(self):
        """Hybrid: the group stage decides who advances, not who wins overall.
        tournament.champion (set when the knockout final is confirmed) must
        win out over standings[0], which only reflects the group stage."""
        tournament = self._create_tournament("hybrid", "Hybrid Completed")
        # Group-stage standings would rank Runner-Up first (more group points),
        # but Champion beat them in the bracket final.
        champion, champion_user = self._add_team(tournament, "Champion")
        runner_up, _ = self._add_team(tournament, "RunnerUp")
        group_match = self._play(tournament, 1, runner_up, champion, 3, 0)  # group stage: RunnerUp "wins"
        # dashboard_view's bracket-final query matches on bracket_type="winners"
        # and group="" (both Match defaults) -- without a real group letter here,
        # this group-stage match would itself satisfy that query and get
        # mistaken for the final.
        group_match.group = "A"
        group_match.save(update_fields=["group"])

        tournament.champion = champion
        tournament.save(update_fields=["champion"])

        final = Match.objects.create(
            tournament=tournament, match_number=2,
            team1=champion, team2=runner_up, score_team1=3, score_team2=1,
            status="confirmed", bracket_type="winners", group="", round_number=1,
        )
        final.winner = champion
        final.save(update_fields=["winner"])

        context = self._dashboard_context(champion_user, tournament)

        self.assertEqual(context["tournament_champion"], champion)
        self.assertEqual(context["tournament_runner_up_1"], runner_up)

    def test_knockout_completed_dashboard_uses_tournament_champion_with_no_runner_ups(self):
        """Bracket formats have no group-stage standings to fall back on --
        only tournament.champion is available, and the runner-up slots stay
        empty rather than guessing at 2nd/3rd place."""
        tournament = self._create_tournament("knockout", "Knockout Completed")
        champion, champion_user = self._add_team(tournament, "Champion")
        tournament.champion = champion
        tournament.save(update_fields=["champion"])

        context = self._dashboard_context(champion_user, tournament)

        self.assertEqual(context["tournament_champion"], champion)
        self.assertIsNone(context["tournament_runner_up_1"])
        self.assertIsNone(context["tournament_runner_up_2"])

    def test_an_active_tournament_has_no_completion_context(self):
        """The celebration context only makes sense once a tournament is
        actually completed; it must not appear for one still in progress."""
        tournament = self._create_tournament("round_robin", "Still Active")
        tournament.status = "active"
        tournament.save(update_fields=["status"])
        team, user = self._add_team(tournament, "Solo")

        context = self._dashboard_context(user, tournament)

        self.assertNotIn("tournament_champion", context)
