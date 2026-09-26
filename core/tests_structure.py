"""Tests for AI_STRUCTURE_PLAN.md: tournament structure (groups, brackets,
withdrawals) and how standings and the AI see it."""
import json

from django.contrib.auth.models import User
from django.db import connection
from django.db.models import Q
from django.template.loader import render_to_string
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core import testing_tournaments as tt
from core.ai import recap, team_news
from core.ai.conversation import SYSTEM_PROMPT as CONVERSATION_PROMPT
from core.ai.explain import SYSTEM_PROMPT as EXPLAIN_PROMPT
from core.ai.facts import Route, build_facts, serialise
from core.ai.router import build_schema, route_question
from core.ai.snapshot import MAX_SNAPSHOT_CHARS, build_snapshot
from core.ai.structure_facts import STRUCTURE_RULE, WORDING_RULE, trim
from core.ai.testing import FakeOllama
from core.models import AIQuestion, Match, OrganizerProfile, Team, TeamMembership
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


class SeparatedByTests(TestCase):
    """ST-5 (T-1): teams level on points say which tiebreaker split them, so
    the AI never has to guess ("top on goal difference")."""

    def setUp(self):
        self.org = _make_organizer()
        self.t = tt.make_league(self.org, tt.NAMES[:4])

    def _play(self, winner_or_first, other, s1, s2):
        match = self.t.matches.get(
            Q(team1__name=winner_or_first, team2__name=other) | Q(team1__name=other, team2__name=winner_or_first)
        )
        tt.play(match, *((s1, s2) if match.team1.name == winner_or_first else (s2, s1)))

    def _reasons(self):
        rows = build_structure(self.t, _label).table
        return [(r["team"].name, r.get("separated_by")) for r in rows]

    def test_games_won(self):
        self._play("Red Rovers", "Blue Jays", 3, 2)
        self._play("Golden Boots", "Green Giants", 1, 0)
        self.assertEqual(self._reasons()[:2], [("Red Rovers", None), ("Golden Boots", "games won")])

    def test_game_difference_then_head_to_head(self):
        self._play("Red Rovers", "Golden Boots", 2, 1)
        self._play("Golden Boots", "Green Giants", 2, 1)
        self._play("Blue Jays", "Red Rovers", 2, 1)
        self.assertEqual(self._reasons()[:3], [
            ("Blue Jays", None), ("Red Rovers", "game difference"), ("Golden Boots", "head-to-head"),
        ])

    def test_level_on_everything(self):
        self._play("Red Rovers", "Golden Boots", 1, 1)
        reasons = dict(self._reasons())
        self.assertEqual(reasons["Golden Boots"], "registration order")

    def test_untied_rows_have_no_reason(self):
        self._play("Red Rovers", "Golden Boots", 2, 0)
        self._play("Blue Jays", "Green Giants", 2, 1)
        self._play("Red Rovers", "Blue Jays", 2, 0)
        rows = dict(self._reasons())
        self.assertIsNone(rows["Red Rovers"])
        self.assertIsNone(rows["Blue Jays"])

    def test_group_tables_get_reasons_too(self):
        t = tt.hybrid_after_groups(self.org)
        rows = build_structure(t, _label).groups["A"]
        self.assertTrue(all("separated_by" not in r for r in rows))   # 9, 6, 3, 0: no ties


