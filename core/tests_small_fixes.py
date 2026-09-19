"""Guards for the small correctness fixes batched as T-4.9 / T-4.10."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from core.models import (
    Court, CourtAvailability, Match, OrganizerProfile, Team, TeamMembership,
    TeamTournamentParticipation, Tournament,
)
from core.views import _get_user_tournament_ids


def make_organizer(username="org"):
    user = User.objects.create_user(username=username, password="Regression-Pass-1")
    OrganizerProfile.objects.filter(user=user).update(verified=True)
    return user


class AvailabilityDateWarningTests(TestCase):
    """_build_slots clamps to max(tournament.start_date, row.start_date), so a
    window ending before the start date yields nothing — silently."""

    def test_config_warns_about_availability_that_ends_before_the_start(self):
        organizer = make_organizer()
        tournament = Tournament.objects.create(
            name="W", format="round_robin", players_per_team=1,
            start_date=timezone.localdate(), created_by=organizer,
        )
        court = Court.objects.create(tournament=tournament, name="C1")
        CourtAvailability.objects.create(
            court=court, weekday=0, start_time="09:00", end_time="10:00",
            start_date=timezone.localdate() - timedelta(days=30),
            end_date=timezone.localdate() - timedelta(days=1),
            is_active=True,
        )

        self.client.force_login(organizer)
        response = self.client.get(f"/tournament/{tournament.pk}/config/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "before the tournament start date",
            response.context["availability_date_warning"],
        )


class UserTournamentIdsTests(TestCase):
    def test_team_without_participations_does_not_inject_none(self):
        user = User.objects.create_user(username="u", password="Regression-Pass-1")
        orphan = Team.objects.create(name="Orphan")
        TeamMembership.objects.create(team=orphan, user=user, role="captain")

        joined = Team.objects.create(name="Joined")
        tournament = Tournament.objects.create(
            name="T", format="round_robin", players_per_team=1
        )
        TeamTournamentParticipation.objects.create(
            team=joined, tournament=tournament, status="active"
        )
        TeamMembership.objects.create(team=joined, user=user, role="member")

        ids = _get_user_tournament_ids(user)
        self.assertNotIn(None, ids)
        self.assertEqual(ids, [tournament.pk])


class DuplicateTournamentTests(TestCase):
    def test_all_configured_fields_are_copied(self):
        organizer = make_organizer("dup")
        source = Tournament.objects.create(
            name="Source", format="knockout", players_per_team=2,
            matches_per_court_per_day=5, enable_third_place_match=True,
            created_by=organizer,
        )
        self.client.force_login(organizer)
        self.client.post(f"/tournament/{source.pk}/duplicate/")

        copy = Tournament.objects.get(name="Copy of Source")
        self.assertEqual(copy.matches_per_court_per_day, 5)
        self.assertTrue(copy.enable_third_place_match)
        self.assertEqual(copy.created_by, organizer)


class ResolveDisputeTests(TestCase):
    def test_missing_scores_reports_an_error_instead_of_silently_doing_nothing(self):
        organizer = make_organizer("disp")
        tournament = Tournament.objects.create(
            name="D", format="round_robin", status="active",
            players_per_team=1, created_by=organizer,
        )
        teams = []
        for i in range(2):
            team = Team.objects.create(name=f"D{i}")
            TeamTournamentParticipation.objects.create(
                team=team, tournament=tournament, status="active"
            )
            teams.append(team)
        match = Match.objects.create(
            tournament=tournament, match_number=1,
            team1=teams[0], team2=teams[1], status="disputed",
        )

        self.client.force_login(organizer)
        response = self.client.post(
            f"/match/{match.pk}/resolve-dispute/", {"resolution_notes": "x"},
            follow=True,
        )

        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(
            any("final score" in m.lower() for m in messages),
            f"expected an explicit error, got: {messages}",
        )
        match.refresh_from_db()
        self.assertEqual(match.status, "disputed")


class PublicProfileRecordTests(TestCase):
    def test_wins_before_joining_are_not_credited(self):
        team = Team.objects.create(name="Legacy")
        tournament = Tournament.objects.create(
            name="P", format="round_robin", players_per_team=1
        )
        TeamTournamentParticipation.objects.create(
            team=team, tournament=tournament, status="active"
        )
        rival = Team.objects.create(name="Rival")
        TeamTournamentParticipation.objects.create(
            team=rival, tournament=tournament, status="active"
        )

        # A win that happened before the newcomer joined.
        Match.objects.create(
            tournament=tournament, match_number=1, team1=team, team2=rival,
            status="confirmed", winner=team,
            scheduled_time=timezone.now() - timedelta(days=10),
        )

        newcomer = User.objects.create_user(
            username="newcomer", password="Regression-Pass-1"
        )
        TeamMembership.objects.create(team=team, user=newcomer, role="member")

        response = self.client.get(f"/users/{newcomer.username}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["wins"], 0,
            "a player must not inherit wins from before they joined",
        )


class TimeSlotFormErrorTests(TestCase):
    def test_invalid_timeslot_surfaces_an_error(self):
        organizer = make_organizer("ts")
        tournament = Tournament.objects.create(
            name="TS", format="round_robin", players_per_team=1,
            created_by=organizer,
        )
        self.client.force_login(organizer)
        response = self.client.post(
            f"/tournament/{tournament.pk}/add-timeslot/",
            {"date": "not-a-date", "start_time": "09:00", "end_time": "10:00"},
            follow=True,
        )
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(messages, "an invalid time slot must not fail silently")
