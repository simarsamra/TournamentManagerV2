"""Tests for AI_STRUCTURE_PLAN.md: tournament structure (groups, brackets,
withdrawals) and how standings and the AI see it."""
from django.contrib.auth.models import User
from django.test import TestCase

from core import testing_tournaments as tt
from core.models import OrganizerProfile


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
