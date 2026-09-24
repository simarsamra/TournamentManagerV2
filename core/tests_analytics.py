"""Analytics page regressions (ANALYTICS_PLAN.md)."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import (
    AuditLog, Match, OrganizerProfile, Team, TeamMembership,
    TeamTournamentParticipation, Tournament, TournamentIndividualRegistration,
)
from core.standings import calculate_standings
from core.views import _ensure_shadow_team_for_registration


def _make_organizer(username):
    user = User.objects.create_user(username=username, password="Regression-Pass-1")
    OrganizerProfile.objects.filter(user=user).update(verified=True)
    user.refresh_from_db()
    return user


class AnalyticsAndAuditOwnershipTests(TestCase):
    """A-1: analytics_view and audit_log_view gated on _is_organizer alone, so
    any organizer could pick another organizer's tournament with
    ?tournament=<pk> and read its audit trail."""

    SECRET = "owner-only audit detail 7f3a"
    GLOBAL_SECRET = "site-wide login detail 91c2"

    def setUp(self):
        self.owner = _make_organizer("owner")
        self.other = _make_organizer("other")
        self.admin = User.objects.create_superuser(
            username="root", password="Regression-Pass-1", email=""
        )
        self.tournament = Tournament.objects.create(
            name="Owned", format="round_robin", status="active",
            players_per_team=1, created_by=self.owner,
        )
        self.other_tournament = Tournament.objects.create(
            name="Other's", format="round_robin", status="active",
            players_per_team=1, created_by=self.other,
        )
        AuditLog.objects.create(
            user=self.owner, action="tournament_updated", details=self.SECRET,
            tournament=self.tournament,
        )
        AuditLog.objects.create(
            user=self.owner, action="login", details=self.GLOBAL_SECRET,
        )

    def _analytics(self, user, tournament=None):
        self.client.force_login(user)
        return self.client.get(
            "/analytics/", {"tournament": (tournament or self.tournament).pk}
        )

    def _audit(self, user, tournament=None):
        self.client.force_login(user)
        return self.client.get(
            "/audit-log/", {"tournament": (tournament or self.tournament).pk}
        )

    def _enroll(self, user, tournament):
        team = Team.objects.create(name=f"{user.username} team")
        TeamTournamentParticipation.objects.create(
            team=team, tournament=tournament, status="active"
        )
        TeamMembership.objects.create(team=team, user=user, role="captain")

    # -- analytics --

    def test_non_owner_organizer_is_redirected_from_analytics(self):
        response = self._analytics(self.other)
        self.assertEqual(response.status_code, 302)
        self.assertNotContains(self.client.get(response.url), self.SECRET)

    def test_owner_sees_recent_activity(self):
        response = self._analytics(self.owner)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Recent Activity")
        self.assertContains(response, self.SECRET)

    def test_site_admin_sees_recent_activity_for_any_tournament(self):
        response = self._analytics(self.admin)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.SECRET)

    def test_enrolled_non_owner_organizer_sees_analytics_but_not_activity(self):
        self._enroll(self.other, self.tournament)
        response = self._analytics(self.other)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Recent Activity")
        self.assertNotContains(response, self.SECRET)

    def test_legacy_tournament_without_creator_is_managed_by_any_organizer(self):
        legacy = Tournament.objects.create(
            name="Legacy", format="round_robin", status="active",
            players_per_team=1, created_by=None,
        )
        AuditLog.objects.create(action="legacy_event", details="legacy detail", tournament=legacy)
        response = self._analytics(self.other, legacy)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "legacy detail")

    # -- audit log --

    def test_non_owner_organizer_is_redirected_from_audit_log(self):
        response = self._audit(self.other)
        self.assertEqual(response.status_code, 302)

    def test_owner_sees_own_tournament_rows_but_not_global_rows(self):
        response = self._audit(self.owner)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.SECRET)
        self.assertNotContains(response, self.GLOBAL_SECRET)

    def test_owner_does_not_see_other_tournaments_rows(self):
        AuditLog.objects.create(
            user=self.other, action="other_event", details="other tournament detail",
            tournament=self.other_tournament,
        )
        response = self._audit(self.owner)
        self.assertNotContains(response, "other tournament detail")
        # The action filter dropdown is built from visible rows only.
        self.assertNotContains(response, "other_event")

    def test_site_admin_sees_tournament_and_global_rows(self):
        response = self._audit(self.admin)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.SECRET)
        self.assertContains(response, self.GLOBAL_SECRET)

    def test_enrolled_non_owner_organizer_cannot_read_audit_log(self):
        self._enroll(self.other, self.tournament)
        response = self._audit(self.other)
        self.assertEqual(response.status_code, 302)


class TeamPerformanceTests(TestCase):
    """A-2: Team Performance set losses = played - wins (every draw became a
    loss) and ranked by win rate alone (a 1-0 team outranked a 9-1 team)."""

    def setUp(self):
        self.organizer = _make_organizer("org")
        self._match_number = 0

    def _tournament(self, fmt, *names):
        tournament = Tournament.objects.create(
            name=fmt, format=fmt, status="active", players_per_team=1,
            created_by=self.organizer,
        )
        teams = []
        for name in names:
            team = Team.objects.create(name=name)
            TeamTournamentParticipation.objects.create(
                team=team, tournament=tournament, status="active"
            )
            teams.append(team)
        return tournament, teams

    def _play(self, tournament, team1, team2, score1, score2):
        self._match_number += 1
        return Match.objects.create(
            tournament=tournament, match_number=self._match_number,
            team1=team1, team2=team2, score_team1=score1, score_team2=score2,
            status="confirmed", winner=team1 if score1 > score2 else team2 if score2 > score1 else None,
        )

    def _team_stats(self, tournament):
        self.client.force_login(self.organizer)
        response = self.client.get("/analytics/", {"tournament": tournament.pk})
        self.assertEqual(response.status_code, 200)
        return response.context["team_stats"]

    def test_a_draw_is_a_draw_not_a_loss(self):
        tournament, (a, b) = self._tournament("round_robin", "A", "B")
        self._play(tournament, a, b, 2, 2)
        stats = {s["team"].pk: s for s in self._team_stats(tournament)}
        self.assertEqual(
            (stats[a.pk]["played"], stats[a.pk]["wins"], stats[a.pk]["draws"], stats[a.pk]["losses"]),
            (1, 0, 1, 0),
        )
        response = self.client.get("/analytics/", {"tournament": tournament.pk})
        self.assertContains(response, "<th>Draws</th>", html=False)
        # Regular (non-internal) teams keep their link to the team page.
        self.assertContains(response, reverse("team_detail", kwargs={"pk": a.pk}))

    def test_draws_column_hidden_when_there_are_no_draws(self):
        tournament, (a, b) = self._tournament("round_robin", "A", "B")
        self._play(tournament, a, b, 3, 1)
        self.client.force_login(self.organizer)
        response = self.client.get("/analytics/", {"tournament": tournament.pk})
        self.assertNotContains(response, "<th>Draws</th>", html=False)

    def test_many_wins_outrank_a_perfect_single_win_in_a_knockout(self):
        tournament, (strong, lucky, filler) = self._tournament("knockout", "Strong", "Lucky", "Filler")
        for _ in range(9):
            self._play(tournament, strong, filler, 3, 0)
        self._play(tournament, filler, strong, 3, 0)
        self._play(tournament, lucky, filler, 3, 0)
        order = [s["team"].pk for s in self._team_stats(tournament)]
        self.assertLess(order.index(strong.pk), order.index(lucky.pk))

    def test_round_robin_order_matches_standings(self):
        tournament, (a, b, c) = self._tournament("round_robin", "A", "B", "C")
        self._play(tournament, a, b, 1, 0)
        self._play(tournament, b, c, 1, 0)
        self._play(tournament, c, a, 5, 0)
        expected = [row["team"].pk for row in calculate_standings(tournament)]
        self.assertEqual([s["team"].pk for s in self._team_stats(tournament)], expected)

    def test_withdrawn_teams_are_left_out(self):
        tournament, (a, b) = self._tournament("round_robin", "A", "B")
        self._play(tournament, a, b, 1, 0)
        TeamTournamentParticipation.objects.filter(team=b).update(status="withdrawn")
        self.assertEqual([s["team"].pk for s in self._team_stats(tournament)], [a.pk])


class IndividualModeLabelTests(TestCase):
    """A-3: standings rows reached the template without display_label, so an
    individual-registration tournament showed its internal shadow-team names
    (__tm_shadow_...) in Points Overview, and Team Performance linked to
    team pages non-organizers can't open."""

    def setUp(self):
        self.organizer = _make_organizer("org")
        self.tournament = Tournament.objects.create(
            name="IND", format="round_robin", status="active",
            players_per_team=1, registration_mode="individual",
            created_by=self.organizer,
        )
        self.registrations = []
        for username, display_name in (("pa", "Player Alpha"), ("pb", "Player Bravo")):
            user = User.objects.create_user(username=username, password="Regression-Pass-1")
            registration = TournamentIndividualRegistration.objects.create(
                tournament=self.tournament, user=user,
                display_name=display_name, status="active",
            )
            _ensure_shadow_team_for_registration(registration, self.tournament.sport_type)
            registration.refresh_from_db()
            self.registrations.append(registration)
        alpha, bravo = (r.shadow_team for r in self.registrations)
        Match.objects.create(
            tournament=self.tournament, match_number=1, team1=alpha, team2=bravo,
            score_team1=3, score_team2=1, winner=alpha, status="confirmed",
        )

    def _get(self, user):
        self.client.force_login(user)
        response = self.client.get("/analytics/", {"tournament": self.tournament.pk})
        self.assertEqual(response.status_code, 200)
        return response

    def test_no_internal_team_names_in_the_page(self):
        for user in (self.organizer, self.registrations[0].user):
            response = self._get(user)
            self.assertNotContains(response, "__tm_shadow_")
            self.assertContains(response, "Player Alpha")
            self.assertContains(response, "Player Bravo")

    def test_internal_teams_are_not_linked(self):
        response = self._get(self.organizer)
        for registration in self.registrations:
            self.assertNotContains(
                response, reverse("team_detail", kwargs={"pk": registration.shadow_team.pk})
            )


