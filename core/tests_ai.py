"""Tests for AI_ANALYTICS_PLAN.md (question answering over the analytics)."""
from django.contrib.auth.models import User
from django.test import TestCase

from core import analytics
from core.models import (
    Match, OrganizerProfile, Team, TeamMembership, TeamTournamentParticipation, Tournament,
)
from core.standings import calculate_standings


def _make_organizer(username):
    user = User.objects.create_user(username=username, password="Regression-Pass-1")
    OrganizerProfile.objects.filter(user=user).update(verified=True)
    user.refresh_from_db()
    return user


class AnalyticsFunctionTests(TestCase):
    """AI-1: the analytics calculations are callable without a request, which
    is how the AI layer will use them."""

    def setUp(self):
        self.organizer = _make_organizer("org")
        self.tournament = Tournament.objects.create(
            name="T", format="round_robin", status="active", players_per_team=1,
            created_by=self.organizer,
        )
        self.a, self.b, self.c = (Team.objects.create(name=n) for n in ("Aces", "Bolts", "Comets"))
        for team in (self.a, self.b, self.c):
            TeamTournamentParticipation.objects.create(
                team=team, tournament=self.tournament, status="active"
            )
        Match.objects.create(
            tournament=self.tournament, match_number=1, team1=self.a, team2=self.b,
            score_team1=3, score_team2=1, winner=self.a, status="confirmed",
        )
        self.upcoming = Match.objects.create(
            tournament=self.tournament, match_number=2, team1=self.b, team2=self.c,
            status="upcoming",
        )

    def test_can_view_analytics_follows_a1(self):
        other = _make_organizer("other")
        player = User.objects.create_user(username="p", password="Regression-Pass-1")
        TeamMembership.objects.create(team=self.c, user=player, role="captain")
        self.assertEqual(analytics.can_view_analytics(self.organizer, self.tournament), (True, True))
        self.assertEqual(analytics.can_view_analytics(player, self.tournament), (True, False))
        self.assertEqual(analytics.can_view_analytics(other, self.tournament), (False, False))

    def test_head_to_head_needs_two_different_teams(self):
        self.assertIsNone(analytics.head_to_head(self.tournament, self.a, self.a))
        self.assertIsNone(analytics.head_to_head(self.tournament, self.a, None))
        card = analytics.head_to_head(self.tournament, self.a, self.b)
        self.assertEqual((card["total_matches"], card["team1_wins"], card["team2_wins"]), (1, 1, 0))

    def test_rolling_form_and_prep_for_a_named_team(self):
        self.assertEqual(analytics.rolling_form(self.tournament, None, 5), [])
        rows = analytics.rolling_form(self.tournament, self.b, 5)
        self.assertEqual([(r["result"], r["opponent"]) for r in rows], [("L", "Aces")])
        prep = analytics.next_opponent_prep(self.tournament, self.b)
        self.assertEqual((prep["opponent"], prep["opponent_record"]["wins"]), (self.c, 0))
        self.assertIsNone(analytics.next_opponent_prep(self.tournament, self.a))

    def test_simulate_with_picks_by_match_pk(self):
        standings = calculate_standings(self.tournament)
        analytics.label_standings(self.tournament, standings)
        offered, total = analytics.simulator_matches(self.tournament)
        self.assertEqual(([m.pk for m in offered], total), ([self.upcoming.pk], 1))
        self.assertEqual(analytics.simulate(self.tournament, standings, [], {}), (None, False))
        simulated, has_choices = analytics.simulate(
            self.tournament, standings, offered, {self.upcoming.pk: "team2"}
        )
        self.assertTrue(has_choices)
        comets = next(row for row in simulated if row["team"] == self.c)
        self.assertEqual((comets["points"], comets["point_change"]), (3, 3))
        # The real rows are untouched.
        self.assertEqual(next(r for r in standings if r["team"] == self.c)["points"], 0)


class PrepSheetDefaultTests(TestCase):
    """AI-1b: with no prep_team in the URL, the prep sheet fell back to the
    rolling-form team. Since A-12 swaps only the form card over HTMX, the live
    prep card and a reload of the pushed URL then showed different teams."""

    def test_prep_sheet_defaults_to_first_active_team_not_form_team(self):
        organizer = _make_organizer("org")
        tournament = Tournament.objects.create(
            name="T", format="round_robin", status="active", players_per_team=1,
            created_by=organizer,
        )
        aces, bolts = (Team.objects.create(name=n) for n in ("Aces", "Bolts"))
        for team in (aces, bolts):
            TeamTournamentParticipation.objects.create(team=team, tournament=tournament, status="active")
        self.client.force_login(organizer)
        response = self.client.get("/analytics/", {"tournament": tournament.pk, "form_team": bolts.pk})
        self.assertEqual(response.context["form_team"], bolts)
        self.assertEqual(response.context["prep_team"], aces)

    def test_explicit_prep_team_still_wins(self):
        organizer = _make_organizer("org")
        tournament = Tournament.objects.create(
            name="T", format="round_robin", status="active", players_per_team=1,
            created_by=organizer,
        )
        aces, bolts = (Team.objects.create(name=n) for n in ("Aces", "Bolts"))
        for team in (aces, bolts):
            TeamTournamentParticipation.objects.create(team=team, tournament=tournament, status="active")
        self.client.force_login(organizer)
        response = self.client.get("/analytics/", {"tournament": tournament.pk, "prep_team": bolts.pk})
        self.assertEqual(response.context["prep_team"], bolts)
