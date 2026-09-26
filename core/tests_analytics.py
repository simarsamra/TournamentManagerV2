"""Analytics page regressions (ANALYTICS_PLAN.md)."""
from datetime import datetime, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from unittest import mock

from core.models import (
    AuditLog, Court, Match, OrganizerProfile, Team, TeamMembership,
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
        # A hybrid is simulated group by group (ST-13): no group was touched.
        self.assertEqual(response.context["simulated_groups"], [])

    def test_group_draw_still_applies(self):
        response = self._get(**{f"sim_{self.group_match.pk}": "draw"})
        [group] = response.context["simulated_groups"]
        self.assertEqual(group["group"], "A")
        changes = {row["team"].pk: row["point_change"] for row in group["rows"]}
        self.assertEqual(changes, {
            self.a.pk: self.tournament.points_per_draw,
            self.b.pk: self.tournament.points_per_draw,
        })


class PageScriptTests(TestCase):
    """A-5: {% block extra_js %} was nested inside {% block content %}, so the
    page's script was rendered twice (in content and in base's slot)."""

    def test_script_is_emitted_once(self):
        organizer = _make_organizer("org")
        tournament = Tournament.objects.create(
            name="T", format="round_robin", status="active", players_per_team=1,
            created_by=organizer,
        )
        self.client.force_login(organizer)
        response = self.client.get("/analytics/", {"tournament": tournament.pk})
        # A-11 moved the chart data into a json_script element; count that.
        self.assertEqual(response.content.decode().count('id="schedule-density-data"'), 1)


class ThemeColourTests(TestCase):
    """A-6: bar tracks and form pills used hard-coded light colours, which
    stayed pale on dark-theme cards."""

    def test_no_hard_coded_light_colours_and_pills_are_labelled(self):
        organizer = _make_organizer("org")
        tournament = Tournament.objects.create(
            name="T", format="round_robin", status="active", players_per_team=1,
            created_by=organizer,
        )
        a, b = (Team.objects.create(name=n) for n in ("A", "B"))
        for team in (a, b):
            TeamTournamentParticipation.objects.create(
                team=team, tournament=tournament, status="active"
            )
        Match.objects.create(
            tournament=tournament, match_number=1, team1=a, team2=b,
            score_team1=2, score_team2=0, winner=a, status="confirmed",
        )
        self.client.force_login(organizer)
        response = self.client.get(
            "/analytics/", {"tournament": tournament.pk, "form_team": a.pk}
        )
        for colour in ("#e2e8f0", "#d1fae5", "#fecaca"):
            self.assertNotContains(response, colour)
        self.assertContains(response, 'class="form-pill is-win" aria-label="Win')


class CourtProgressTests(TestCase):
    """A-9: "Court Utilization" was confirmed / total scheduled matches -- the
    share played, not how busy the court is. Renamed to Court Progress."""

    def test_court_progress_is_share_of_scheduled_matches_played(self):
        organizer = _make_organizer("org")
        tournament = Tournament.objects.create(
            name="T", format="round_robin", status="active", players_per_team=1,
            created_by=organizer,
        )
        court = Court.objects.create(tournament=tournament, name="Court 1")
        a, b = (Team.objects.create(name=n) for n in ("A", "B"))
        for number, status in enumerate(("confirmed", "upcoming", "upcoming", "upcoming"), start=1):
            Match.objects.create(
                tournament=tournament, match_number=number, team1=a, team2=b,
                court=court, status=status,
            )
        self.client.force_login(organizer)
        response = self.client.get("/analytics/", {"tournament": tournament.pk})
        self.assertEqual(response.context["court_stats"][0]["completion_pct"], 25.0)
        self.assertContains(response, "Court Progress")
        self.assertNotContains(response, "Utilization")


class SimulatorTiebreakerTests(TestCase):
    """A-8: simulated standings sorted by (points, game_diff, games_won, wins)
    whatever the tournament's configured tiebreakers, so two teams level on
    projected points could come out in a different order from the real
    table."""

    def setUp(self):
        self.organizer = _make_organizer("org")
        self.tournament = Tournament.objects.create(
            name="T", format="round_robin", status="active", players_per_team=1,
            created_by=self.organizer, tiebreaker_order='["games_won", "game_diff"]',
        )
        self.x, self.y, self.z = (Team.objects.create(name=n) for n in ("X", "Y", "Z"))
        for team in (self.x, self.y, self.z):
            TeamTournamentParticipation.objects.create(
                team=team, tournament=self.tournament, status="active"
            )
        # X: better game difference (+2); Y: more games won (5 vs 2).
        self._match(1, self.x, self.z, 2, 0, "confirmed")
        self._match(2, self.y, self.z, 5, 4, "confirmed")
        self.upcoming = self._match(3, self.x, self.y, None, None, "upcoming")
        self.client.force_login(self.organizer)

    def _match(self, number, team1, team2, score1, score2, status):
        winner = None
        if score1 is not None and score1 != score2:
            winner = team1 if score1 > score2 else team2
        return Match.objects.create(
            tournament=self.tournament, match_number=number, team1=team1, team2=team2,
            score_team1=score1, score_team2=score2, winner=winner, status=status,
        )

    def test_projected_tie_uses_the_configured_tiebreakers(self):
        response = self.client.get(
            "/analytics/", {"tournament": self.tournament.pk, f"sim_{self.upcoming.pk}": "draw"}
        )
        simulated = [row["team"].pk for row in response.context["simulated_standings"]]
        real = [row["team"].pk for row in calculate_standings(self.tournament)]
        # Level on points before and after a projected draw, so the order is
        # decided by tiebreakers alone and must match the real table: Y first.
        self.assertEqual(simulated, real)
        self.assertEqual(simulated[:2], [self.y.pk, self.x.pk])
        self.assertEqual(
            [row["rank"] for row in response.context["simulated_standings"]], [1, 2, 3]
        )

    def test_truncation_is_shown(self):
        for number in range(4, 13):
            self._match(number, self.y, self.z, None, None, "upcoming")
        response = self.client.get("/analytics/", {"tournament": self.tournament.pk})
        self.assertEqual(len(response.context["simulator_matches"]), 8)
        self.assertContains(response, "Showing the next 8 of 10 upcoming matches")

    def test_no_truncation_note_when_everything_fits(self):
        response = self.client.get("/analytics/", {"tournament": self.tournament.pk})
        self.assertNotContains(response, "Showing the next")


class AnalyticsCleanupTests(TestCase):
    """A-11: standings computed twice per request, Points Overview widths
    relying on widthratio's divide-by-zero behaviour, and chart data passed
    through |safe."""

    def setUp(self):
        self.organizer = _make_organizer("org")
        self.tournament = Tournament.objects.create(
            name="T", format="round_robin", status="active", players_per_team=1,
            created_by=self.organizer,
        )
        self.teams = [Team.objects.create(name=n) for n in ("A", "B", "C")]
        for team in self.teams:
            TeamTournamentParticipation.objects.create(
                team=team, tournament=self.tournament, status="active"
            )
        self.client.force_login(self.organizer)

    def _get(self):
        return self.client.get("/analytics/", {"tournament": self.tournament.pk})

    def test_standings_are_calculated_once(self):
        a, b, _ = self.teams
        Match.objects.create(
            tournament=self.tournament, match_number=1, team1=a, team2=b, status="upcoming",
        )
        with mock.patch(
            "core.views.reporting.calculate_standings", wraps=calculate_standings
        ) as spy:
            response = self._get()
        self.assertTrue(response.context["simulator_matches"])
        self.assertEqual(spy.call_count, 1)

    def test_points_bars_are_zero_when_nobody_has_points(self):
        widths = [row["points_pct"] for row in self._get().context["standings"]]
        self.assertEqual(widths, [0, 0, 0])

    def test_points_bars_scale_to_the_leader(self):
        a, b, c = self.teams
        for number, (t1, t2) in enumerate(((a, c), (b, c), (a, b)), start=1):
            Match.objects.create(
                tournament=self.tournament, match_number=number, team1=t1, team2=t2,
                score_team1=1, score_team2=0, winner=t1, status="confirmed",
            )
        widths = {row["team"].pk: row["points_pct"] for row in self._get().context["standings"]}
        self.assertEqual(widths, {a.pk: 100, b.pk: 50, c.pk: 0})

    def test_schedule_density_is_passed_as_json_script(self):
        content = self._get().content.decode()
        self.assertIn('<script id="schedule-density-data" type="application/json">', content)
        # No longer pasted inline as a JS literal through |safe.
        self.assertNotIn("var data = {", content)


class ScheduleDensityTests(TestCase):
    """A-10: schedule density drew one bar per calendar day, so a months-long
    league rendered hundreds of rows. Spans over 45 days group by week."""

    def setUp(self):
        self.organizer = _make_organizer("org")
        self.tournament = Tournament.objects.create(
            name="T", format="round_robin", status="active", players_per_team=1,
            created_by=self.organizer,
        )
        self.a, self.b = (Team.objects.create(name=n) for n in ("A", "B"))
        self.client.force_login(self.organizer)

    def _schedule(self, day_offsets):
        start = timezone.make_aware(datetime(2026, 3, 2, 12, 0))  # a Monday
        for number, offset in enumerate(day_offsets, start=1):
            Match.objects.create(
                tournament=self.tournament, match_number=number, team1=self.a, team2=self.b,
                status="upcoming", scheduled_time=start + timedelta(days=offset),
            )
        response = self.client.get("/analytics/", {"tournament": self.tournament.pk})
        return response, response.context["schedule_density"]

    def test_short_span_is_daily(self):
        response, buckets = self._schedule(range(10))
        self.assertEqual(len(buckets), 10)
        self.assertEqual(buckets[0], ["2026-03-02", 1])
        self.assertContains(response, "Matches per Day")

    def test_long_span_is_weekly(self):
        # Two matches in each of the first two days, then one every 3 days to day 90.
        response, buckets = self._schedule([0, 0, 1, 1] + list(range(3, 91, 3)))
        self.assertEqual(buckets[0], ["Week of Mar 2", 6])  # Mon 2 - Sun 8: days 0,0,1,1,3,6
        self.assertEqual(len(buckets), 13)  # 91 days from a Monday span 13 weeks
        self.assertEqual(sum(count for _, count in buckets), 4 + len(range(3, 91, 3)))
        self.assertContains(response, "Matches per Week")


class WidgetStatePreservationTests(TestCase):
    """A-12 (1): the four widget forms were separate GET forms, so submitting
    one dropped the others' parameters -- choosing a head-to-head pair reset
    the form-trend team and every simulator pick."""

    def setUp(self):
        self.organizer = _make_organizer("org")
        self.tournament = Tournament.objects.create(
            name="T", format="round_robin", status="active", players_per_team=1,
            created_by=self.organizer,
        )
        self.a, self.b, self.c = (Team.objects.create(name=n) for n in ("A", "B", "C"))
        for team in (self.a, self.b, self.c):
            TeamTournamentParticipation.objects.create(
                team=team, tournament=self.tournament, status="active"
            )
        self.upcoming = Match.objects.create(
            tournament=self.tournament, match_number=1, team1=self.a, team2=self.b,
            status="upcoming",
        )
        self.client.force_login(self.organizer)

    @staticmethod
    def _hidden(name, value):
        return f'<input type="hidden" name="{name}" value="{value}">'

    def test_every_other_form_carries_each_widgets_state(self):
        response = self.client.get("/analytics/", {
            "tournament": self.tournament.pk,
            "h2h_team1": self.b.pk, "h2h_team2": self.c.pk,
            "form_team": self.c.pk, "form_window": 8,
            "prep_team": self.b.pk,
            f"sim_{self.upcoming.pk}": "team2",
        })
        content = response.content.decode()
        # Each widget's values ride along in the three *other* forms...
        for name, value in (
            ("h2h_team1", self.b.pk), ("h2h_team2", self.c.pk),
            ("form_team", self.c.pk), ("form_window", 8),
            ("prep_team", self.b.pk), (f"sim_{self.upcoming.pk}", "team2"),
        ):
            self.assertEqual(content.count(self._hidden(name, value)), 3, name)
        # ...and the tournament in all four, so a submit stays on it.
        self.assertEqual(content.count(self._hidden("tournament", self.tournament.pk)), 4)

    def test_submitting_one_widget_keeps_the_others(self):
        # The URL a head-to-head submit produces once the hidden fields ride along.
        response = self.client.get("/analytics/", {
            "tournament": self.tournament.pk,
            "h2h_team1": self.a.pk, "h2h_team2": self.c.pk,
            "form_team": self.c.pk, f"sim_{self.upcoming.pk}": "team2",
        })
        self.assertEqual(response.context["form_team"].pk, self.c.pk)
        self.assertEqual(response.context["simulator_matches"][0].selected_outcome, "team2")

    def test_unknown_parameters_are_not_echoed(self):
        response = self.client.get("/analytics/", {
            "tournament": self.tournament.pk, "sim_99999": "team1", "junk": "x",
        })
        self.assertNotContains(response, 'name="sim_99999"')
        self.assertNotContains(response, 'name="junk"')


class WidgetHtmxPartialTests(WidgetStatePreservationTests):
    """A-12 (2): each widget card updates in place over HTMX instead of a
    full-page reload (which needed a scroll-restore script to paper over
    it). Reuses the fixture above; its own tests run again here too."""

    def _htmx(self, target, **params):
        return self.client.get(
            "/analytics/",
            {"tournament": self.tournament.pk, **params},
            HTTP_HX_REQUEST="true", HTTP_HX_TARGET=target,
        )

    def test_htmx_widget_request_returns_only_that_card(self):
        response = self._htmx("analytics-h2h", h2h_team1=self.a.pk, h2h_team2=self.c.pk)
        self.assertTemplateUsed(response, "core/partials/analytics_h2h.html")
        self.assertTemplateNotUsed(response, "core/analytics.html")
        self.assertContains(response, 'id="analytics-h2h"')
        self.assertContains(response, "Head-to-Head Matchup Card")
        self.assertNotContains(response, "Rolling Form Trend")
        self.assertNotContains(response, "<html")

    def test_htmx_response_refreshes_the_other_forms_hidden_state(self):
        response = self._htmx(
            "analytics-h2h", h2h_team1=self.a.pk, h2h_team2=self.c.pk, form_team=self.b.pk,
        )
        content = response.content.decode()
        for widget in ("form", "prep", "sim"):
            self.assertIn(f'<span id="analytics-hidden-{widget}" class="analytics-hidden-state" hx-swap-oob="true">', content)
        # The new head-to-head pick reaches the other three forms.
        self.assertEqual(content.count(self._hidden("h2h_team2", self.c.pk)), 3)
        # The swapped card's own hidden block is not out-of-band.
        self.assertIn('<span id="analytics-hidden-h2h" class="analytics-hidden-state">', content)

    def test_each_widget_has_a_partial(self):
        for target, template in (
            ("analytics-form", "core/partials/analytics_form.html"),
            ("analytics-prep", "core/partials/analytics_prep.html"),
            ("analytics-sim", "core/partials/analytics_simulator.html"),
        ):
            with self.subTest(target=target):
                response = self._htmx(target)
                self.assertTemplateUsed(response, template)
                self.assertTemplateNotUsed(response, "core/analytics.html")

    def test_non_htmx_and_unknown_target_get_the_full_page(self):
        full = self.client.get("/analytics/", {"tournament": self.tournament.pk})
        self.assertTemplateUsed(full, "core/analytics.html")
        other = self._htmx("page-content-region")
        self.assertTemplateUsed(other, "core/analytics.html")

    def test_forms_submit_over_htmx_and_still_work_without_it(self):
        content = self.client.get("/analytics/", {"tournament": self.tournament.pk}).content.decode()
        for widget in ("h2h", "form", "prep", "sim"):
            self.assertIn(
                f'<form method="get" action="/analytics/" hx-get="/analytics/" '
                f'hx-target="#analytics-{widget}" hx-swap="outerHTML" hx-push-url="true"',
                content,
            )
        self.assertNotIn("analytics-scroll:", content)


class AnalyticsQueryCountTests(TestCase):
    """A-13: roughly two extra queries per court and per withdrawn team. The
    page's query count must not grow with the tournament's size."""

    def _tournament(self, size):
        organizer = _make_organizer(f"org{size}")
        tournament = Tournament.objects.create(
            name=f"T{size}", format="round_robin", status="active", players_per_team=1,
            created_by=organizer,
        )
        courts = [
            Court.objects.create(tournament=tournament, name=f"Court {n}")
            for n in range(size // 4)
        ]
        teams = []
        for n in range(size):
            team = Team.objects.create(name=f"T{size}-{n}")
            TeamTournamentParticipation.objects.create(
                team=team, tournament=tournament,
                status="withdrawn" if n >= size - size // 4 else "active",
            )
            teams.append(team)
        number = 0
        for i, t1 in enumerate(teams):
            for t2 in teams[i + 1:]:
                number += 1
                played = number % 2 == 0
                Match.objects.create(
                    tournament=tournament, match_number=number, team1=t1, team2=t2,
                    court=courts[number % len(courts)],
                    status="confirmed" if played else ("forfeited" if number % 7 == 0 else "upcoming"),
                    score_team1=2 if played else None, score_team2=1 if played else None,
                    winner=t1 if played or number % 7 == 0 else None,
                )
        return organizer, tournament

    def _count(self, size):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        organizer, tournament = self._tournament(size)
        self.client.force_login(organizer)
        self.client.get("/analytics/", {"tournament": tournament.pk})  # warm session/caches
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get("/analytics/", {"tournament": tournament.pk})
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    def test_query_count_does_not_grow_with_tournament_size(self):
        self.assertEqual(self._count(8), self._count(16))

    def _count_with_ties(self, size):
        """Every team active and on one court; alternate matches confirmed, so
        many teams tie on points, game difference and games won and the
        default head-to-head tiebreaker runs once per tied group."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        organizer = _make_organizer(f"tie{size}")
        tournament = Tournament.objects.create(
            name=f"Ties{size}", format="round_robin", status="active",
            players_per_team=1, created_by=organizer,
        )
        self.assertIn("head_to_head", tournament.get_tiebreaker_order())
        court = Court.objects.create(tournament=tournament, name="C1")
        teams = [Team.objects.create(name=f"Tie{size}-{n}") for n in range(size)]
        for team in teams:
            TeamTournamentParticipation.objects.create(team=team, tournament=tournament, status="active")
        number = 0
        for i, t1 in enumerate(teams):
            for t2 in teams[i + 1:]:
                number += 1
                played = number % 2 == 0
                Match.objects.create(
                    tournament=tournament, match_number=number, team1=t1, team2=t2, court=court,
                    status="confirmed" if played else "upcoming",
                    score_team1=2 if played else None, score_team2=1 if played else None,
                    winner=t1 if played else None,
                )
        self.client.force_login(organizer)
        self.client.get("/analytics/", {"tournament": tournament.pk})
        with CaptureQueriesContext(connection) as ctx:
            self.client.get("/analytics/", {"tournament": tournament.pk})
        return len(ctx.captured_queries)

    def test_query_count_does_not_grow_with_head_to_head_ties(self):
        self.assertEqual(self._count_with_ties(8), self._count_with_ties(16))

    def test_withdrawal_card_counts_are_unchanged_by_batching(self):
        organizer = _make_organizer("org")
        tournament = Tournament.objects.create(
            name="W", format="round_robin", status="active", players_per_team=1,
            created_by=organizer,
        )
        stayer, leaver = (Team.objects.create(name=n) for n in ("Stayer", "Leaver"))
        TeamTournamentParticipation.objects.create(team=stayer, tournament=tournament, status="active")
        TeamTournamentParticipation.objects.create(team=leaver, tournament=tournament, status="withdrawn")
        for number, status in enumerate(("forfeited", "cancelled", "confirmed"), start=1):
            Match.objects.create(
                tournament=tournament, match_number=number, team1=stayer, team2=leaver,
                status=status, winner=stayer if status == "forfeited" else None,
                score_team1=1 if status == "confirmed" else None,
                score_team2=0 if status == "confirmed" else None,
            )
        self.client.force_login(organizer)
        info = self.client.get("/analytics/", {"tournament": tournament.pk}).context["withdrawal_info"]
        self.assertEqual(
            [(row["team"].pk, row["display_label"], row["affected_matches"]) for row in info],
            [(leaver.pk, "Leaver", 2)],
        )