def _walk(value, path=""):
    """Yield (path, key, value) for every dict entry in a facts document."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield path, key, item
            yield from _walk(item, f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _walk(item, f"{path}[{i}]")


class RoutedFactsStructureTests(TestCase):
    """ST-6 (G-1, G-8): routed answers see one table per group, a bracket
    summary in brackets, and what-ifs within the match's group."""

    def setUp(self):
        self.org = _make_organizer()

    def test_hybrid_standings_are_per_group(self):
        t = tt.hybrid_after_groups(self.org)
        facts = build_facts(t, self.org, Route("standings"))
        self.assertNotIn("standings_top", facts)
        self.assertEqual([g["group"] for g in facts["groups"]], ["A", "B"])
        self.assertEqual([len(g["table"]) for g in facts["groups"]], [4, 4])
        self.assertEqual({g["advance"] for g in facts["groups"]}, {2})
        b = facts["groups"][1]["table"]
        self.assertEqual([(r["team"], r["points"], r["status"]) for r in b[:2]], [
            ("Golden Boots", 9, "through to the semi-final"), ("Blue Jays", 6, "through to the semi-final"),
        ])
        self.assertEqual(facts["phase"], "knockout")
        self.assertEqual(facts["bracket"]["next_round"], "Semi-final")
        self.assertEqual(facts["tournament"]["kind"], "groups")

    def test_one_group(self):
        t = tt.hybrid_after_groups(self.org)
        facts = build_facts(t, self.org, Route("standings", group="B"))
        self.assertEqual([g["group"] for g in facts["groups"]], ["B"])

    def test_knockout_has_no_table(self):
        t = tt.knockout_after_round_1(self.org)
        facts = build_facts(t, self.org, Route("standings"))
        keys = {key for _, key, _ in _walk(facts)}
        self.assertFalse(keys & {"rank", "points", "table", "standings_top", "results_top"})
        self.assertEqual(facts["bracket"]["next_round"], "Semi-final")
        self.assertEqual({row["out_in"] for row in facts["bracket"]["knocked_out"]}, {"Quarter-final"})

    def test_named_teams_carry_their_status(self):
        t = tt.hybrid_after_one_semi(self.org)
        facts = build_facts(t, self.org, Route("head_to_head", tt.team("Red Rovers"), tt.team("Blue Jays")))
        self.assertEqual(facts["head_to_head"]["team_a_status"], "through to the final")
        self.assertEqual(facts["head_to_head"]["team_b_status"], "out in the semi-final")

    def test_next_match_has_a_stage(self):
        t = tt.hybrid_after_one_semi(self.org)
        facts = build_facts(t, self.org, Route("next_match", tt.team("Green Giants")))
        self.assertEqual(facts["next_match"]["stage"], "Semi-final")

    def test_what_if_stays_in_the_group(self):
        # Favourites win group rounds 1-2: A = Red Rovers 6, Green Giants 3,
        # Silver Hawks 3, Black Bears 0, all still in contention. What if Red
        # Rovers beat Silver Hawks? Red Rovers reach 9: through.
        t = tt.make_hybrid(self.org)
        for match in t.matches.exclude(group="").filter(round_number__lte=2).order_by("match_number"):
            tt.play(match, *((2, 0) if tt._stronger_first(match) else (0, 2)))
        match = t.matches.get(group="A", round_number=3, team1__name="Red Rovers")
        winner = "team1" if match.team1.name == "Red Rovers" else "team2"
        facts = build_facts(t, self.org, Route("what_if", tt.team("Red Rovers"), tt.team("Silver Hawks"),
                                               match=match, winner=winner))
        what_if = facts["what_if"]
        self.assertEqual(what_if["group"], "A")
        self.assertEqual({r["team"] for r in what_if["projected_standings_top"]},
                         {"Red Rovers", "Green Giants", "Silver Hawks", "Black Bears"})
        self.assertEqual(what_if["status_changes"], [{
            "team": "Red Rovers", "before": "still in the race to go through", "after": "through to the knockouts",
        }])

    def test_group_schema_only_for_hybrids(self):
        keys = {"T1": object()}
        self.assertNotIn("group", build_schema(keys)["properties"])
        self.assertEqual(build_schema(keys, ["A", "B"])["properties"]["group"]["enum"], ["A", "B", "none"])

    def test_router_picks_a_group(self):
        t = tt.hybrid_after_groups(self.org)
        with FakeOllama() as fake:
            fake.respond_chat(json.dumps({"intent": "standings", "team_a": "none", "team_b": "none",
                                          "window": 5, "winner": "none", "group": "B"}))
            routed = route_question(t, "who leads group b")
        self.assertEqual((routed.route.intent, routed.route.group), ("standings", "B"))
        body = fake.requests[0]["body"]
        self.assertIn("GROUPS:", body["messages"][1]["content"])
        self.assertIn("group", body["format"]["properties"])

    def test_unknown_group_is_refused(self):
        t = tt.hybrid_after_groups(self.org)
        with FakeOllama() as fake:
            fake.respond_chat(json.dumps({"intent": "standings", "team_a": "none", "team_b": "none",
                                          "window": 5, "winner": "none", "group": "Z"}))
            routed = route_question(t, "who leads group z")
        self.assertEqual(routed.route.intent, "unknown")
        self.assertIn("no group Z", routed.message)

    def test_explanations_are_told_the_structure_rule(self):
        self.assertIn(STRUCTURE_RULE, EXPLAIN_PROMPT)

    def test_trim_keeps_who_goes_through(self):
        t = tt.hybrid_after_groups(self.org)
        facts = build_facts(t, self.org, Route("standings"))
        trim(facts, 10, lambda f: len(serialise(f)), keep_groups={"B"})
        self.assertEqual([len(g["table"]) for g in facts["groups"]], [3, 4])


