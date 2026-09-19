"""Guards for reschedule request handling."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from core.models import (
    Court, Match, RescheduleRequest, Team, TeamMembership,
    TeamTournamentParticipation, Tournament,
)


class RespondRescheduleTests(TestCase):
    def setUp(self):
        self.tournament = Tournament.objects.create(
            name="RS", format="round_robin", status="active",
            players_per_team=1, default_match_duration=30,
        )
        self.court = Court.objects.create(tournament=self.tournament, name="C1")
        self.teams, self.users = [], []
        for i in range(2):
            team = Team.objects.create(name=f"T{i}")
            TeamTournamentParticipation.objects.create(
                team=team, tournament=self.tournament, status="active"
            )
            user = User.objects.create_user(
                username=f"u{i}", password="Regression-Pass-1"
            )
            TeamMembership.objects.create(team=team, user=user, role="captain")
            self.teams.append(team)
            self.users.append(user)

        self.start = timezone.now() + timedelta(days=1)
        self.match = Match.objects.create(
            tournament=self.tournament, match_number=1,
            team1=self.teams[0], team2=self.teams[1], court=self.court,
            scheduled_time=self.start,
            scheduled_end_time=self.start + timedelta(minutes=30),
            status="upcoming",
        )

    def test_an_answered_request_cannot_be_answered_again(self):
        request_row = RescheduleRequest.objects.create(
            match=self.match, requested_by=self.users[0],
            new_time=self.start + timedelta(days=1), new_court=self.court,
        )
        self.client.force_login(self.users[1])

        self.client.post(f"/reschedule/{request_row.pk}/respond/", {"action": "approve"})
        request_row.refresh_from_db()
        self.match.refresh_from_db()
        self.assertEqual(request_row.status, "approved")
        applied_time = self.match.scheduled_time

        # A second response must not flip the status behind an applied reschedule.
        self.client.post(f"/reschedule/{request_row.pk}/respond/", {"action": "reject"})
        request_row.refresh_from_db()
        self.match.refresh_from_db()
        self.assertEqual(request_row.status, "approved")
        self.assertEqual(self.match.scheduled_time, applied_time)

    def test_two_requests_cannot_both_claim_the_same_slot(self):
        """Conflicts were only checked at request time, so two pending requests
        targeting one free slot could both be approved."""
        other_team = Team.objects.create(name="T2")
        TeamTournamentParticipation.objects.create(
            team=other_team, tournament=self.tournament, status="active"
        )
        third = Team.objects.create(name="T3")
        TeamTournamentParticipation.objects.create(
            team=third, tournament=self.tournament, status="active"
        )
        other_user = User.objects.create_user(
            username="u2", password="Regression-Pass-1"
        )
        rival_user = User.objects.create_user(
            username="u3", password="Regression-Pass-1"
        )
        TeamMembership.objects.create(team=other_team, user=other_user, role="captain")
        TeamMembership.objects.create(team=third, user=rival_user, role="captain")

        second_match = Match.objects.create(
            tournament=self.tournament, match_number=2,
            team1=other_team, team2=third, court=self.court,
            scheduled_time=self.start + timedelta(days=2),
            scheduled_end_time=self.start + timedelta(days=2, minutes=30),
            status="upcoming",
        )

        target = self.start + timedelta(days=5)
        first = RescheduleRequest.objects.create(
            match=self.match, requested_by=self.users[0],
            new_time=target, new_court=self.court,
        )
        second = RescheduleRequest.objects.create(
            match=second_match, requested_by=other_user,
            new_time=target, new_court=self.court,
        )

        self.client.force_login(self.users[1])
        self.client.post(f"/reschedule/{first.pk}/respond/", {"action": "approve"})
        self.client.logout()

        self.client.force_login(rival_user)
        self.client.post(f"/reschedule/{second.pk}/respond/", {"action": "approve"})

        first.refresh_from_db()
        second.refresh_from_db()
        second_match.refresh_from_db()

        self.assertEqual(first.status, "approved")
        self.assertEqual(
            second.status, "cancelled",
            "the second request must not be approved onto an occupied court",
        )
        self.assertNotEqual(second_match.scheduled_time, target)
