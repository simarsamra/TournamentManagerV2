"""Tests for AI_STRUCTURE_PLAN.md: tournament structure (groups, brackets,
withdrawals) and how standings and the AI see it."""
from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from core import testing_tournaments as tt
from core.models import OrganizerProfile, Team, TeamMembership
from core.standings import _head_to_head_matches, calculate_standings
from core.structure import build_structure, stage_labels, structure_kind


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


class WithdrawnTeamTests(TestCase):
    """ST-2 (X-1, X-2): a withdrawn team could be seeded into a hybrid's
    knockout from the group table, and the standings page's "W" badge tested
    Team.status, which is never "withdrawn" (withdrawal is recorded on the
    participation)."""

    def setUp(self):
        self.org = _make_organizer()

    def _withdraw_red_rovers_mid_groups(self):
        t = tt.make_hybrid(self.org, name="Hybrid")
        for match in t.matches.exclude(group="").filter(round_number__lte=2).order_by("match_number"):
            tt.play(match, *((2, 0) if tt._stronger_first(match) else (0, 2)))
        a = {r["team"].name: r["points"] for r in calculate_standings(t, group="A")}
        self.assertEqual(a["Red Rovers"], 6)
        tt.withdraw(t, tt.team("Red Rovers"), policy="void")
        return tt.play_group_stage(t)

    def test_a_withdrawn_team_is_not_seeded(self):
        t = self._withdraw_red_rovers_mid_groups()
        seeded = set()
        for match in t.matches.filter(group="", bracket_type="winners").select_related("team1", "team2"):
            seeded.update(name for name in _names(match) if name)
        self.assertNotIn("Red Rovers", seeded)
        self.assertTrue({"Green Giants", "Silver Hawks"} <= seeded)

    def test_standings_rows_say_who_withdrew(self):
        t = tt.league_with_withdrawal(self.org)
        flags = {r["team"].name: r["withdrawn"] for r in calculate_standings(t)}
        self.assertTrue(flags["Blue Jays"])
        self.assertEqual([n for n, w in flags.items() if w], ["Blue Jays"])

    def test_standings_page_shows_the_badge(self):
        t = tt.league_with_withdrawal(self.org)
        self.client.force_login(self.org)
        response = self.client.get("/standings/", {"tournament": t.pk})
        self.assertContains(response, "badge-withdrawn", count=1)

    def test_group_tables_show_the_badge(self):
        t = self._withdraw_red_rovers_mid_groups()
        self.client.force_login(self.org)
        response = self.client.get("/standings/", {"tournament": t.pk})
        self.assertContains(response, "badge-withdrawn", count=1)

    def test_public_standings_show_the_badge(self):
        t = tt.league_with_withdrawal(self.org)
        self.client.force_login(self.org)
        response = self.client.get("/public/standings/", {"tournament": t.pk})
        self.assertContains(response, "badge-withdrawn", count=1)