class SimulatorHybridTests(TestCase):
    """A-4: the what-if simulator offered every upcoming match, including a
    hybrid tournament's knockout matches, and applied a "draw" pick to them
    (both teams +points_per_draw), though a knockout match cannot be drawn."""

    def setUp(self):
        self.organizer = _make_organizer("org")
        self.tournament = Tournament.objects.create(
            name="Hybrid", format="hybrid", status="active", players_per_team=1,
            created_by=self.organizer,
        )
        self.a, self.b = (Team.objects.create(name=n) for n in ("Aces", "Bolts"))
        for team in (self.a, self.b):
            TeamTournamentParticipation.objects.create(
                team=team, tournament=self.tournament, status="active", group="A"
            )
        self.group_match = Match.objects.create(
            tournament=self.tournament, match_number=1, team1=self.a, team2=self.b,
            status="upcoming", group="A",
        )
        self.knockout_match = Match.objects.create(
            tournament=self.tournament, match_number=2, team1=self.a, team2=self.b,
            status="upcoming", group="",
        )
        self.client.force_login(self.organizer)

    def _get(self, **params):
        return self.client.get("/analytics/", {"tournament": self.tournament.pk, **params})

    def test_only_group_matches_are_offered(self):
        offered = [m.pk for m in self._get().context["simulator_matches"]]
        self.assertEqual(offered, [self.group_match.pk])
        self.assertTrue(all(m.draw_allowed for m in self._get().context["simulator_matches"]))

    def test_forged_knockout_draw_changes_nothing(self):
        response = self._get(**{f"sim_{self.knockout_match.pk}": "draw"})
        self.assertFalse(response.context["simulator_has_choices"])
        self.assertEqual(
            [row["point_change"] for row in response.context["simulated_standings"]], [0, 0]
        )

    def test_group_draw_still_applies(self):
        response = self._get(**{f"sim_{self.group_match.pk}": "draw"})
        changes = {row["team"].pk: row["point_change"] for row in response.context["simulated_standings"]}
        self.assertEqual(changes, {
            self.a.pk: self.tournament.points_per_draw,
            self.b.pk: self.tournament.points_per_draw,
        })