class AnswerCardTests(TestCase):
    """ST-6: the answer card shows what the facts hold, per group or as a
    bracket summary."""

    def setUp(self):
        self.org = _make_organizer()

    def _render(self, facts):
        return render_to_string("core/partials/ai_answer_facts.html",
                                {"facts": facts, "route": {"intent": "standings"}})

    def test_group_tables(self):
        html = self._render(build_facts(tt.hybrid_after_groups(self.org), self.org, Route("standings")))
        self.assertIn("Group A", html)
        self.assertIn("top 2 go through", html)
        self.assertIn("through to the semi-final", html)
        self.assertIn("Next round:</strong> Semi-final", html)

    def test_bracket_summary(self):
        html = self._render(build_facts(tt.knockout_after_round_1(self.org), self.org, Route("standings")))
        self.assertIn("Black Bears (Quarter-final)", html)
        self.assertNotIn("<th>Pts</th>", html)


class SnapshotStructureTests(TestCase):
    """ST-7 (G-1, G-3, G-4, K-2, K-6, W-2, S-1, S-3): the conversation's
    snapshot knows groups, stages, statuses and withdrawals."""

    def setUp(self):
        self.org = _make_organizer()

    def test_hybrid_groups_and_gaps(self):
        snap = build_snapshot(tt.hybrid_after_one_semi(self.org), self.org)
        self.assertNotIn("table", snap)
        self.assertEqual([len(g["table"]) for g in snap["groups"]], [4, 4])
        rows = {r["team"]: r for g in snap["groups"] for r in g["table"]}
        self.assertLessEqual(max(r["points"] for r in rows.values()), 9)
        hawks = rows["Silver Hawks"]
        self.assertEqual(hawks["points_behind_group_leader"], 6)       # Red Rovers 9, not the overall leader
        self.assertEqual(hawks["points_behind_last_place_through"], 3)  # Green Giants 6
        self.assertEqual(hawks["status"], "out in the group stage")
        self.assertEqual(rows["Blue Jays"]["status"], "out in the semi-final")
        self.assertEqual(snap["tournament"]["phase"], "knockout")

    def test_stages_and_undecided_matches(self):
        snap = build_snapshot(tt.hybrid_after_one_semi(self.org), self.org)
        semi = next(r for r in snap["results"] if r["stage"] == "Semi-final")
        self.assertEqual((semi["team1"], semi["score1"]), ("Red Rovers", 3))
        self.assertNotIn("round", semi)
        teams = {v for f in snap["fixtures"] for v in (f["team1"], f["team2"])}
        self.assertNotIn("TBD", teams)
        final = next(f for f in snap["fixtures"] if f["stage"] == "Final")
        self.assertEqual({final["team1"], final["team2"]}, {"Red Rovers", "to be decided"})
        self.assertEqual(snap["tournament"]["matches_left"], 1)     # the other semi-final

    def test_placeholders_are_counted_by_stage(self):
        snap = build_snapshot(tt.hybrid_after_groups(self.org), self.org)
        self.assertEqual(snap["later_matches_to_be_decided"], [{"stage": "Final", "matches": 1}])

    def test_withdrawn_rows_stay_flagged(self):
        t = tt.league_with_withdrawal(self.org)
        snap = build_snapshot(t, self.org)
        ranks = [r["rank"] for r in snap["table"]]
        self.assertEqual(ranks, list(range(1, 7)))
        jays = next(r for r in snap["table"] if r["team"] == "Blue Jays")
        self.assertTrue(jays["withdrawn"])
        self.assertEqual(jays["status"], "withdrew")
        self.assertEqual(snap["withdrawn"], ["Blue Jays"])

    def test_leader_gap_ignores_a_withdrawn_leader(self):
        t = tt.make_league(self.org, tt.NAMES[:4])
        for match in t.matches.filter(Q(team1__name="Blue Jays") | Q(team2__name="Blue Jays")).order_by("match_number")[:2]:
            tt.play(match, *((1, 0) if match.team1.name == "Blue Jays" else (0, 1)))
        tt.withdraw(t, tt.team("Blue Jays"), policy="void")
        snap = build_snapshot(t, self.org)
        self.assertEqual(snap["table"][0]["team"], "Blue Jays")
        second = snap["table"][1]
        self.assertEqual(second["points_behind_leader"], 0)

    def test_awaiting_confirmation(self):
        t = tt.make_league(self.org, tt.NAMES[:4])
        match = t.matches.order_by("match_number").first()
        match.score_team1, match.score_team2, match.status = 2, 1, "pending_confirmation"
        match.save()
        snap = build_snapshot(t, self.org)
        self.assertEqual([a["match"] for a in snap["awaiting_confirmation"]], [match.match_number])
        self.assertNotIn(match.match_number, [f["match"] for f in snap["fixtures"]])
        self.assertEqual(snap["tournament"]["matches_left"], 5)
        self.assertNotIn("score1", snap["awaiting_confirmation"][0])

    def test_unscheduled_result_has_no_broken_date(self):
        t = tt.make_league(self.org, tt.NAMES[:4])
        match = t.matches.order_by("match_number").first()
        Match.objects.filter(pk=match.pk).update(scheduled_time=None)
        tt.play(match, 1, 0)
        Match.objects.filter(pk=match.pk).update(score_submitted_at=None)
        snap = build_snapshot(t, self.org)
        self.assertIsNone(snap["results"][0]["date"])
        self.assertNotIn('"not schedu"', serialise(snap))

    def test_fits(self):
        snap = build_snapshot(tt.hybrid_finished(self.org), self.org)
        self.assertLess(len(serialise(snap)), MAX_SNAPSHOT_CHARS)
        self.assertEqual(snap["tournament"]["placings"]["champion"], "Red Rovers")

    def test_conversation_prompt_has_the_rule(self):
        self.assertIn(STRUCTURE_RULE, CONVERSATION_PROMPT)


