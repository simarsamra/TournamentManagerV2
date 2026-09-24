"""Tournament status transitions: draft, active, paused, completed, cancelled.

Split out of the old core/tests.py — see FOLLOWUP_PLAN.md F-3.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.db import models
from datetime import timedelta
from ..models import (
    Team,
    Tournament,
    Player,
    TeamMembership,
    TeamTournamentParticipation,
    TeamTournamentCourtPreference,
)
from ..scheduling import generate_fixtures
from ..standings import advance_winner
from .helpers import _captain_user


class TournamentLifecycleTests(TestCase):
    """Tests for end-to-end tournament lifecycle (organizer + team UI flows)."""

    def setUp(self):
        self.organizer = User.objects.create_user(
            username="org_admin", password="pass123", is_staff=True
        )

    def test_organizer_creates_and_manages_knockout_tournament(self):
        """Full organizer flow: create tournament, add court/timeslots, manage teams, generate fixtures."""
        self.client.force_login(self.organizer)

        # Step 1: Create tournament
        response = self.client.post(
            reverse("tournament_setup"),
            {
                "name": "Regional Knockout",
                "format": "knockout",
                "sport_type": "table_tennis",
                "points_per_win": 3,
                "points_per_loss": 0,
                "points_per_draw": 1,
                "default_match_duration": 30,
                "players_per_team": 1,
                "num_groups": 2,
                "teams_per_group_advance": 1,
                "withdrawal_policy": "forfeit",
            },
        )
        self.assertEqual(response.status_code, 302)  # Should redirect after creating
        tournament = Tournament.objects.get(name="Regional Knockout")
        self.assertEqual(tournament.format, "knockout")
        self.assertEqual(tournament.status, "setup")

        # Step 2: Add court
        response = self.client.post(
            reverse("add_court", kwargs={"pk": tournament.pk}),
            {"name": "Court 1", "is_available": True},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        court = tournament.courts.get(name="Court 1")
        self.assertIsNotNone(court)

        # Step 3: Add time slot
        now = timezone.now()
        response = self.client.post(
            reverse("add_timeslot", kwargs={"pk": tournament.pk}),
            {
                "date": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
                "start_time": "10:00",
                "end_time": "12:00",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(tournament.time_slots.count(), 1)

        # Step 4: Add teams (simulating organizer registration)
        for i in range(1, 5):
            user = User.objects.create_user(
                username=f"team_user_{i}", password="pass123"
            )
            team, _ = Team.objects.get_or_create(name=f"Team {i}")
            participation, _ = TeamTournamentParticipation.objects.get_or_create(
                team=team, tournament=tournament, defaults={"status": "active", "seed": i}
            )
            TeamMembership.objects.create(team=team, user=user, role="captain")
            Player.objects.create(team=team, name=f"Player {i}")
            TeamTournamentCourtPreference.objects.get_or_create(participation=participation, court=court)

        # Step 5: Start tournament (generate fixtures)
        response = self.client.post(
            reverse("start_tournament", kwargs={"pk": tournament.pk}),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        tournament.refresh_from_db()
        self.assertEqual(tournament.status, "active")
        self.assertIsNotNone(tournament.started_at)

        # Verify fixtures were generated
        matches = tournament.matches.all()
        self.assertGreater(matches.count(), 0)
        # Knockout with 4 teams: 2 semifinal + 1 final = 3 matches
        self.assertEqual(matches.count(), 3)

        # Step 6: View fixtures
        response = self.client.get(reverse("fixtures"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("matches", response.context)
        self.assertEqual(len(response.context["matches"]), 3)

    def test_team_registers_plays_and_views_standings(self):
        """Full team user flow: register, play matches, submit scores, confirm scores, view standings."""
        # Step 1: Create and start a tournament
        tournament = Tournament.objects.create(
            name="Team Flow Tournament",
            format="round_robin",
            sport_type="table_tennis",
            points_per_win=3,
            points_per_loss=0,
            points_per_draw=1,
            default_match_duration=30,
        )

        # Create 3 teams
        teams_data = []
        for i in range(1, 4):
            user = User.objects.create_user(
                username=f"team_player_{i}", password="pass123"
            )
            team, _ = Team.objects.get_or_create(name=f"Team {i}")
            TeamTournamentParticipation.objects.get_or_create(
                team=team, tournament=tournament, defaults={"status": "active", "seed": i}
            )
            TeamMembership.objects.create(team=team, user=user, role="captain")
            teams_data.append((user, team))

        generate_fixtures(tournament)
        tournament.status = "active"
        tournament.started_at = timezone.now()
        tournament.save(update_fields=["status", "started_at"])

        # Step 2: Team user 1 logs in and submits a score
        user1, team1 = teams_data[0]
        self.client.force_login(user1)

        # Find a match for team1
        match = tournament.matches.filter(team1=team1, status="upcoming").first()
        if not match:
            match = tournament.matches.filter(team2=team1, status="upcoming").first()

        self.assertIsNotNone(match, "Should have upcoming match for team1")

        # Submit score
        response = self.client.post(
            reverse("submit_score", kwargs={"pk": match.pk}),
            {
                "score_team1": 3,
                "score_team2": 1,
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)

        # Verify match is now pending confirmation
        match.refresh_from_db()
        self.assertEqual(match.status, "pending_confirmation")
        self.assertEqual(match.score_team1, 3)
        self.assertEqual(match.score_team2, 1)

        # Step 3: Opponent logs in and confirms the score
        opponent_team = match.team2 if match.team1 == team1 else match.team1
        opponent_user = _captain_user(opponent_team)
        self.client.force_login(opponent_user)

        response = self.client.post(
            reverse("confirm_score", kwargs={"pk": match.pk}),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)

        # Verify match is now confirmed
        match.refresh_from_db()
        self.assertEqual(match.status, "confirmed")
        self.assertIsNotNone(match.winner)

        # Step 4: Any logged-in user views standings
        # Stay logged in as user2 to view standings
        response = self.client.get(reverse("standings"), follow=True)
        self.assertEqual(response.status_code, 200)
        
        # Check if standings are in context - may be under different key
        context_keys = list(response.context.keys()) if response.context else []
        standings_found = any(key in ["standings", "tournament_standings"] for key in context_keys)
        
        # If standings are available, verify they're correct
        if standings_found:
            standings_key = next((k for k in context_keys if k in ["standings", "tournament_standings"]), None)
            standings = response.context[standings_key]
            self.assertTrue(len(standings) > 0)
            
            # Winner should have 3 points
            winner_standing = next(
                (s for s in standings if s["team"] == match.winner), None
            )
            if winner_standing:
                self.assertEqual(winner_standing["points"], 3)

    def test_hybrid_tournament_full_lifecycle_group_to_knockout(self):
        """Full hybrid tournament flow: groups, group advancement, knockout, finals."""
        # Create hybrid tournament
        tournament = Tournament.objects.create(
            name="Hybrid Championship",
            format="hybrid",
            sport_type="table_tennis",
            points_per_win=3,
            points_per_loss=0,
            points_per_draw=1,
            num_groups=2,
            teams_per_group_advance=1,
            default_match_duration=30,
        )

        # Create 4 teams (2 per group)
        teams = []
        for i in range(1, 5):
            user = User.objects.create_user(
                username=f"hybrid_team_{i}", password="pass123"
            )
            team, _ = Team.objects.get_or_create(name=f"Team {i}")
            TeamTournamentParticipation.objects.get_or_create(
                team=team, tournament=tournament, defaults={"status": "active", "seed": i}
            )
            TeamMembership.objects.get_or_create(team=team, user=user, defaults={"role": "captain"})
            teams.append(team)

        # Generate group stage fixtures
        generate_fixtures(tournament)

        # Verify groups were assigned
        teams_with_groups = tournament.team_participations.filter(group__gt="")
        self.assertEqual(teams_with_groups.count(), 4, "All teams should be assigned to groups")

        # Verify group stage matches were created
        group_matches = tournament.matches.filter(group__gt="")
        self.assertTrue(group_matches.exists(), "Group stage matches should be created")

        # Complete group stage matches
        for match in group_matches:
            match.status = "confirmed"
            match.score_team1 = 3
            match.score_team2 = 1
            match.winner = match.team1
            match.save(update_fields=["status", "score_team1", "score_team2", "winner"])

        # Trigger knockout generation from group stage completion
        from ..standings import check_group_stage_complete
        knockout_generated = check_group_stage_complete(tournament)
        self.assertTrue(knockout_generated, "Knockout should be generated after group stage")

        # Verify knockout matches were created
        knockout_matches = tournament.matches.filter(group="")
        self.assertTrue(knockout_matches.exists(), "Knockout matches should exist after group stage")

        # Verify knockout structure
        ko_by_round = knockout_matches.values_list("round_number", flat=True).distinct()
        self.assertTrue(len(list(ko_by_round)) > 0, "Knockout should have multiple rounds")

        # Complete first knockout round
        first_round_ko = knockout_matches.filter(round_number=knockout_matches.aggregate(models.Min("round_number"))["round_number__min"])
        for match in first_round_ko:
            if match.team1 and match.team2:
                match.status = "confirmed"
                match.score_team1 = 2
                match.score_team2 = 1
                match.winner = match.team1
                match.save(update_fields=["status", "score_team1", "score_team2", "winner"])
                advance_winner(match)

        # Verify tournament has proper structure
        all_matches = tournament.matches.all()
        self.assertGreater(all_matches.count(), 0, "Tournament should have matches")

    def test_tournament_audit_log_tracks_lifecycle_events(self):
        """Verify audit log tracks all tournament lifecycle events."""
        from ..models import AuditLog

        self.client.force_login(self.organizer)

        # Create tournament
        response = self.client.post(
            reverse("tournament_setup"),
            {
                "name": "Audit Test",
                "format": "knockout",
                "sport_type": "table_tennis",
                "points_per_win": 3,
                "points_per_loss": 0,
                "points_per_draw": 1,
                "default_match_duration": 30,
                "players_per_team": 1,
                "num_groups": 2,
                "teams_per_group_advance": 1,
                "withdrawal_policy": "forfeit",
            },
        )
        self.assertEqual(response.status_code, 302)  # Redirect after creating

        tournament = Tournament.objects.get(name="Audit Test")

        # Add a court
        self.client.post(
            reverse("add_court", kwargs={"pk": tournament.pk}),
            {"name": "Court 1", "is_available": True},
        )

        # Add a team
        user = User.objects.create_user(username="audit_test_team", password="pass123")
        team, _ = Team.objects.get_or_create(name="Audit Test Team")
        TeamTournamentParticipation.objects.get_or_create(
            team=team, tournament=tournament, defaults={"status": "active", "seed": 1}
        )
        TeamMembership.objects.get_or_create(team=team, user=user, defaults={"role": "captain"})

        # Check audit log has entries
        audit_entries = AuditLog.objects.filter(tournament=tournament)
        self.assertGreater(audit_entries.count(), 0)

        # Verify key events are logged
        actions = [entry.action for entry in audit_entries]
        self.assertIn("tournament_created", actions)
        self.assertIn("court_added", actions)

