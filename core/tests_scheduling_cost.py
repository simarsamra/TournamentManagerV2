"""Guards for the slot-building cost reductions in T-6.1.

Three changes are covered here:

* `_build_slots` accepts `max_slots` and stops once it has that many, so
  `count_available_slots` can answer "at least N?" without materialising a
  year of slots;
* the date walk steps weekday-to-weekday instead of day-by-day, which must
  produce exactly the same slots;
* the open-ended horizon is configurable rather than a hardcoded 365.
"""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Court, CourtAvailability, Tournament
from core.scheduling import _build_slots, count_available_slots


def _next_weekday(start, weekday):
    """First date on or after `start` falling on `weekday`."""
    return start + timedelta(days=(weekday - start.weekday()) % 7)


class SlotBuildingTests(TestCase):
    def setUp(self):
        # No end date on either the tournament or the availability row, so the
        # open-ended horizon decides how far the build runs.
        self.start = timezone.localdate()
        self.tournament = Tournament.objects.create(
            name="Cost", format="round_robin", players_per_team=1,
            start_date=self.start, default_match_duration=60,
        )
        self.court = Court.objects.create(tournament=self.tournament, name="C1")
        self.weekday = _next_weekday(self.start, 0).weekday()
        CourtAvailability.objects.create(
            court=self.court, weekday=self.weekday,
            start_time="09:00", end_time="12:00", is_active=True,
        )
        self.courts = [self.court]

    def test_slots_land_only_on_the_configured_weekday(self):
        """The weekday-stepping walk must not drift onto other days."""
        slots = _build_slots(self.tournament, self.courts)
        self.assertTrue(slots)
        self.assertEqual(
            {timezone.localtime(s[0]).date().weekday() for s in slots},
            {self.weekday},
        )

    def test_first_slot_is_the_first_matching_weekday_on_or_after_the_start(self):
        slots = _build_slots(self.tournament, self.courts)
        self.assertEqual(
            timezone.localtime(slots[0][0]).date(),
            _next_weekday(self.start, self.weekday),
        )

    def test_daily_slots_honour_matches_per_court_per_day(self):
        """A 3-hour window at 60 minutes fits three matches, but the row's
        per-day cap (default 1) is what decides."""
        slots = _build_slots(self.tournament, self.courts)
        first_day = timezone.localtime(slots[0][0]).date()
        same_day = [s for s in slots if timezone.localtime(s[0]).date() == first_day]
        self.assertEqual(len(same_day), 1)

        CourtAvailability.objects.all().update(matches_per_court_per_day=3)
        slots = _build_slots(self.tournament, self.courts)
        first_day = timezone.localtime(slots[0][0]).date()
        same_day = [s for s in slots if timezone.localtime(s[0]).date() == first_day]
        self.assertEqual(len(same_day), 3)

    def test_max_slots_stops_the_build(self):
        self.assertEqual(len(_build_slots(self.tournament, self.courts, max_slots=5)), 5)

    def test_max_slots_never_exceeds_what_is_actually_available(self):
        """A limit above the true total returns the true total, not the limit."""
        CourtAvailability.objects.all().update(end_date=self.start + timedelta(days=7))
        true_total = len(_build_slots(self.tournament, self.courts))
        self.assertEqual(
            len(_build_slots(self.tournament, self.courts, max_slots=true_total + 50)),
            true_total,
        )

    def test_count_caps_at_the_limit(self):
        self.assertEqual(count_available_slots(self.tournament, limit=4), 4)

    def test_count_below_the_limit_is_exact(self):
        """The readiness check prints this number, so a short count must be real."""
        CourtAvailability.objects.all().update(end_date=self.start + timedelta(days=7))
        exact = count_available_slots(self.tournament)
        self.assertEqual(count_available_slots(self.tournament, limit=exact + 100), exact)

    def test_count_without_a_limit_is_unchanged(self):
        CourtAvailability.objects.all().update(end_date=self.start + timedelta(days=21))
        self.assertEqual(
            count_available_slots(self.tournament),
            len(_build_slots(self.tournament, self.courts)),
        )

    @override_settings(OPEN_AVAILABILITY_HORIZON_DAYS=14)
    def test_horizon_setting_bounds_an_open_ended_row(self):
        slots = _build_slots(self.tournament, self.courts)
        last = timezone.localtime(slots[-1][0]).date()
        self.assertLessEqual(last, self.start + timedelta(days=14))

    @override_settings(OPEN_AVAILABILITY_HORIZON_DAYS=14)
    def test_a_shorter_horizon_yields_fewer_slots(self):
        short = len(_build_slots(self.tournament, self.courts))
        with override_settings(OPEN_AVAILABILITY_HORIZON_DAYS=365):
            long = len(_build_slots(self.tournament, self.courts))
        self.assertLess(short, long)


class AdditionalStartTimesTests(TestCase):
    """The extra start times are now parsed once per row rather than per day;
    the resulting slots must be identical."""

    def setUp(self):
        self.start = timezone.localdate()
        self.tournament = Tournament.objects.create(
            name="Extra", format="round_robin", players_per_team=1,
            start_date=self.start, end_date=self.start + timedelta(days=21),
            default_match_duration=60,
        )
        self.court = Court.objects.create(tournament=self.tournament, name="C1")
        self.weekday = _next_weekday(self.start, 2).weekday()

    def test_explicit_times_produce_one_slot_each_per_matching_day(self):
        CourtAvailability.objects.create(
            court=self.court, weekday=self.weekday,
            start_time="09:00", end_time="10:00",
            additional_start_times="13:00,15:30", is_active=True,
        )
        slots = _build_slots(self.tournament, [self.court])
        first_day = timezone.localtime(slots[0][0]).date()
        times = sorted(
            timezone.localtime(s[0]).time().strftime("%H:%M")
            for s in slots if timezone.localtime(s[0]).date() == first_day
        )
        self.assertEqual(times, ["09:00", "13:00", "15:30"])

    def test_unparseable_extra_times_are_skipped_not_fatal(self):
        CourtAvailability.objects.create(
            court=self.court, weekday=self.weekday,
            start_time="09:00", end_time="10:00",
            additional_start_times="13:00, not-a-time, ,15:30", is_active=True,
        )
        slots = _build_slots(self.tournament, [self.court])
        first_day = timezone.localtime(slots[0][0]).date()
        times = sorted(
            timezone.localtime(s[0]).time().strftime("%H:%M")
            for s in slots if timezone.localtime(s[0]).date() == first_day
        )
        self.assertEqual(times, ["09:00", "13:00", "15:30"])

    def test_max_slots_applies_to_explicit_times_too(self):
        CourtAvailability.objects.create(
            court=self.court, weekday=self.weekday,
            start_time="09:00", end_time="10:00",
            additional_start_times="13:00,15:30", is_active=True,
        )
        self.assertEqual(
            len(_build_slots(self.tournament, [self.court], max_slots=2)), 2
        )