def _publish(tournament):
    """A published news update, as the worker would leave it."""
    previous = recap.latest_recap(tournament)
    facts, covered, _, memory = recap.build_recap_facts(tournament, previous)
    return AIQuestion.objects.create(
        tournament=tournament, kind="recap", question="Automatic news update", status="done",
        answer_verified=True, finished_at=timezone.now(), facts=facts, answer="News",
        route={"kind": "recap", "covered_match_ids": covered, "story": {"intro": "News"},
               "positions": memory["positions"], "statuses": memory["statuses"]},
    )


def _story_reply(parts):
    return json.dumps({"story": {p: "Great games." for p in parts}, "results": [], "previews": []})


class NewsStructureTests(TestCase):
    """ST-8 (G-1, G-4, G-6, G-7, K-1, K-2, K-5, W-1, W-3): the news board is
    told about groups, stages, statuses and withdrawals, and is asked for a
    story part that fits the format."""

    def setUp(self):
        self.org = _make_organizer()

    def _group_rounds(self, t, rounds, upset=()):
        for match in t.matches.exclude(group="").filter(round_number__in=rounds).order_by("match_number"):
            tt.play(match, *((2, 0) if tt._stronger_first(match, upset) else (0, 2)))

    def _write(self, t, parts):
        job = AIQuestion.objects.create(tournament=t, kind="recap", question="Automatic news update")
        with FakeOllama() as fake:
            fake.respond_chat(_story_reply(parts))
            recap.write_recap(job)
        return job, fake.requests[0]["body"]

    def test_group_stage_facts_and_part(self):
        t = tt.make_hybrid(self.org)
        self._group_rounds(t, [1, 2])
        facts, _, _, memory = recap.build_recap_facts(t)
        self.assertNotIn("standings_top", facts)
        self.assertEqual([g["group"] for g in facts["groups"]], ["A", "B"])
        self.assertEqual(memory["part"], "groups")
        self.assertEqual({r["stage"] for r in facts["new_results"]}, {"Group A", "Group B"})
        _, body = self._write(t, recap.story_parts(False, "groups"))
        required = body["format"]["properties"]["story"]["required"]
        self.assertIn("groups", required)
        self.assertNotIn("table", required)
        self.assertIn(STRUCTURE_RULE, body["messages"][0]["content"])

    def test_position_changes_stay_within_a_group(self):
        t = tt.make_hybrid(self.org)
        self._group_rounds(t, [1, 2])
        _publish(t)
        match = t.matches.get(group="B", round_number=3, team1__name="Golden Boots")
        tt.play(match, 0, 2)          # Purple Pumas beat Golden Boots
        facts, *_ = recap.build_recap_facts(t, recap.latest_recap(t))
        moves = facts["position_changes_since_last_recap"]
        self.assertEqual({m["team"] for m in moves}, {"Purple Pumas", "Golden Boots", "Blue Jays"})
        self.assertEqual({m["group"] for m in moves}, {"B"})

    def test_knockout_phase_reports_status_changes(self):
        t = tt.hybrid_after_groups(self.org)
        _publish(t)
        semi = next(m for m in tt.ready(t, "winners") if m.team1.name == "Red Rovers")
        tt.play(semi, 3, 1)
        facts, _, _, memory = recap.build_recap_facts(t, recap.latest_recap(t))
        self.assertEqual(memory["part"], "knockouts")
        row = next(r for r in facts["new_results"] if r["team1"] == "Red Rovers")
        self.assertEqual(row["stage"], "Semi-final")
        changes = {c["team"]: (c["was"], c["now"]) for c in facts["status_changes_since_last_recap"]}
        self.assertEqual(changes["Blue Jays"], ("through to the semi-final", "out in the semi-final"))
        self.assertEqual(changes["Red Rovers"], ("through to the semi-final", "through to the final"))
        self.assertNotIn("position_changes_since_last_recap", facts)

    def test_hybrid_finale(self):
        t = tt.hybrid_finished(self.org)
        facts, _, _, memory = recap.build_recap_facts(t)
        self.assertEqual((facts["champion"], facts["runner_up"]), ("Red Rovers", "Green Giants"))
        self.assertNotIn("standings_top", facts)
        self.assertEqual(memory["part"], "knockouts")
        _, body = self._write(t, recap.story_parts(True, "knockouts"))
        self.assertEqual(body["format"]["properties"]["story"]["required"],
                         ["title", "intro", "champion", "results", "knockouts", "sign_off"])

    def test_knockout_is_a_bracket_not_a_table(self):
        t = tt.knockout_after_round_1(self.org)
        _, body = self._write(t, recap.story_parts(False, "bracket"))
        required = body["format"]["properties"]["story"]["required"]
        self.assertIn("bracket", required)
        self.assertNotIn("table", required)
        system = body["messages"][0]["content"]
        self.assertNotIn("top of the table", system)
        facts = json.loads(body["messages"][1]["content"].split("FACTS:\n<<<\n", 1)[1].rsplit("\n>>>", 1)[0])
        self.assertEqual(facts["bracket"]["next_round"], "Semi-final")
        self.assertEqual({r["stage"] for r in facts["new_results"]}, {"Quarter-final"})

    def test_withdrawals_are_reported_and_walkovers_marked(self):
        t = tt.make_league(self.org, tt.NAMES[:4])
        tt.withdraw(t, tt.team("Blue Jays"), policy="forfeit")
        facts, *_ = recap.build_recap_facts(t)
        self.assertEqual([w["team"] for w in facts["withdrawals"]], ["Blue Jays"])
        walkovers = [r for r in facts["new_results"] if r.get("walkover_after_withdrawal")]
        self.assertEqual(len(walkovers), 3)
        jays = next(r for r in facts["standings_top"] if r["team"] == "Blue Jays")
        self.assertTrue(jays["withdrawn"])

    def test_withdrawals_reported_once(self):
        t = tt.make_league(self.org, tt.NAMES[:4])
        tt.withdraw(t, tt.team("Blue Jays"), policy="void")
        _publish(t)
        tt.play(t.matches.filter(status="upcoming").order_by("match_number").first(), 1, 0)
        facts, *_ = recap.build_recap_facts(t, recap.latest_recap(t))
        self.assertNotIn("withdrawals", facts)

    def test_an_update_from_before_groups_were_understood(self):
        t = tt.make_hybrid(self.org)
        self._group_rounds(t, [1])
        AIQuestion.objects.create(
            tournament=t, kind="recap", question="Automatic news update", status="done",
            answer_verified=True, finished_at=timezone.now(), answer="Old news",
            facts={"standings_top": [{"rank": 1, "team": "Red Rovers", "points": 3}]},
            route={"kind": "recap", "covered_match_ids": list(t.matches.filter(status="confirmed")
                                                             .values_list("pk", flat=True)),
                   "story": {"intro": "Old news"}},
        )
        self._group_rounds(t, [2])
        facts, *_ = recap.build_recap_facts(t, recap.latest_recap(t))
        self.assertNotIn("position_changes_since_last_recap", facts)
        self.assertEqual(len(facts["new_results"]), 4)

    def test_new_parts_render(self):
        html = render_to_string("core/partials/news_story.html",
                                {"story": {"title": "T", "groups": "Group A is wild."}, "final": False})
        self.assertIn("The Groups", html)
        self.assertIn("Group A is wild.", html)


