"""Registration capacity under concurrent requests.

Registration was a check-then-act: count the active participants, then create
one, with nothing holding the two together. SQLite masked this by accident --
it locks the whole table, so the losing request died with "database table is
locked" rather than overfilling the tournament. PostgreSQL commits both, so a
one-slot tournament quietly ends up with two participants.

Both behaviours were reproduced before the fix was written. That makes this a
migration hazard rather than a pre-existing bug made visible: moving to
PostgreSQL removes SQLite's accidental protection.
"""
import threading

from django.db import connections
from django.test import TransactionTestCase, tag
from django.utils import timezone

from core.models import Team, TeamTournamentParticipation, Tournament
from core.services.enrollment import active_participant_count
from core.views.helpers import _claim_participant_slot


def on_postgres():
    return "postgresql" in connections["default"].settings_dict["ENGINE"]


class ClaimParticipantSlotTests(TransactionTestCase):
    """Single-threaded behaviour, which must hold on either backend."""

    def setUp(self):
        self.tournament = Tournament.objects.create(
            name="Claim", format="round_robin", players_per_team=1,
            start_date=timezone.localdate(), status="registration_open",
            expected_teams_count=2,
        )

    def _register(self, name):
        team = Team.objects.create(name=name)
        TeamTournamentParticipation.objects.create(
            team=team, tournament=self.tournament, status="active"
        )

    def test_a_free_slot_is_claimable(self):
        from django.db import transaction
        with transaction.atomic():
            self.assertTrue(_claim_participant_slot(self.tournament))

    def test_a_full_tournament_is_not_claimable(self):
        from django.db import transaction
        self._register("C1")
        self._register("C2")
        with transaction.atomic():
            self.assertFalse(_claim_participant_slot(self.tournament))

    def test_uncapped_tournaments_are_always_claimable(self):
        from django.db import transaction
        # 0 is the uncapped sentinel; the column is NOT NULL, which PostgreSQL
        # enforces even where SQLite let a null through.
        self.tournament.expected_teams_count = 0
        self.tournament.save(update_fields=["expected_teams_count"])
        for i in range(5):
            self._register(f"U{i}")
        with transaction.atomic():
            self.assertTrue(_claim_participant_slot(self.tournament))

    def test_waitlisted_entries_do_not_consume_a_slot(self):
        from django.db import transaction
        team = Team.objects.create(name="W1")
        TeamTournamentParticipation.objects.create(
            team=team, tournament=self.tournament, status="waitlisted"
        )
        with transaction.atomic():
            self.assertTrue(_claim_participant_slot(self.tournament))


@tag("concurrency")
class ConcurrentRegistrationTests(TransactionTestCase):
    """Two simultaneous registrations for the last free slot.

    Skipped on SQLite: select_for_update is a no-op there, and SQLite's
    table-level locking produces a different (also acceptable) outcome -- one
    request raises OperationalError instead of being serialised.
    """

    def setUp(self):
        if not on_postgres():
            self.skipTest("row locking is a no-op on SQLite")
        self.tournament = Tournament.objects.create(
            name="Concurrent", format="round_robin", players_per_team=1,
            start_date=timezone.localdate(), status="registration_open",
            expected_teams_count=1,
        )

    def _race(self, worker, count=2):
        barrier = threading.Barrier(count, timeout=10)
        outcomes = []
        lock = threading.Lock()

        def run(index):
            try:
                barrier.wait()
                result = worker(index)
                with lock:
                    outcomes.append(result)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive(), "a registration thread deadlocked")
        return outcomes

    def test_only_one_of_two_concurrent_registrations_takes_the_last_slot(self):
        from django.db import transaction

        def worker(index):
            with transaction.atomic():
                if not _claim_participant_slot(self.tournament):
                    return False
                team = Team.objects.create(name=f"Race{index}")
                TeamTournamentParticipation.objects.create(
                    team=team, tournament=self.tournament, status="active"
                )
                return True

        outcomes = self._race(worker)

        self.assertEqual(sorted(outcomes), [False, True], f"outcomes: {outcomes}")
        self.assertEqual(active_participant_count(self.tournament), 1)

    def test_the_unguarded_pattern_really_does_overfill(self):
        """Pins the bug itself. Without the lock both requests commit, which is
        why the lock is not optional on PostgreSQL."""
        def worker(index):
            full = (
                active_participant_count(self.tournament)
                >= self.tournament.expected_teams_count
            )
            if full:
                return False
            team = Team.objects.create(name=f"Unguarded{index}")
            TeamTournamentParticipation.objects.create(
                team=team, tournament=self.tournament, status="active"
            )
            return True

        self._race(worker)
        self.assertEqual(
            active_participant_count(self.tournament), 2,
            "expected the unguarded check-then-act to overfill; if this now "
            "reports 1, the race closed some other way and the guard above "
            "should be re-examined",
        )

    def test_ten_concurrent_registrations_fill_exactly_the_capacity(self):
        from django.db import transaction

        self.tournament.expected_teams_count = 3
        self.tournament.save(update_fields=["expected_teams_count"])

        def worker(index):
            with transaction.atomic():
                if not _claim_participant_slot(self.tournament):
                    return False
                team = Team.objects.create(name=f"Many{index}")
                TeamTournamentParticipation.objects.create(
                    team=team, tournament=self.tournament, status="active"
                )
                return True

        outcomes = self._race(worker, count=10)

        self.assertEqual(sum(1 for o in outcomes if o), 3)
        self.assertEqual(active_participant_count(self.tournament), 3)