class StageLabelTests(TestCase):
    """ST-3 (groundwork for G-4, K-2): every match gets a stage name, the
    way the bracket pages name rounds."""

    def setUp(self):
        self.org = _make_organizer()

    def _labels(self, tournament, **filters):
        matches = tournament.matches.filter(**filters).order_by("round_number", "match_number")
        labels = stage_labels(tournament)
        return [labels[m.pk] for m in matches]

    def test_hybrid(self):
        t = tt.make_hybrid(self.org, third_place=True)
        self.assertEqual(set(self._labels(t, group="A")), {"Group A"})
        self.assertEqual(self._labels(t, group="", bracket_type="winners"), ["Semi-final", "Semi-final", "Final"])
        self.assertEqual(self._labels(t, bracket_type="third_place"), ["Third-place match"])

    def test_knockout_of_8(self):
        t = tt.make_knockout(self.org)
        self.assertEqual(self._labels(t), ["Quarter-final"] * 4 + ["Semi-final"] * 2 + ["Final"])

    def test_knockout_of_16(self):
        t = tt.make_knockout(self.org, [f"Team {i}" for i in range(1, 17)])
        self.assertEqual(self._labels(t, round_number=1), ["Round of 16"] * 8)

    def test_byes_keep_the_round_name(self):
        t = tt.make_knockout(self.org, tt.NAMES[:6])
        self.assertEqual(self._labels(t, round_number=1), ["Quarter-final"] * 4)

    def test_double_elimination(self):
        t = tt.make_double_elimination(self.org)
        self.assertEqual(self._labels(t, bracket_type="winners"),
                         ["Winners bracket quarter-final"] * 4 + ["Winners bracket semi-final"] * 2
                         + ["Winners bracket final"])
        self.assertEqual(self._labels(t, bracket_type="losers"),
                         ["Losers bracket round 1"] * 2 + ["Losers bracket round 2"] * 2
                         + ["Losers bracket round 3", "Losers bracket final"])
        self.assertEqual(self._labels(t, bracket_type="grand_final"), ["Grand final", "Grand final decider"])

    def test_consolation(self):
        t = tt.make_consolation(self.org)
        tt.play_ready(t)
        self.assertEqual(self._labels(t, bracket_type="consolation"),
                         ["Consolation semi-final", "Consolation semi-final", "Consolation final"])

    def test_league(self):
        t = tt.make_league(self.org, tt.NAMES[:4])
        self.assertEqual(set(self._labels(t, round_number=2)), {"Round 2"})

    def test_kinds(self):
        self.assertEqual(structure_kind(tt.make_league(self.org, tt.NAMES[:4], name="L")), "league")
        self.assertEqual(structure_kind(tt.make_hybrid(self.org, name="H")), "groups")
        self.assertEqual(structure_kind(tt.make_consolation(self.org, name="C")), "bracket")


def _label(team):
    return team.name