class TeamNewsStructureTests(TestCase):
    """ST-9 (G-1, G-5, K-3, K-4, K-7): "My team's take" knows the team's
    group, its status, its stages and its byes."""

    def setUp(self):
        self.org = _make_organizer()

    def _facts(self, tournament, name):
        tournament.refresh_from_db()
        return team_news.build_team_facts(tournament, tt.team(name))

    def _write(self, tournament, name, parts):
        job = AIQuestion.objects.create(tournament=tournament, kind="team_news", question="take",
                                        route={"team_id": tt.team(name).pk})
        with FakeOllama() as fake:
            fake.respond_chat(json.dumps({"story": {p: "Nice one." for p in parts}}))
            team_news.write_team_news(job)
        return fake.requests[0]["body"]

    def test_group_stage_story_stays_in_the_group(self):
        t = tt.make_hybrid(self.org)
        for match in t.matches.exclude(group="").filter(round_number__lte=2).order_by("match_number"):
            tt.play(match, *((2, 0) if tt._stronger_first(match) else (0, 2)))
        facts, part, run_over = self._facts(t, "Blue Jays")
        self.assertEqual((facts["your_group"], part, run_over), ("B", "group", False))
        group_b = {"Golden Boots", "Purple Pumas", "Orange Owls"}
        self.assertTrue({r["team"] for r in facts["teams_around_you"]} <= group_b)
        self.assertEqual(facts["leader"]["team"], "Golden Boots")
        self.assertEqual(facts["teams_in_group"], 4)
        self.assertEqual({r["stage"] for r in facts["your_results"]}, {"Group B"})
        body = self._write(t, "Blue Jays", recap.story_parts(False, "group"))
        self.assertIn("group", body["format"]["properties"]["story"]["required"])
        self.assertNotIn("table", body["format"]["properties"]["story"]["required"])

    def test_out_in_the_semi_final(self):
        t = tt.hybrid_after_one_semi(self.org)
        facts, part, run_over = self._facts(t, "Blue Jays")
        self.assertEqual((facts["your_status"], part, run_over), ("out in the semi-final", "run", True))
        semi = facts["your_results"][-1]
        self.assertEqual((semi["stage"], semi["result"]), ("Semi-final", "lost"))
        body = self._write(t, "Blue Jays", recap.story_parts(False, "run"))
        self.assertIn(team_news.RUN_OVER_NOTE, body["messages"][0]["content"])
        self.assertIn("run", body["format"]["properties"]["story"]["required"])

    def test_still_in(self):
        facts, part, run_over = self._facts(tt.hybrid_after_one_semi(self.org), "Red Rovers")
        self.assertEqual((facts["your_status"], part, run_over), ("through to the final", "run", False))
        # The final's other side isn't known yet, so no next match to hype.
        self.assertEqual(facts["your_next_matches"], [])

    def test_knockout_loser(self):
        facts, part, _ = self._facts(tt.knockout_after_round_1(self.org), "Black Bears")
        self.assertEqual((facts["your_status"], part), ("out in the quarter-final", "run"))
        self.assertNotIn("your_standing", facts)
        self.assertNotIn("teams_around_you", facts)

    def test_double_elimination_one_life(self):
        t = tt.make_double_elimination(self.org)
        tt.play_ready(t, "winners")
        facts, _, run_over = self._facts(t, "Black Bears")
        self.assertIn("losers bracket", facts["your_status"])
        self.assertIn("one more", facts["your_status"])
        self.assertFalse(run_over)
        self.assertEqual(facts["your_next_matches"][0]["stage"], "Losers bracket round 1")

    def test_a_bye_is_a_result(self):
        t = tt.make_knockout(self.org, tt.NAMES[:6])
        bye = t.matches.filter(status="bye").exclude(winner=None).select_related("winner").first()
        facts, *_ = self._facts(t, bye.winner.name)
        self.assertEqual(facts["your_results"], [{"played": facts["your_results"][0]["played"],
                                                  "stage": "Quarter-final", "result": "advanced with a bye"}])

    def test_a_withdrawn_neighbour_is_skipped(self):
        t = tt.league_with_withdrawal(self.org)
        facts, part, _ = self._facts(t, "Red Rovers")
        self.assertEqual(part, "table")
        self.assertNotIn("Blue Jays", [r["team"] for r in facts["teams_around_you"]])
        self.assertEqual(facts["teams_in_table"], 5)


