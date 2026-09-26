"""Tests for AI_STRUCTURE_PLAN.md: tournament structure (groups, brackets,
withdrawals) and how standings and the AI see it."""
from django.contrib.auth.models import User
from django.test import TestCase

from core import testing_tournaments as tt
from core.models import OrganizerProfile, TeamMembership
from core.standings import _head_to_head_matches, calculate_standings


def _make_organizer(username="org"):
    user = User.objects.create_user(username=username, password="Regression-Pass-1")
    OrganizerProfile.objects.filter(user=user).update(verified=True)
    user.refresh_from_db()
    return user


def _groups(tournament):
    groups = {}
    for p in tournament.team_participations.select_related("team").order_by("team__name"):
        groups.setdefault(p.group, set()).add(p.team.name)
    return groups


def _names(match):
    return (match.team1.name if match.team1 else None, match.team2.name if match.team2 else None)


class TournamentBuilderTests(TestCase):
    """ST-0: the shared test tournaments are built by the production code."""

    def setUp(self):
        self.org = _make_organizer()

    def test_hybrid_groups_follow_snake_seeding(self):
        t = tt.make_hybrid(self.org)
        self.assertEqual(_groups(t), {
            "A": {"Red Rovers", "Green Giants", "Silver Hawks", "Black Bears"},
            "B": {"Golden Boots", "Blue Jays", "Purple Pumas", "Orange Owls"},
        })

    def test_group_stage_seeds_the_knockout(self):
        t = tt.hybrid_after_groups(self.org)
        semis = {frozenset(_names(m)) for m in tt.ready(t, "winners")}
        self.assertEqual(semis, {
            frozenset({"Red Rovers", "Blue Jays"}), frozenset({"Green Giants", "Golden Boots"}),
        })

    def test_hybrid_finished_crowns_the_final_winner(self):
        t = tt.hybrid_finished(self.org)
        self.assertEqual(t.status, "completed")
        self.assertEqual(t.champion.name, "Red Rovers")

    def test_knockout_winners_advance(self):
        t = tt.knockout_after_round_1(self.org)
        second = tt.ready(t, "winners")
        self.assertEqual(len(second), 2)
        for match in second:
            self.assertTrue(match.team1_id and match.team2_id)

    def test_double_elimination_losers_drop_down(self):
        t = tt.make_double_elimination(self.org)
        played = tt.play_ready(t, "winners")
        losers = {(m.team2 if m.winner_id == m.team1_id else m.team1).name for m in played}
        placed = set()
        for match in t.matches.filter(bracket_type="losers").select_related("team1", "team2"):
            placed.update(n for n in _names(match) if n)
        self.assertEqual(losers, placed)

    def test_consolation_bracket_appears_after_round_1(self):
        t = tt.make_consolation(self.org)
        self.assertFalse(t.matches.filter(bracket_type="consolation").exists())
        tt.play_ready(t, "winners")
        self.assertTrue(t.matches.filter(bracket_type="consolation").exists())

    def test_league_withdrawal_goes_through_the_real_code(self):
        t = tt.league_with_withdrawal(self.org)
        p = t.team_participations.get(team__name="Blue Jays")
        self.assertEqual(p.status, "withdrawn")
        self.assertFalse(t.matches.filter(status="upcoming", team1__name="Blue Jays").exists())
        self.assertFalse(t.matches.filter(status="upcoming", team2__name="Blue Jays").exists())


class HybridStandingsTests(TestCase):
    """ST-1 (G-2): in a hybrid, calculate_standings(tournament) with no group
    counted knockout matches, so a knockout win added league points."""

    def setUp(self):
        self.org = _make_organizer()

    def _points(self, tournament):
        return {row["team"].name: (row["points"], row["played"]) for row in calculate_standings(tournament)}

    def test_a_knockout_win_adds_no_points(self):
        t = tt.hybrid_after_one_semi(self.org)
        points = self._points(t)
        self.assertEqual(points["Red Rovers"], (9, 3))
        self.assertEqual(points["Blue Jays"], (6, 3))

    def test_the_final_adds_no_points_either(self):
        t = tt.hybrid_finished(self.org)
        points = self._points(t)
        self.assertEqual(points["Red Rovers"], (9, 3))
        self.assertEqual(points["Green Giants"], (6, 3))

    def test_group_tables_are_unchanged(self):
        t = tt.hybrid_finished(self.org)
        a = [(r["team"].name, r["points"]) for r in calculate_standings(t, group="A")]
        self.assertEqual(a, [("Red Rovers", 9), ("Green Giants", 6), ("Silver Hawks", 3), ("Black Bears", 0)])

    def test_head_to_head_ignores_knockout_results(self):
        t = tt.hybrid_after_one_semi(self.org)
        matches = _head_to_head_matches(t)
        self.assertTrue(matches)
        self.assertTrue(all(m.group for m in matches))

    def test_league_head_to_head_still_reads_every_match(self):
        t = tt.make_league(self.org, tt.NAMES[:4])
        tt.play(t.matches.order_by("match_number").first(), 1, 0)
        self.assertEqual(len(_head_to_head_matches(t)), 1)

    def test_analytics_page_shows_group_stage_points(self):
        t = tt.hybrid_after_one_semi(self.org)
        self.client.force_login(self.org)
        response = self.client.get("/analytics/", {"tournament": t.pk})
        points = {row["team"].name: row["points"] for row in response.context["standings"]}
        self.assertEqual(points["Red Rovers"], 9)


class HybridDashboardRankTests(TestCase):
    """ST-1 (D-4): a hybrid team's dashboard rank is its rank in its group."""

    def setUp(self):
        self.org = _make_organizer()
        self.tournament = tt.hybrid_after_one_semi(self.org)
        self.player = User.objects.create_user(username="jay", password="Regression-Pass-1")
        TeamMembership.objects.create(team=tt.team("Blue Jays"), user=self.player, role="captain")
        self.client.force_login(self.player)

    def test_rank_is_within_the_group(self):
        response = self.client.get("/dashboard/", {"tournament": self.tournament.pk})
        self.assertEqual(response.context["team_standing"]["rank"], 2)
        self.assertEqual(response.context["team_standing"]["points"], 6)
        self.assertEqual(response.context["team_standing_group"], "B")
        self.assertContains(response, "in Group B")

    def test_league_dashboard_has_no_group(self):
        league = tt.make_league(self.org, tt.NAMES[:4], name="Plain league")
        TeamMembership.objects.create(team=tt.team("Red Rovers"), user=self.player)
        response = self.client.get("/dashboard/", {"tournament": league.pk})
        self.assertIsNone(response.context.get("team_standing_group"))
        self.assertNotContains(response, "in Group")
