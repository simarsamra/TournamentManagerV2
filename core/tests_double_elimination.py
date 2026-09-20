"""Double elimination with a real losers bracket.

Before this, `generate_double_elimination` produced a winners bracket only, so
a team was out after one defeat and the format did not do what its name says.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from core.models import (
    Match, OrganizerProfile, Team, TeamTournamentParticipation, Tournament,
)
from core.scheduling import (
    _losers_bracket_shape, estimate_required_matches, generate_double_elimination,
)
from core.standings import (
    _determine_champion, advance_winner, get_losers_bracket_data,
)


def make_tournament(n, bracket_reset=True, name="DE"):
    organizer = User.objects.create_user(f"de_org_{name}", password="Double-Elim-1")
    OrganizerProfile.objects.filter(user=organizer).update(verified=True)
    tournament = Tournament.objects.create(
        name=name, format="double_elimination", players_per_team=1,
        start_date=timezone.localdate(), created_by=organizer,
        enable_bracket_reset=bracket_reset,
    )
    for i in range(n):
        team = Team.objects.create(name=f"{name}-T{i + 1}")
        TeamTournamentParticipation.objects.create(
            team=team, tournament=tournament, status="active", seed=i + 1,
        )
    return tournament


def play(match, winner):
    """Confirm `match` with `winner` and propagate, as the views do."""
    match.refresh_from_db()
    match.winner = winner
    match.score_team1 = 3 if winner == match.team1 else 0
    match.score_team2 = 3 if winner == match.team2 else 0
    match.status = "confirmed"
    match.save()
    advance_winner(match)
    return match


def playable(tournament):
    """Matches ready to be played: both teams known, not yet decided."""
    return list(
        tournament.matches.filter(
            status="upcoming", team1__isnull=False, team2__isnull=False,
        ).order_by("match_number")
    )


def run_to_completion(tournament, pick=lambda m: m.team1):
    """Play the whole bracket, always picking with `pick`. Returns match count."""
    played = 0
    for _ in range(200):
        ready = playable(tournament)
        if not ready:
            break
        play(ready[0], pick(ready[0]))
        played += 1
    else:
        raise AssertionError("bracket did not terminate")
    return played


class ShapeTests(TestCase):
    def test_four_team_shape(self):
        self.assertEqual(
            _losers_bracket_shape(4), [("seed", 1, 1), ("absorb", 1, 2)]
        )

    def test_eight_team_shape(self):
        self.assertEqual(
            _losers_bracket_shape(8),
            [("seed", 2, 1), ("absorb", 2, 2), ("pair", 1, None), ("absorb", 1, 3)],
        )

    def test_sixteen_team_shape_totals_bracket_size_minus_two(self):
        shape = _losers_bracket_shape(16)
        self.assertEqual(sum(count for _, count, _ in shape), 14)
        self.assertEqual(len(shape), 6)

    def test_two_team_bracket_has_no_losers_rounds(self):
        self.assertEqual(_losers_bracket_shape(2), [])


class StructureTests(TestCase):
    def test_eight_teams_creates_the_full_bracket(self):
        tournament = make_tournament(8, name="S8")
        generate_double_elimination(tournament)

        counts = {
            kind: tournament.matches.filter(bracket_type=kind).count()
            for kind in ("winners", "losers", "grand_final")
        }
        self.assertEqual(counts["winners"], 7)
        self.assertEqual(counts["losers"], 6)
        self.assertEqual(counts["grand_final"], 2)   # final + decider

    def test_a_losers_bracket_is_actually_created(self):
        """The regression this whole feature exists for."""
        tournament = make_tournament(8, name="S8b")
        generate_double_elimination(tournament)
        self.assertTrue(tournament.matches.filter(bracket_type="losers").exists())

    def test_every_winners_match_but_the_final_sends_its_loser_down(self):
        tournament = make_tournament(8, name="S8c")
        generate_double_elimination(tournament)

        winners = tournament.matches.filter(bracket_type="winners")
        final = winners.order_by("-round_number").first()
        for match in winners.exclude(pk=final.pk):
            with self.subTest(match=match.match_number):
                self.assertIsNotNone(
                    match.next_loser_match_id,
                    f"WB r{match.round_number} p{match.bracket_position} drops nobody",
                )

    def test_the_winners_final_loser_reaches_the_last_losers_round(self):
        tournament = make_tournament(8, name="S8d")
        generate_double_elimination(tournament)
        wb_final = tournament.matches.filter(bracket_type="winners").order_by("-round_number").first()
        self.assertIsNotNone(wb_final.next_loser_match_id)
        self.assertEqual(
            Match.objects.get(pk=wb_final.next_loser_match_id).bracket_type, "losers"
        )

    def test_grand_final_is_fed_by_both_brackets(self):
        tournament = make_tournament(8, name="S8e")
        generate_double_elimination(tournament)
        gf = tournament.matches.get(bracket_type="grand_final", round_number=1)

        feeders = {m.bracket_type for m in gf.previous_matches.all()}
        self.assertEqual(feeders, {"winners", "losers"})

    def test_no_decider_when_bracket_reset_is_off(self):
        tournament = make_tournament(8, bracket_reset=False, name="S8f")
        generate_double_elimination(tournament)
        self.assertEqual(tournament.matches.filter(bracket_type="grand_final").count(), 1)


class CompletionTests(TestCase):
    def test_eight_teams_plays_out_and_produces_a_champion(self):
        tournament = make_tournament(8, name="C8")
        generate_double_elimination(tournament)
        run_to_completion(tournament)

        champion = _determine_champion(tournament)
        self.assertIsNotNone(champion)

    def test_the_champion_is_the_last_grand_final_winner(self):
        tournament = make_tournament(8, name="C8b")
        generate_double_elimination(tournament)
        run_to_completion(tournament)

        last_gf = (
            tournament.matches.filter(bracket_type="grand_final", status="confirmed")
            .order_by("-round_number").first()
        )
        self.assertEqual(_determine_champion(tournament), last_gf.winner)

    def test_nobody_is_eliminated_on_a_single_defeat(self):
        """The defining property of the format, and what was broken."""
        tournament = make_tournament(8, name="C8c")
        generate_double_elimination(tournament)
        run_to_completion(tournament)

        losses = {}
        for match in tournament.matches.filter(status="confirmed"):
            if not match.winner_id:
                continue
            loser = match.team2_id if match.winner_id == match.team1_id else match.team1_id
            if loser:
                losses[loser] = losses.get(loser, 0) + 1

        champion = _determine_champion(tournament)
        eliminated = [tid for tid, count in losses.items() if tid != champion.id]
        self.assertTrue(eliminated)
        for team_id in eliminated:
            with self.subTest(team=team_id):
                self.assertEqual(
                    losses[team_id], 2,
                    "an eliminated team must have lost exactly twice",
                )

    def test_four_teams_plays_out(self):
        tournament = make_tournament(4, name="C4")
        generate_double_elimination(tournament)
        run_to_completion(tournament)
        self.assertIsNotNone(_determine_champion(tournament))

    def test_two_teams_plays_out(self):
        tournament = make_tournament(2, name="C2")
        generate_double_elimination(tournament)
        run_to_completion(tournament)
        self.assertIsNotNone(_determine_champion(tournament))

    def test_sixteen_teams_plays_out(self):
        tournament = make_tournament(16, name="C16")
        generate_double_elimination(tournament)
        run_to_completion(tournament)
        self.assertIsNotNone(_determine_champion(tournament))


class ByeTests(TestCase):
    """Non-power-of-two fields. A winners round-1 bye produces no loser, so the
    losers bracket has slots nothing can ever fill."""

    def test_five_teams_plays_out(self):
        tournament = make_tournament(5, name="B5")
        generate_double_elimination(tournament)
        run_to_completion(tournament)
        self.assertIsNotNone(_determine_champion(tournament))

    def test_six_teams_plays_out(self):
        tournament = make_tournament(6, name="B6")
        generate_double_elimination(tournament)
        run_to_completion(tournament)
        self.assertIsNotNone(_determine_champion(tournament))

    def test_seven_teams_plays_out(self):
        tournament = make_tournament(7, name="B7")
        generate_double_elimination(tournament)
        run_to_completion(tournament)
        self.assertIsNotNone(_determine_champion(tournament))

    def test_eleven_teams_plays_out(self):
        tournament = make_tournament(11, name="B11")
        generate_double_elimination(tournament)
        run_to_completion(tournament)
        self.assertIsNotNone(_determine_champion(tournament))

    def test_walkover_matches_are_not_scheduled(self):
        tournament = make_tournament(5, name="B5b")
        generate_double_elimination(tournament)
        for match in tournament.matches.filter(status="bye"):
            with self.subTest(match=match.match_number):
                self.assertIsNone(match.scheduled_time)


class BracketResetTests(TestCase):
    def _reach_grand_final(self, tournament):
        for _ in range(200):
            gf = tournament.matches.get(bracket_type="grand_final", round_number=1)
            if gf.team1_id and gf.team2_id:
                return gf
            ready = [m for m in playable(tournament) if m.bracket_type != "grand_final"]
            if not ready:
                return gf
            play(ready[0], ready[0].team1)
        raise AssertionError("never reached the grand final")

    def test_decider_is_cancelled_when_the_winners_champion_wins(self):
        tournament = make_tournament(8, name="R8")
        generate_double_elimination(tournament)
        gf = self._reach_grand_final(tournament)
        play(gf, gf.team1)          # team1 is the winners-bracket champion

        decider = tournament.matches.get(bracket_type="grand_final", round_number=2)
        self.assertEqual(decider.status, "cancelled")
        self.assertEqual(_determine_champion(tournament), gf.winner)

    def test_decider_is_played_when_the_losers_champion_wins(self):
        tournament = make_tournament(8, name="R8b")
        generate_double_elimination(tournament)
        gf = self._reach_grand_final(tournament)
        play(gf, gf.team2)          # team2 came up through the losers bracket

        decider = tournament.matches.get(bracket_type="grand_final", round_number=2)
        self.assertEqual(decider.status, "upcoming")
        self.assertEqual(decider.team1_id, gf.team1_id)
        self.assertEqual(decider.team2_id, gf.team2_id)

        play(decider, decider.team2)
        self.assertEqual(_determine_champion(tournament), decider.team2)

    def test_without_reset_the_grand_final_settles_it(self):
        tournament = make_tournament(8, bracket_reset=False, name="R8c")
        generate_double_elimination(tournament)
        gf = self._reach_grand_final(tournament)
        play(gf, gf.team2)
        self.assertEqual(_determine_champion(tournament), gf.team2)


class EstimateTests(TestCase):
    def test_estimate_matches_what_is_generated(self):
        for n in (2, 4, 5, 8, 11, 16):
            with self.subTest(teams=n):
                tournament = make_tournament(n, bracket_reset=False, name=f"E{n}")
                generate_double_elimination(tournament)
                real = tournament.matches.exclude(status="bye").count()
                estimate = estimate_required_matches(tournament, team_count=n)
                self.assertGreaterEqual(
                    estimate, real,
                    f"{n} teams: estimate {estimate} under-reserves {real} matches",
                )
                self.assertLessEqual(
                    estimate - real, n,
                    f"{n} teams: estimate {estimate} over-reserves vs {real}",
                )

    def test_bracket_reset_reserves_one_more(self):
        with_reset = make_tournament(8, bracket_reset=True, name="Ea")
        without = make_tournament(8, bracket_reset=False, name="Eb")
        self.assertEqual(
            estimate_required_matches(with_reset, team_count=8),
            estimate_required_matches(without, team_count=8) + 1,
        )


class TournamentCompletionTests(TestCase):
    """The completion check looked for a winners final with no next_match.
    In double elimination the winners final now feeds the grand final, so that
    query finds nothing and the tournament would never complete."""

    def _activate(self, tournament):
        tournament.status = "active"
        tournament.save(update_fields=["status"])

    def test_tournament_completes_and_records_the_champion(self):
        from core.views import _check_and_finalize_tournament

        tournament = make_tournament(8, name="TC8")
        generate_double_elimination(tournament)
        self._activate(tournament)
        run_to_completion(tournament)

        self.assertTrue(_check_and_finalize_tournament(tournament))
        tournament.refresh_from_db()
        self.assertEqual(tournament.status, "completed")
        self.assertIsNotNone(tournament.champion)

    def test_tournament_does_not_complete_while_the_losers_bracket_runs(self):
        from core.views import _check_and_finalize_tournament

        tournament = make_tournament(8, name="TC8b")
        generate_double_elimination(tournament)
        self._activate(tournament)

        # Play only the winners bracket through to its final.
        for _ in range(50):
            ready = [m for m in playable(tournament) if m.bracket_type == "winners"]
            if not ready:
                break
            play(ready[0], ready[0].team1)

        self.assertFalse(_check_and_finalize_tournament(tournament))
        tournament.refresh_from_db()
        self.assertEqual(tournament.status, "active")

    def test_completing_after_a_decider_uses_the_decider_winner(self):
        from core.views import _check_and_finalize_tournament

        tournament = make_tournament(8, name="TC8c")
        generate_double_elimination(tournament)
        self._activate(tournament)

        for _ in range(200):
            gf = tournament.matches.get(bracket_type="grand_final", round_number=1)
            if gf.team1_id and gf.team2_id:
                break
            ready = [m for m in playable(tournament) if m.bracket_type != "grand_final"]
            if not ready:
                break
            play(ready[0], ready[0].team1)

        play(gf, gf.team2)                 # losers champion forces the decider
        self.assertFalse(_check_and_finalize_tournament(tournament))

        decider = tournament.matches.get(bracket_type="grand_final", round_number=2)
        play(decider, decider.team1)

        self.assertTrue(_check_and_finalize_tournament(tournament))
        tournament.refresh_from_db()
        self.assertEqual(tournament.champion, decider.winner)


class StandingsPageTests(TestCase):
    """The losers bracket must actually reach the page; get_bracket_data only
    ever returned the winners side."""

    def test_standings_page_renders_all_three_brackets(self):
        tournament = make_tournament(8, name="P8")
        generate_double_elimination(tournament)
        tournament.status = "active"
        tournament.save(update_fields=["status"])
        run_to_completion(tournament)

        user = User.objects.create_user("de_viewer", password="Double-Elim-1")
        self.client.force_login(user)
        session = self.client.session
        session["tournament_id"] = tournament.pk
        session.save()

        response = self.client.get("/standings/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("losers_bracket", response.context)
        self.assertTrue(response.context["losers_bracket"])
        self.assertTrue(response.context["grand_final_matches"])

        body = response.content.decode()
        self.assertIn("Winners Bracket", body)
        self.assertIn("Losers Bracket", body)
        self.assertIn("Grand Final", body)

    def test_walkover_matches_are_not_displayed(self):
        tournament = make_tournament(5, name="P5")
        generate_double_elimination(tournament)
        shown = [
            m
            for round_matches in get_losers_bracket_data(tournament).values()
            for m in round_matches
        ]
        self.assertTrue(shown)
        for match in shown:
            self.assertNotEqual(match.status, "bye")