class CorrectedResultTests(TestCase):
    """ST-10 (S-2): a corrected score no longer keeps the headline written
    about the old one, and the next update tells the correction."""

    def setUp(self):
        self.org = _make_organizer()
        self.t = tt.make_league(self.org, tt.NAMES[:4])
        self.match = self.t.matches.get(team1__name="Red Rovers", team2__name="Golden Boots")
        tt.play(self.match, 3, 0)

    def _write_update(self, headline):
        job = AIQuestion.objects.create(tournament=self.t, kind="recap", question="Automatic news update")
        with FakeOllama() as fake:
            fake.respond_chat(json.dumps({"story": {"title": "News", "intro": "Big day."},
                                          "results": [{"key": "r1", "headline": headline}], "previews": []}))
            recap.write_recap(job)
        job.status, job.finished_at = "done", timezone.now()
        job.save()
        return job, json.loads(fake.requests[0]["body"]["messages"][1]["content"]
                               .split("FACTS:\n<<<\n", 1)[1].rsplit("\n>>>", 1)[0])

    def _headline(self):
        board = recap.news_board(self.t)
        items = [i for s in board["sections"] for i in s["items"] if i["match"].pk == self.match.pk]
        return items[0]["headline"]

    def _override(self, s1, s2):
        self.client.force_login(self.org)
        self.client.post(f"/match/{self.match.pk}/override-result/",
                         {"override_score_team1": s1, "override_score_team2": s2, "override_reason": "typo"})
        self.match.refresh_from_db()
        self.assertEqual((self.match.score_team1, self.match.score_team2), (s1, s2))

    def test_the_old_headline_goes_and_the_correction_is_told(self):
        self._write_update("Rovers romp 3-0")
        self.assertEqual(self._headline(), "Rovers romp 3-0")
        self._override(1, 3)
        self.assertEqual(self._headline(), "")
        self.assertTrue(recap.new_results(self.t, recap.latest_recap(self.t)).filter(pk=self.match.pk).exists())
        _, facts = self._write_update("Boots bounce back 3-1")
        row = next(r for r in facts["new_results"] if r["team1"] == "Red Rovers")
        self.assertEqual((row["score1"], row["score2"], row.get("corrected")), (1, 3, True))
        self.assertEqual(self._headline(), "Boots bounce back 3-1")

    def test_an_unchanged_result_stays_covered(self):
        self._write_update("Rovers romp 3-0")
        self.assertFalse(recap.new_results(self.t, recap.latest_recap(self.t)).exists())

    def test_updates_from_before_keep_their_headlines(self):
        job, _ = self._write_update("Rovers romp 3-0")
        route = dict(job.route)
        route.pop("covered_scores")
        AIQuestion.objects.filter(pk=job.pk).update(route=route)
        self._override(1, 3)
        self.assertEqual(self._headline(), "Rovers romp 3-0")
        self.assertFalse(recap.new_results(self.t, recap.latest_recap(self.t)).filter(pk=self.match.pk).exists())