class BuildStructureTests(TestCase):
    """ST-4: each team's status, the phase and the placings, per format."""

    def setUp(self):
        self.org = _make_organizer()

    def _states(self, tournament):
        tournament.refresh_from_db()
        structure = build_structure(tournament, _label)
        names = {tt.team(n).pk: n for n in tt.NAMES if Team.objects.filter(name=n).exists()}
        return structure, {names[pk]: (s.status, s.detail) for pk, s in structure.teams.items() if pk in names}

    # -- hybrid --
    def test_hybrid_after_the_groups(self):
        structure, states = self._states(tt.hybrid_after_groups(self.org))
        self.assertEqual(structure.phase, "knockout")
        self.assertEqual(structure.kind, "groups")
        self.assertEqual(structure.advance_per_group, 2)
        self.assertEqual(sorted(structure.groups), ["A", "B"])
        for name in ("Red Rovers", "Green Giants", "Golden Boots", "Blue Jays"):
            self.assertEqual(states[name], ("alive", "Semi-final"), name)
        for name in ("Silver Hawks", "Black Bears", "Purple Pumas", "Orange Owls"):
            self.assertEqual(states[name], ("out_in_groups", ""), name)
        self.assertEqual(structure.teams[tt.team("Blue Jays").pk].text, "through to the semi-final")

    def _group_a_rounds(self, tournament, rounds, upset=()):
        for match in tournament.matches.filter(group="A", round_number__in=rounds).order_by("match_number"):
            tt.play(match, *((2, 0) if tt._stronger_first(match, upset) else (0, 2)))

    def test_nobody_is_through_after_one_round(self):
        t = tt.make_hybrid(self.org)
        self._group_a_rounds(t, [1])
        structure, states = self._states(t)
        self.assertEqual(structure.phase, "group_stage")
        self.assertEqual({states[n][0] for n in ("Red Rovers", "Green Giants", "Silver Hawks", "Black Bears")},
                         {"in_contention"})

    def test_a_possible_three_way_tie_decides_nothing(self):
        # Favourites win rounds 1-2: Red Rovers 6, Green Giants 3, Silver
        # Hawks 3, Black Bears 0. Round 3 could still leave Red Rovers, Silver
        # Hawks and Green Giants level on 6, or three teams level on 3 for
        # second, so nobody is through or out yet.
        t = tt.make_hybrid(self.org)
        self._group_a_rounds(t, [1, 2])
        _, states = self._states(t)
        self.assertEqual({states[n][0] for n in ("Red Rovers", "Green Giants", "Silver Hawks", "Black Bears")},
                         {"in_contention"})

    def test_through_and_out_before_the_last_round(self):
        # Silver Hawks beat Green Giants: Red Rovers 6, Silver Hawks 6, Green
        # Giants 0, Black Bears 0, with one round left (3 points at most).
        t = tt.make_hybrid(self.org)
        self._group_a_rounds(t, [1, 2], upset=("Silver Hawks",))
        a = {r["team"].name: r["points"] for r in calculate_standings(t, group="A")}
        self.assertEqual(a, {"Red Rovers": 6, "Silver Hawks": 6, "Green Giants": 0, "Black Bears": 0})
        _, states = self._states(t)
        self.assertEqual(states["Red Rovers"][0], "through")
        self.assertEqual(states["Silver Hawks"][0], "through")
        self.assertEqual(states["Green Giants"][0], "out_in_groups")
        self.assertEqual(states["Black Bears"][0], "out_in_groups")

    def test_group_statuses_on_the_rows(self):
        t = tt.make_hybrid(self.org)
        self._group_a_rounds(t, [1, 2], upset=("Silver Hawks",))
        structure = build_structure(t, _label)
        self.assertEqual({r["team"].name: r["status"] for r in structure.groups["A"]}["Red Rovers"], "through")

    def test_hybrid_after_one_semi(self):
        _, states = self._states(tt.hybrid_after_one_semi(self.org))
        self.assertEqual(states["Blue Jays"], ("out", "Semi-final"))
        self.assertEqual(states["Red Rovers"], ("alive", "Final"))

    def test_semi_loser_plays_for_third(self):
        _, states = self._states(tt.hybrid_after_one_semi(self.org, third_place=True))
        self.assertEqual(states["Blue Jays"], ("playing_for_third", ""))

    def test_hybrid_finished(self):
        structure, states = self._states(tt.hybrid_finished(self.org))
        self.assertEqual(structure.phase, "finished")
        self.assertEqual(states["Red Rovers"][0], "champion")
        self.assertEqual(states["Green Giants"][0], "runner_up")
        self.assertEqual(structure.placings, {
            "champion": "Red Rovers", "runner_up": "Green Giants",
            "semi_finalists": ["Blue Jays", "Golden Boots"],
        })

    # -- brackets --
    def test_knockout_after_round_1(self):
        structure, states = self._states(tt.knockout_after_round_1(self.org))
        self.assertEqual(structure.kind, "bracket")
        for name in tt.NAMES[:4]:
            self.assertEqual(states[name], ("alive", "Semi-final"), name)
        for name in tt.NAMES[4:]:
            self.assertEqual(states[name], ("out", "Quarter-final"), name)
        self.assertEqual(structure.teams[tt.team("Black Bears").pk].text, "out in the quarter-final")

    def test_third_place_match_decides_third(self):
        t = tt.make_knockout(self.org, third_place=True)
        tt.play_ready(t)          # quarter-finals
        tt.play_ready(t)          # semi-finals
        tt.play_ready(t, "third_place")
        tt.play_ready(t)          # final
        structure, states = self._states(t)
        self.assertEqual(structure.placings["champion"], "Red Rovers")
        self.assertEqual(structure.placings["runner_up"], "Golden Boots")
        self.assertEqual(structure.placings["third"], "Blue Jays")
        self.assertEqual(states["Green Giants"], ("out", "Third-place match"))
        self.assertNotIn("semi_finalists", structure.placings)

    def test_double_elimination_losses(self):
        t = tt.make_double_elimination(self.org)
        tt.play_ready(t, "winners")
        _, states = self._states(t)
        self.assertEqual({states[n][0] for n in tt.NAMES[:4]}, {"unbeaten"})
        self.assertEqual({states[n][0] for n in tt.NAMES[4:]}, {"one_life_left"})
        tt.play_ready(t, "losers")
        _, states = self._states(t)
        out = [n for n in tt.NAMES[4:] if states[n][0] == "out"]
        self.assertEqual(len(out), 2)
        self.assertTrue(all(states[n][1] == "Losers bracket round 1" for n in out))

    def _play_de_to_grand_final(self, t, upset=()):
        for _ in range(12):
            if tt.ready(t, "grand_final"):
                break
            tt.play_ready(t, "losers") or tt.play_ready(t, "winners")
        return tt.ready(t, "grand_final")[0]

    def test_grand_final_loser_from_the_winners_bracket_gets_a_decider(self):
        t = tt.make_double_elimination(self.org, reset=True)
        final = self._play_de_to_grand_final(t)
        unbeaten = final.team1 if final.team1.name == "Red Rovers" else final.team2
        other = final.team2 if unbeaten == final.team1 else final.team1
        tt.play(final, *((0, 2) if final.team1 == unbeaten else (2, 0)))
        _, states = self._states(t)
        self.assertEqual(states["Red Rovers"][0], "one_life_left")
        self.assertEqual(states[other.name][0], "one_life_left")

    def test_without_a_reset_the_grand_final_decides(self):
        t = tt.make_double_elimination(self.org, reset=False, name="No reset")
        final = self._play_de_to_grand_final(t)
        tt.play(final, *((0, 2) if final.team1.name == "Red Rovers" else (2, 0)))
        structure, states = self._states(t)
        self.assertEqual(states["Red Rovers"][0], "runner_up")
        self.assertEqual(structure.placings["runner_up"], "Red Rovers")

    def test_consolation(self):
        t = tt.make_consolation(self.org)
        tt.play_ready(t)
        _, states = self._states(t)
        self.assertEqual({states[n] for n in tt.NAMES[4:]}, {("in_consolation", "Consolation semi-final")})
        tt.play_ready(t, "consolation")
        tt.play_ready(t, "consolation")
        _, states = self._states(t)
        self.assertEqual(states["Silver Hawks"], ("consolation_winner", ""))
        self.assertEqual(states["Black Bears"], ("out", "Consolation semi-final"))

    # -- leagues and withdrawals --
    def test_league_running_and_finished(self):
        t = tt.make_league(self.org, tt.NAMES[:4])
        structure, states = self._states(t)
        self.assertEqual((structure.kind, structure.phase), ("league", "league"))
        self.assertEqual(set(states.values()), {("in_league", "")})
        for match in t.matches.order_by("match_number"):
            tt.play(match, *((1, 0) if tt._stronger_first(match) else (0, 1)))
        structure, states = self._states(t)
        self.assertEqual(structure.phase, "finished")
        self.assertEqual(structure.placings, {"champion": "Red Rovers", "runner_up": "Golden Boots", "third": "Blue Jays"})
        self.assertEqual(states["Green Giants"], ("placed", "4th"))

    def test_withdrawn_team_takes_no_placing(self):
        t = tt.make_league(self.org, tt.NAMES[:4])
        matches = list(t.matches.order_by("match_number"))
        for match in matches:
            if "Red Rovers" in _names(match):
                tt.play(match, *((1, 0) if match.team1.name == "Red Rovers" else (0, 1)))
        tt.withdraw(t, tt.team("Red Rovers"), policy="void")
        for match in t.matches.filter(status="upcoming").order_by("match_number"):
            tt.play(match, *((1, 0) if tt._stronger_first(match) else (0, 1)))
        t.refresh_from_db()
        self.assertEqual(calculate_standings(t)[0]["team"].name, "Red Rovers")
        structure, states = self._states(t)
        self.assertEqual(states["Red Rovers"], ("withdrawn", ""))
        self.assertEqual(structure.placings["champion"], "Golden Boots")

    def test_withdrawn_mid_season(self):
        t = tt.league_with_withdrawal(self.org)
        _, states = self._states(t)
        self.assertEqual(states["Blue Jays"], ("withdrawn", ""))

    def test_tiebreakers_in_words(self):
        structure = build_structure(tt.make_league(self.org, tt.NAMES[:4]), _label)
        self.assertEqual(structure.tiebreakers, ["game difference", "games won", "head-to-head"])

    def test_query_budget(self):
        t = tt.hybrid_finished(self.org)
        with CaptureQueriesContext(connection) as ctx:
            build_structure(t, _label)
        # Matches and participations once, then calculate_standings per group
        # (teams, confirmed matches, forfeits: 3 each; no head-to-head query
        # without a tie). Never a query per team or per match.
        self.assertEqual(len(ctx.captured_queries), 2 + 2 * 3)
