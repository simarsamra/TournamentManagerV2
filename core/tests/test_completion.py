"""Auto-completion when the last match of a tournament is confirmed or forfeited.

Split out of the old core/tests.py — see FOLLOWUP_PLAN.md F-3.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from ..models import (
    Team,
    Tournament,
    Court,
    TimeSlot,
    TeamMembership,
    TeamTournamentParticipation,
)
from ..scheduling import generate_fixtures
from ..scheduling import generate_consolation_if_ready
from ..standings import advance_winner
from ..standings import _determine_champion
from ..views import _check_and_finalize_tournament
from .helpers import _captain_user, _participation


class TournamentCompletionTests(TestCase):
    """Tests for auto-detection of tournament completion and champion logic."""

    def setUp(self):
        self.organizer = User.objects.create_user(
            username="comp_organizer", password="pass123", is_staff=True
        )

    def _create_tournament(self, fmt="round_robin", name="CompTest"):
        return Tournament.objects.create(
            name=name,
            format=fmt,
            sport_type="badminton",
            status="active",
            points_per_win=3,
            points_per_loss=0,
            points_per_draw=1,
            num_groups=2,
            teams_per_group_advance=1,
            default_match_duration=30,
        )

    def _create_team(self, tournament, team_name, username=None, seed=0):
        username = username or team_name.lower().replace(" ", "_") + "_comp"
        user = User.objects.create_user(username=username, password="pass123")
        team, _ = Team.objects.get_or_create(name=team_name)
        TeamTournamentParticipation.objects.get_or_create(
            team=team, tournament=tournament, defaults={"status": "active", "seed": seed}
        )
        TeamMembership.objects.get_or_create(team=team, user=user, defaults={"role": "captain"})
        return team

    def _confirm_match(self, match, s1, s2):
        """Directly confirm a match and advance winner."""
        match.score_team1 = s1
        match.score_team2 = s2
        if s1 > s2:
            match.winner = match.team1
        elif s2 > s1:
            match.winner = match.team2
        else:
            match.winner = None
        match.status = "confirmed"
        match.save()
        advance_winner(match)

    # ---- Round Robin ----

    def test_round_robin_auto_complete(self):
        """Confirming the last RR match should mark tournament completed with champion."""
        t = self._create_tournament(fmt="round_robin")
        [self._create_team(t, f"RR{i}") for i in range(1, 4)]
        generate_fixtures(t)

        matches = list(t.matches.filter(team1__isnull=False, team2__isnull=False).order_by("match_number"))
        self.assertGreater(len(matches), 0)

        # Confirm all but last directly
        for m in matches[:-1]:
            self._confirm_match(m, 3, 0)

        # Confirm last via _check_and_finalize_tournament pathway
        last = matches[-1]
        self._confirm_match(last, 3, 0)
        _check_and_finalize_tournament(t)

        t.refresh_from_db()
        self.assertEqual(t.status, "completed")
        self.assertIsNotNone(t.completed_at)
        self.assertIsNotNone(t.champion)

    def test_double_round_robin_auto_complete(self):
        """DRR completion check works correctly."""
        t = self._create_tournament(fmt="double_round_robin", name="DRRComp")
        [self._create_team(t, f"DRR{i}") for i in range(1, 4)]
        generate_fixtures(t)

        matches = list(t.matches.filter(team1__isnull=False, team2__isnull=False).order_by("match_number"))
        for m in matches[:-1]:
            self._confirm_match(m, 2, 0)

        # Should not be complete yet
        _check_and_finalize_tournament(t)
        t.refresh_from_db()
        self.assertEqual(t.status, "active")

        # Confirm last
        self._confirm_match(matches[-1], 2, 0)
        _check_and_finalize_tournament(t)
        t.refresh_from_db()
        self.assertEqual(t.status, "completed")
        self.assertIsNotNone(t.champion)

    # ---- Knockout ----

    def test_knockout_auto_complete(self):
        """Confirming the KO final triggers tournament completion."""
        t = self._create_tournament(fmt="knockout", name="KOComp")
        for i in range(1, 5):
            self._create_team(t, f"KO{i}", seed=i)
        generate_fixtures(t)

        # Semi-finals
        semis = list(t.matches.filter(round_number=1).order_by("bracket_position"))
        for m in semis:
            self._confirm_match(m, 3, 1)

        # Reload final (team slots filled by advance_winner)
        final = t.matches.filter(round_number=2).first()
        final.refresh_from_db()
        self._confirm_match(final, 3, 1)
        _check_and_finalize_tournament(t)

        t.refresh_from_db()
        self.assertEqual(t.status, "completed")
        self.assertEqual(t.champion, final.winner)

    def test_no_complete_while_matches_pending(self):
        """Confirming a non-final match should NOT complete the tournament."""
        t = self._create_tournament(fmt="knockout", name="KONonFinal")
        for i in range(1, 5):
            self._create_team(t, f"KON{i}", seed=i)
        generate_fixtures(t)

        semi = t.matches.filter(round_number=1).first()
        self._confirm_match(semi, 3, 1)
        _check_and_finalize_tournament(t)

        t.refresh_from_db()
        self.assertEqual(t.status, "active")
        self.assertIsNone(t.champion)

    # ---- Double Elimination ----

    def test_double_elimination_auto_complete(self):
        """Grand final of DE triggers completion."""
        t = self._create_tournament(fmt="double_elimination", name="DEComp")
        for i in range(1, 5):
            self._create_team(t, f"DE{i}", seed=i)
        generate_fixtures(t)

        # Confirm matches round-by-round; advance_winner fills team slots between rounds
        for _attempt in range(20):
            confirmable = list(
                t.matches.filter(team1__isnull=False, team2__isnull=False)
                .exclude(status__in=["confirmed", "cancelled", "bye", "forfeited"])
                .order_by("round_number", "match_number")
            )
            if not confirmable:
                break
            for m in confirmable:
                self._confirm_match(m, 3, 1)

        _check_and_finalize_tournament(t)
        t.refresh_from_db()
        self.assertEqual(t.status, "completed")
        self.assertIsNotNone(t.champion)

    # ---- Consolation ----

    def test_consolation_auto_complete(self):
        """Winners bracket final of consolation tournament triggers completion."""
        t = self._create_tournament(fmt="consolation", name="ConsolComp")
        for i in range(1, 5):
            self._create_team(t, f"Con{i}", seed=i)
        generate_fixtures(t)

        # Round 1 — sets up consolation bracket and advances to semi/final
        r1_matches = list(t.matches.filter(bracket_type="winners", round_number=1))
        for m in r1_matches:
            self._confirm_match(m, 3, 1)

        generate_consolation_if_ready(t)

        # Winners bracket final
        final = (
            t.matches.filter(bracket_type="winners", next_match__isnull=True,
                             team1__isnull=False, team2__isnull=False)
            .order_by("-round_number")
            .first()
        )
        if final:
            final.refresh_from_db()
            self._confirm_match(final, 3, 1)
            _check_and_finalize_tournament(t)
            t.refresh_from_db()
            self.assertEqual(t.status, "completed")

    # ---- Hybrid ----

    def test_hybrid_auto_complete(self):
        """Hybrid: group stage → KO auto-generated → KO final confirmed → completed."""
        t = self._create_tournament(fmt="hybrid", name="HybridComp")
        t.num_groups = 2
        t.teams_per_group_advance = 1
        t.save(update_fields=["num_groups", "teams_per_group_advance"])

        court = Court.objects.create(tournament=t, name="C1")
        TimeSlot.objects.create(
            tournament=t,
            court=court,
            start_time=timezone.now() + timedelta(days=1),
            end_time=timezone.now() + timedelta(days=1, hours=2),
        )

        for i in range(1, 5):
            team = self._create_team(t, f"Hyb{i}", seed=i)
            # Assign groups
            participation = _participation(team, t)
            participation.group = "A" if i <= 2 else "B"
            participation.save(update_fields=["group"])

        generate_fixtures(t)
        self.assertTrue(
            t.matches.filter(group="", bracket_type="winners", team1__isnull=True, team2__isnull=True).exists()
        )

        from ..standings import check_group_stage_complete

        group_matches = list(t.matches.filter(group__gt="").order_by("match_number"))
        for m in group_matches:
            self._confirm_match(m, 3, 1)

        check_group_stage_complete(t)
        _check_and_finalize_tournament(t)
        t.refresh_from_db()
        self.assertEqual(t.status, "active")

        # KO matches generated; confirm the final
        ko_final = (
            t.matches.filter(bracket_type="winners", group="", next_match__isnull=True,
                             team1__isnull=False, team2__isnull=False)
            .order_by("-round_number")
            .first()
        )
        if ko_final:
            ko_final.refresh_from_db()
            self._confirm_match(ko_final, 3, 1)
            _check_and_finalize_tournament(t)
            t.refresh_from_db()
            self.assertEqual(t.status, "completed")
            self.assertIsNotNone(t.champion)

    # ---- Manual Override ----

    def test_mark_tournament_complete_view(self):
        """POST to complete_tournament marks tournament completed."""
        t = self._create_tournament(name="ManualComplete")
        self.client.force_login(self.organizer)
        response = self.client.post(
            reverse("complete_tournament", kwargs={"pk": t.pk}),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        t.refresh_from_db()
        self.assertEqual(t.status, "completed")
        self.assertIsNotNone(t.completed_at)

    def test_mark_tournament_complete_view_requires_active(self):
        """Attempting to complete a non-active tournament shows an error."""
        t = self._create_tournament(name="NotActiveComplete")
        t.status = "setup"
        t.save(update_fields=["status"])
        self.client.force_login(self.organizer)
        response = self.client.post(
            reverse("complete_tournament", kwargs={"pk": t.pk}),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        t.refresh_from_db()
        self.assertEqual(t.status, "setup")

    def test_mark_tournament_complete_view_requires_organizer(self):
        """Non-organizer cannot access complete_tournament view."""
        t = self._create_tournament(name="NonOrgComplete")
        user = User.objects.create_user(username="plain_user_comp", password="pass123")
        self.client.force_login(user)
        response = self.client.post(
            reverse("complete_tournament", kwargs={"pk": t.pk}),
        )
        # Should redirect to dashboard (not organizer)
        self.assertEqual(response.status_code, 302)
        t.refresh_from_db()
        self.assertEqual(t.status, "active")

    # ---- Score submit blocked ----

    def test_score_submit_blocked_when_completed(self):
        """Submitting a score on a completed tournament is rejected."""
        t = self._create_tournament(name="BlockedScore")
        teams = [self._create_team(t, f"Blk{i}") for i in range(1, 3)]
        generate_fixtures(t)
        t.status = "completed"
        t.save(update_fields=["status"])

        match = t.matches.filter(team1__isnull=False, team2__isnull=False).first()
        match.status = "upcoming"
        match.save(update_fields=["status"])

        self.client.force_login(_captain_user(teams[0]))
        response = self.client.post(
            reverse("submit_score", kwargs={"pk": match.pk}),
            {"score_team1": 3, "score_team2": 1},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        match.refresh_from_db()
        self.assertEqual(match.status, "upcoming")  # unchanged

    # ---- Forfeit triggers completion ----

    def test_forfeit_triggers_completion(self):
        """Forfeiting the last match of a RR tournament should complete it."""
        t = self._create_tournament(fmt="round_robin", name="ForfeitComp")
        [self._create_team(t, f"Forf{i}") for i in range(1, 3)]
        generate_fixtures(t)

        matches = list(t.matches.filter(team1__isnull=False, team2__isnull=False).order_by("match_number"))

        # Confirm all but last
        for m in matches[:-1]:
            self._confirm_match(m, 3, 0)

        # Forfeit last
        last = matches[-1]
        last.status = "forfeited"
        last.winner = last.team1
        last.save(update_fields=["status", "winner"])
        _check_and_finalize_tournament(t)

        t.refresh_from_db()
        self.assertEqual(t.status, "completed")

    # ---- _determine_champion logic ----

    def test_determine_champion_rr(self):
        """_determine_champion returns top-ranked team for round robin."""
        t = self._create_tournament(fmt="round_robin", name="ChampRR")
        teams = [self._create_team(t, f"ChR{i}") for i in range(1, 4)]
        generate_fixtures(t)

        # Give team 0 all wins
        winner_team = teams[0]
        for m in t.matches.filter(team1__isnull=False, team2__isnull=False):
            if m.team1 == winner_team:
                self._confirm_match(m, 3, 0)
            elif m.team2 == winner_team:
                self._confirm_match(m, 0, 3)

        champion = _determine_champion(t)
        self.assertEqual(champion, winner_team)

    def test_determine_champion_knockout(self):
        """_determine_champion returns winner of final for knockout."""
        t = self._create_tournament(fmt="knockout", name="ChampKO")
        for i in range(1, 3):
            self._create_team(t, f"ChK{i}", seed=i)
        generate_fixtures(t)

        final = t.matches.filter(team1__isnull=False, team2__isnull=False).first()
        self._confirm_match(final, 3, 1)

        champion = _determine_champion(t)
        self.assertEqual(champion, final.winner)