class WordingTests(TestCase):
    """ST-11 (T-2, T-3): scores and competitors in the tournament's words."""

    def setUp(self):
        self.org = _make_organizer()

    def test_score_unit_and_participant_everywhere(self):
        t = tt.make_league(self.org, tt.NAMES[:4], sport_type="badminton", players_per_team=1)
        tt.play(t.matches.order_by("match_number").first(), 2, 1)
        blocks = [
            build_facts(t, self.org, Route("standings"))["tournament"],
            build_snapshot(t, self.org)["tournament"],
            recap.build_recap_facts(t)[0]["tournament"],
            team_news.build_team_facts(t, tt.team("Red Rovers"))[0]["tournament"],
        ]
        for block in blocks:
            self.assertEqual((block["score_unit"], block["participant"]), ("games", "player"))

    def test_teams_and_goals(self):
        t = tt.make_league(self.org, tt.NAMES[:4], players_per_team=5)
        block = build_facts(t, self.org, Route("standings"))["tournament"]
        self.assertEqual((block["score_unit"], block["participant"]), ("goals", "team"))

    def test_every_prompt_explains_the_words(self):
        for prompt in (EXPLAIN_PROMPT, CONVERSATION_PROMPT, recap.RECAP_PROMPT, team_news.PROMPT):
            self.assertIn(WORDING_RULE, prompt)
            self.assertIn(STRUCTURE_RULE, prompt)

    def test_individual_events_say_my_take(self):
        t = tt.make_league(self.org, tt.NAMES[:4], registration_mode="individual")
        html = render_to_string("core/partials/news_flip.html", {"tournament": t, "mode": "team"})
        self.assertIn("My take", html)
        self.assertNotIn("My team's take", html)
