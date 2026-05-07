"""Scheduling engine for generating tournament fixtures."""
import math
import itertools
from collections import defaultdict
from datetime import datetime, timedelta, time
from django.db import models
from django.db.models import F
from django.utils import timezone
from .models import Match, Court, TimeSlot, Team, CourtAvailability, TeamTournamentCourtPreference


REST_DAYS_BEFORE_SEMIFINAL = 1
REST_DAYS_BEFORE_FINAL = 1
MAX_ESTIMATE_LOOKAHEAD_DAYS = 365 * 3


def _active_teams(tournament):
    """Return active teams for a tournament via participation, annotated with seed/group."""
    return list(
        Team.objects.filter(
            participations__tournament=tournament,
            participations__status="active",
        )
        .annotate(
            seed=F("participations__seed"),
            group=F("participations__group"),
        )
        .order_by("participations__seed", "id")
        .distinct()
    )


def generate_round_robin(tournament):
    """Generate all-play-all fixtures."""
    teams = _active_teams(tournament)
    n = len(teams)
    if n < 2:
        return

    matches_data = []
    if n % 2 == 1:
        teams.append(None)  # bye placeholder
        n += 1

    # Standard round-robin circle method
    fixed = teams[0]
    rotating = teams[1:]
    match_num = 1

    for round_idx in range(n - 1):
        round_pairs = []
        round_pairs.append((fixed, rotating[0]))
        for i in range(1, n // 2):
            round_pairs.append((rotating[i], rotating[n - 1 - i]))
        rotating = [rotating[-1]] + rotating[:-1]

        for t1, t2 in round_pairs:
            if t1 is None or t2 is None:
                continue
            matches_data.append({
                "team1": t1,
                "team2": t2,
                "round_number": round_idx + 1,
                "match_number": match_num,
            })
            match_num += 1

    _assign_schedule(tournament, matches_data)


def generate_double_round_robin(tournament):
    """Generate home-and-away round-robin fixtures."""
    teams = _active_teams(tournament)
    n = len(teams)
    if n < 2:
        return

    first_leg = []
    if n % 2 == 1:
        teams.append(None)  # bye placeholder
        n += 1

    fixed = teams[0]
    rotating = teams[1:]
    match_num = 1

    for round_idx in range(n - 1):
        round_pairs = [(fixed, rotating[0])]
        for i in range(1, n // 2):
            round_pairs.append((rotating[i], rotating[n - 1 - i]))
        rotating = [rotating[-1]] + rotating[:-1]

        for t1, t2 in round_pairs:
            if t1 is None or t2 is None:
                continue
            first_leg.append((t1, t2, round_idx + 1))

    matches_data = []
    for t1, t2, round_no in first_leg:
        matches_data.append({
            "team1": t1,
            "team2": t2,
            "round_number": round_no,
            "match_number": match_num,
        })
        match_num += 1

    round_offset = n - 1
    for t1, t2, round_no in first_leg:
        matches_data.append({
            "team1": t2,
            "team2": t1,
            "round_number": round_no + round_offset,
            "match_number": match_num,
        })
        match_num += 1

    _assign_schedule(tournament, matches_data)


def _bracket_seed_order(size):
    """Return standard bracket seed positions for given bracket size.

    For size=8: [1,8,4,5,3,6,2,7] — ensures top seeds are spread apart
    and byes go to the highest seeds.
    """
    if size == 1:
        return [1]
    half = _bracket_seed_order(size // 2)
    return [item for seed in half for item in (seed, size + 1 - seed)]


def _knockout_round_match_counts(team_count):
    """Return match counts per elimination round for the given team count."""
    if team_count < 2:
        return []

    bracket_size = 1
    while bracket_size < team_count:
        bracket_size *= 2

    round_counts = []
    matches_in_round = bracket_size // 2
    while matches_in_round >= 1:
        round_counts.append(matches_in_round)
        matches_in_round //= 2
    return round_counts


def _hybrid_group_and_advancer_counts(tournament, team_count):
    """Return (group_stage_matches, advancing_slots) for hybrid format."""
    num_groups = max(1, min(tournament.num_groups or 1, team_count))
    base_size = team_count // num_groups
    remainder = team_count % num_groups

    group_matches = 0
    advancers = 0
    per_group_advance = max(1, tournament.teams_per_group_advance or 1)
    for idx in range(num_groups):
        size = base_size + (1 if idx < remainder else 0)
        if size >= 2:
            group_matches += size * (size - 1) // 2
        advancers += min(size, per_group_advance)
    return group_matches, advancers


def _date_capacity_from_slots(slots):
    """Collapse slot list into a date -> number of available slots map."""
    capacity_by_date = defaultdict(int)
    for start_t, _, _ in slots:
        d = timezone.localtime(start_t).date() if timezone.is_aware(start_t) else start_t.date()
        capacity_by_date[d] += 1
    return dict(capacity_by_date)


def _rest_days_before_round(round_num, final_round, semifinal_round,
                            rest_before_semifinal=REST_DAYS_BEFORE_SEMIFINAL,
                            rest_before_final=REST_DAYS_BEFORE_FINAL):
    """Return required calendar-day rest before the given round starts."""
    if round_num == final_round:
        return max(0, rest_before_final)
    if semifinal_round and round_num == semifinal_round:
        return max(0, rest_before_semifinal)
    return 0


def _simulate_rounds_over_capacity(start_date, capacity_by_date, round_match_counts,
                                   rest_before_semifinal=REST_DAYS_BEFORE_SEMIFINAL,
                                   rest_before_final=REST_DAYS_BEFORE_FINAL):
    """Simulate round-constrained completion date from daily capacity map."""
    if not round_match_counts:
        return start_date
    if not capacity_by_date:
        return None

    final_round = len(round_match_counts)
    semifinal_round = final_round - 1 if final_round >= 3 else None
    prev_round_end = None

    for idx, match_count in enumerate(round_match_counts, start=1):
        if match_count <= 0:
            continue

        if prev_round_end is None:
            earliest = start_date
        else:
            rest_days = _rest_days_before_round(
                idx,
                final_round,
                semifinal_round,
                rest_before_semifinal=rest_before_semifinal,
                rest_before_final=rest_before_final,
            )
            earliest = prev_round_end + timedelta(days=1 + rest_days)

        remaining = match_count
        current_date = earliest
        round_end = None

        while remaining > 0:
            if (current_date - start_date).days > MAX_ESTIMATE_LOOKAHEAD_DAYS:
                return None
            capacity = capacity_by_date.get(current_date, 0)
            if capacity > 0:
                remaining -= min(capacity, remaining)
                round_end = current_date
            current_date += timedelta(days=1)

        if round_end is None:
            return None
        prev_round_end = round_end

    return prev_round_end


def estimate_completion_date(tournament, team_count=None, capacity_by_date=None, start_date=None):
    """Estimate completion date with format-aware round dependencies.

    Uses daily slot capacity and enforces elimination rest gaps before
    semi-finals and finals.
    """
    n = team_count if team_count is not None else tournament.team_participations.filter(status="active").count()
    if n < 2:
        return start_date or tournament.start_date or timezone.localdate()

    start = start_date or tournament.start_date or timezone.localdate()
    if capacity_by_date is None:
        courts = list(tournament.courts.filter(is_available=True))
        if not courts:
            return None
        slots = _build_slots(tournament, courts)
        capacity_by_date = _date_capacity_from_slots(slots)

    if not capacity_by_date:
        return None

    fmt = tournament.format
    if fmt in ("knockout", "consolation"):
        return _simulate_rounds_over_capacity(
            start,
            capacity_by_date,
            _knockout_round_match_counts(n),
        )

    if fmt == "hybrid":
        group_matches, advancers = _hybrid_group_and_advancer_counts(tournament, n)
        group_end = start
        if group_matches > 0:
            group_end = _simulate_rounds_over_capacity(
                start,
                capacity_by_date,
                [group_matches],
                rest_before_semifinal=0,
                rest_before_final=0,
            )
            if group_end is None:
                return None

        if advancers < 2:
            return group_end

        knockout_start = group_end + timedelta(days=1)
        return _simulate_rounds_over_capacity(
            knockout_start,
            capacity_by_date,
            _knockout_round_match_counts(advancers),
        )

    required_matches = estimate_required_matches(tournament, team_count=n)
    return _simulate_rounds_over_capacity(
        start,
        capacity_by_date,
        [required_matches],
        rest_before_semifinal=0,
        rest_before_final=0,
    )


def _assign_round_based_schedule(tournament, matches, phase_start_date=None,
                                 rest_before_semifinal=REST_DAYS_BEFORE_SEMIFINAL,
                                 rest_before_final=REST_DAYS_BEFORE_FINAL):
    """Assign schedule to elimination rounds while enforcing round rest gaps."""
    courts = list(tournament.courts.filter(is_available=True))
    if not courts:
        return

    slots = _build_slots(tournament, courts)
    if not slots:
        return

    slots_by_date = defaultdict(list)
    for start_t, end_t, court in slots:
        d = timezone.localtime(start_t).date() if timezone.is_aware(start_t) else start_t.date()
        slots_by_date[d].append((start_t, end_t, court))
    for day in slots_by_date:
        slots_by_date[day].sort(key=lambda item: (item[0], item[2].id))

    rounds = defaultdict(list)
    for match in matches:
        if match.status == "bye":
            continue
        rounds[match.round_number].append(match)
    if not rounds:
        return

    used_slots = set()
    final_round = max(rounds.keys())
    semifinal_round = final_round - 1 if final_round >= 3 else None
    prev_round_last_date = None
    phase_start = phase_start_date or tournament.start_date or timezone.localdate()

    for round_num in sorted(rounds.keys()):
        round_matches = rounds[round_num]
        if prev_round_last_date is None:
            current_date = phase_start
        else:
            rest_days = _rest_days_before_round(
                round_num,
                final_round,
                semifinal_round,
                rest_before_semifinal=rest_before_semifinal,
                rest_before_final=rest_before_final,
            )
            current_date = prev_round_last_date + timedelta(days=1 + rest_days)

        scheduled_dates = []
        for match in round_matches:
            target_date = current_date
            slot = None
            for _ in range(MAX_ESTIMATE_LOOKAHEAD_DAYS):
                for candidate in slots_by_date.get(target_date, []):
                    start_t, _, court = candidate
                    slot_key = (start_t, court.id)
                    if slot_key in used_slots:
                        continue
                    slot = candidate
                    break
                if slot:
                    break
                target_date += timedelta(days=1)

            if slot:
                start_t, end_t, court = slot
                match.scheduled_time = start_t
                match.scheduled_end_time = end_t
                match.court = court
                match.save(update_fields=["scheduled_time", "scheduled_end_time", "court"])
                used_slots.add((start_t, court.id))
                scheduled_dates.append(target_date)
                current_date = target_date

        if scheduled_dates:
            prev_round_last_date = max(scheduled_dates)


def generate_knockout(tournament, teams=None, start_match=1, bracket_type="winners",
                      round_offset=0, group=""):
    """Generate single-elimination bracket."""
    if teams is None:
        teams = _active_teams(tournament)

    n = len(teams)
    if n < 2:
        return []

    # Calculate bracket size (next power of 2)
    bracket_size = 1
    while bracket_size < n:
        bracket_size *= 2

    num_rounds = int(math.log2(bracket_size))

    # Seed teams into bracket positions using standard bracket ordering
    seed_order = _bracket_seed_order(bracket_size)
    seeded = [None] * bracket_size
    for pos, seed_num in enumerate(seed_order):
        if seed_num <= n:
            seeded[pos] = teams[seed_num - 1]

    all_matches = []
    match_num = start_match
    current_round_matches = []

    # First round
    for i in range(0, bracket_size, 2):
        t1 = seeded[i]
        t2 = seeded[i + 1]
        is_bye = t1 is None or t2 is None
        m = Match(
            tournament=tournament,
            match_number=match_num,
            team1=t1,
            team2=t2,
            round_number=1 + round_offset,
            bracket_position=i // 2,
            bracket_type=bracket_type,
            status="bye" if is_bye else "upcoming",
            winner=t1 if (is_bye and t1) else (t2 if (is_bye and t2) else None),
            group=group,
        )
        current_round_matches.append(m)
        all_matches.append(m)
        match_num += 1

    # Save first round
    Match.objects.bulk_create(current_round_matches)

    # Subsequent rounds
    for round_num in range(2, num_rounds + 1):
        prev_matches = current_round_matches
        current_round_matches = []
        for i in range(0, len(prev_matches), 2):
            m = Match(
                tournament=tournament,
                match_number=match_num,
                round_number=round_num + round_offset,
                bracket_position=i // 2,
                bracket_type=bracket_type,
                status="upcoming",
                group=group,
            )
            current_round_matches.append(m)
            all_matches.append(m)
            match_num += 1

        Match.objects.bulk_create(current_round_matches)

        # Link previous round to next
        for i in range(0, len(prev_matches), 2):
            next_match = current_round_matches[i // 2]
            prev_matches[i].next_match = next_match
            prev_matches[i + 1].next_match = next_match
            prev_matches[i].save(update_fields=["next_match"])
            prev_matches[i + 1].save(update_fields=["next_match"])

            # Auto-advance byes
            if prev_matches[i].status == "bye" and prev_matches[i].winner:
                next_match.team1 = prev_matches[i].winner
            if prev_matches[i + 1].status == "bye" and prev_matches[i + 1].winner:
                next_match.team2 = prev_matches[i + 1].winner
            next_match.save(update_fields=["team1", "team2"])

    _assign_round_based_schedule(tournament, all_matches)
    return all_matches


def generate_knockout_placeholders(
    tournament, num_teams, start_match=1, bracket_type="winners", round_offset=0, group="", schedule=True
):
    """Generate a single-elimination bracket skeleton with TBD teams."""
    if num_teams < 2:
        return []

    bracket_size = 1
    while bracket_size < num_teams:
        bracket_size *= 2

    num_rounds = int(math.log2(bracket_size))
    all_matches = []
    match_num = start_match
    current_round_matches = []

    for i in range(0, bracket_size, 2):
        m = Match(
            tournament=tournament,
            match_number=match_num,
            round_number=1 + round_offset,
            bracket_position=i // 2,
            bracket_type=bracket_type,
            status="upcoming",
            group=group,
        )
        current_round_matches.append(m)
        all_matches.append(m)
        match_num += 1

    Match.objects.bulk_create(current_round_matches)

    for round_num in range(2, num_rounds + 1):
        prev_matches = current_round_matches
        current_round_matches = []
        for i in range(0, len(prev_matches), 2):
            m = Match(
                tournament=tournament,
                match_number=match_num,
                round_number=round_num + round_offset,
                bracket_position=i // 2,
                bracket_type=bracket_type,
                status="upcoming",
                group=group,
            )
            current_round_matches.append(m)
            all_matches.append(m)
            match_num += 1

        Match.objects.bulk_create(current_round_matches)

        for i in range(0, len(prev_matches), 2):
            next_match = current_round_matches[i // 2]
            prev_matches[i].next_match = next_match
            prev_matches[i + 1].next_match = next_match
            prev_matches[i].save(update_fields=["next_match"])
            prev_matches[i + 1].save(update_fields=["next_match"])

    if schedule:
        _assign_round_based_schedule(tournament, all_matches)
    return all_matches


def generate_hybrid(tournament):
    """Generate group stage (round robin) then knockout brackets."""
    teams = _active_teams(tournament)
    n = len(teams)
    num_groups = tournament.num_groups

    if n < num_groups * 2:
        num_groups = max(1, n // 2)

    # Assign groups using snake-draft seeding
    groups = {chr(65 + i): [] for i in range(num_groups)}
    group_keys = list(groups.keys())
    for idx, team in enumerate(teams):
        cycle = idx // num_groups
        if cycle % 2 == 0:
            g = group_keys[idx % num_groups]
        else:
            g = group_keys[num_groups - 1 - (idx % num_groups)]
        groups[g].append(team)
        from .models import TeamTournamentParticipation
        TeamTournamentParticipation.objects.filter(
            team=team, tournament=tournament
        ).update(group=g)

    # Generate round-robin for each group
    match_num = 1
    for group_name, group_teams in groups.items():
        gt = group_teams
        gn = len(gt)
        if gn < 2:
            continue

        if gn % 2 == 1:
            gt = gt + [None]
            gn += 1

        fixed = gt[0]
        rotating = gt[1:]
        for round_idx in range(gn - 1):
            pairs = [(fixed, rotating[0])]
            for i in range(1, gn // 2):
                pairs.append((rotating[i], rotating[gn - 1 - i]))
            rotating = [rotating[-1]] + rotating[:-1]

            for t1, t2 in pairs:
                if t1 is None or t2 is None:
                    continue
                Match.objects.create(
                    tournament=tournament,
                    match_number=match_num,
                    team1=t1,
                    team2=t2,
                    round_number=round_idx + 1,
                    group=group_name,
                    status="upcoming",
                )
                match_num += 1

    # Pre-create knockout placeholders (TBD) so schedule is visible from start.
    advance_count = max(0, tournament.teams_per_group_advance or 0)
    advancing_slots = sum(
        min(len(group_teams), advance_count) for group_teams in groups.values()
    )
    if advancing_slots >= 2:
        # Use the furthest group-stage round as the knockout round-number offset.
        max_group_round = max(
            (len(group_teams) - 1 for group_teams in groups.values() if len(group_teams) >= 2),
            default=0,
        )
        generate_knockout_placeholders(
            tournament,
            num_teams=advancing_slots,
            start_match=match_num,
            round_offset=max_group_round,
            schedule=False,
        )

    # Schedule group stage normally (multiple matches per day as courts allow)
    _assign_schedule_to_group_stage(tournament)
    # Schedule knockout placeholders: one match per day, final with 1-day rest gap
    _assign_hybrid_knockout_schedule(tournament)


def generate_double_elimination(tournament):
    """Generate double elimination bracket (winners + losers)."""
    # Start with a standard winners bracket
    teams = _active_teams(tournament)
    generate_knockout(tournament, teams=teams, bracket_type="winners")
    # Losers bracket matches are created dynamically as teams are eliminated


def generate_consolation(tournament):
    """Generate main knockout bracket; consolation is generated after round 1 completes."""
    teams = _active_teams(tournament)
    generate_knockout(tournament, teams=teams, bracket_type="winners")


def generate_consolation_if_ready(tournament):
    """Generate consolation bracket from first-round losers once round 1 is complete."""
    if tournament.format != "consolation":
        return False
    if tournament.matches.filter(bracket_type="consolation").exists():
        return False

    first_round = tournament.matches.filter(bracket_type="winners", round_number=1)
    if not first_round.exists():
        return False

    incomplete = first_round.exclude(status__in=["confirmed", "forfeited", "cancelled", "bye"])
    if incomplete.exists():
        return False

    losers = []
    for match in first_round:
        if not match.team1_id or not match.team2_id or not match.winner_id:
            continue
        if match.winner_id == match.team1_id:
            losers.append(match.team2)
        elif match.winner_id == match.team2_id:
            losers.append(match.team1)

    if len(losers) < 2:
        return False

    max_match = tournament.matches.aggregate(m=models.Max("match_number"))["m"] or 0
    generate_knockout(
        tournament,
        teams=losers,
        start_match=max_match + 1,
        bracket_type="consolation",
    )
    return True


def generate_fixtures(tournament):
    """Main entry point for fixture generation."""
    # Clear existing matches
    tournament.matches.all().delete()
    tournament.open_slots.all().delete()

    fmt = tournament.format
    if fmt == "round_robin":
        generate_round_robin(tournament)
    elif fmt == "double_round_robin":
        generate_double_round_robin(tournament)
    elif fmt == "knockout":
        generate_knockout(tournament)
    elif fmt == "double_elimination":
        generate_double_elimination(tournament)
    elif fmt == "consolation":
        generate_consolation(tournament)
    elif fmt == "hybrid":
        generate_hybrid(tournament)


def estimate_required_matches(tournament, team_count=None):
    """Estimate how many match slots are needed for a tournament format."""
    n = team_count if team_count is not None else tournament.team_participations.filter(status="active").count()
    if n < 2:
        return 0

    if tournament.format == "round_robin":
        return n * (n - 1) // 2
    if tournament.format == "double_round_robin":
        return n * (n - 1)
    if tournament.format in ("knockout", "consolation"):
        return n - 1
    if tournament.format == "double_elimination":
        return max(n - 1, (2 * n) - 2)
    if tournament.format == "hybrid":
        num_groups = max(1, min(tournament.num_groups or 1, n))
        base_size = n // num_groups
        remainder = n % num_groups
        group_matches = 0
        advancers = 0
        for idx in range(num_groups):
            size = base_size + (1 if idx < remainder else 0)
            if size >= 2:
                group_matches += size * (size - 1) // 2
            advancers += min(size, max(1, tournament.teams_per_group_advance))
        return group_matches + max(0, advancers - 1)
    return n - 1


def count_available_slots(tournament):
    """Return the number of currently schedulable court-bound slots."""
    courts = list(tournament.courts.filter(is_available=True))
    if not courts:
        return 0
    return len(_build_slots(tournament, courts))


def _assign_schedule(tournament, matches_data):
    """Assign times and courts to match data dicts, then bulk create."""
    courts = list(tournament.courts.filter(is_available=True))

    if not courts:
        # Create matches without schedule
        Match.objects.bulk_create([
            Match(tournament=tournament, **md) for md in matches_data
        ])
        return

    slots = _build_slots(tournament, courts)
    used_slots = set()
    matches_to_create = []

    for md in matches_data:
        t1 = md["team1"]
        t2 = md["team2"]
        result = _find_preferred_slot(t1, t2, slots, used_slots, matches_to_create, tournament)
        if result:
            start_t, end_t, court = result
            md["scheduled_time"] = start_t
            md["scheduled_end_time"] = end_t
            md["court"] = court
            used_slots.add((start_t, court.id))

        matches_to_create.append(Match(tournament=tournament, **md))

    Match.objects.bulk_create(matches_to_create)


def _has_team_conflict(t1, t2, start, end, used_slots, pending_matches):
    """Check if either team is already scheduled at this time or on the same day."""
    target_day = timezone.localtime(start).date() if timezone.is_aware(start) else start.date()
    target_team_ids = {tid for tid in (getattr(t1, "id", None), getattr(t2, "id", None)) if tid}

    # Placeholder matches (TBD vs TBD) should not block each other by team conflict.
    if not target_team_ids:
        return False

    for m in pending_matches:
        if not m.scheduled_time:
            continue

        match_day = (
            timezone.localtime(m.scheduled_time).date()
            if timezone.is_aware(m.scheduled_time)
            else m.scheduled_time.date()
        )
        match_team_ids = {tid for tid in (m.team1_id, m.team2_id) if tid}
        same_team = bool(target_team_ids & match_team_ids)
        if not same_team:
            continue

        if match_day == target_day:
            return True

        if m.scheduled_end_time and start < m.scheduled_end_time and end > m.scheduled_time:
            return True
    return False


def _find_preferred_slot(t1, t2, slots, used_slots, pending_matches, tournament=None):
    """Find the best available slot using 3-tier preference matching.

    Tries: 1) mutual preferred courts, 2) any preferred court, 3) any available slot.
    Returns (start_time, end_time, court) or None if no slot found.
    """
    if tournament is None and slots:
        tournament = slots[0][2].tournament

    def _preferred_court_ids(team):
        if not team or not tournament:
            return set()
        return set(
            TeamTournamentCourtPreference.objects.filter(
                participation__team=team,
                participation__tournament=tournament,
            ).values_list("court_id", flat=True)
        )

    t1_prefs = _preferred_court_ids(t1)
    t2_prefs = _preferred_court_ids(t2)
    mutual_prefs = t1_prefs & t2_prefs
    any_prefs = t1_prefs | t2_prefs

    # Try mutual preferred courts first
    if mutual_prefs:
        for start_t, end_t, court in slots:
            slot_key = (start_t, court.id)
            if slot_key in used_slots:
                continue
            if _has_team_conflict(t1, t2, start_t, end_t, used_slots, pending_matches):
                continue
            if court.id in mutual_prefs:
                return start_t, end_t, court

    # Try any preferred court
    if any_prefs:
        for start_t, end_t, court in slots:
            slot_key = (start_t, court.id)
            if slot_key in used_slots:
                continue
            if _has_team_conflict(t1, t2, start_t, end_t, used_slots, pending_matches):
                continue
            if court.id in any_prefs:
                return start_t, end_t, court

    # Fallback to any available slot
    for start_t, end_t, court in slots:
        slot_key = (start_t, court.id)
        if slot_key in used_slots:
            continue
        if _has_team_conflict(t1, t2, start_t, end_t, used_slots, pending_matches):
            continue
        return start_t, end_t, court

    return None


def _build_slots(tournament, courts):
    """Build available time slots for scheduling."""
    duration = timedelta(minutes=tournament.default_match_duration)
    base_date = tournament.start_date or timezone.localdate()
    slots = []
    seen = set()

    # When court availability has no end_date ("Open"), use the tournament's end_date.
    # If neither is set, extend 365 days from start to avoid artificially capping slots.
    open_fallback = tournament.end_date or (base_date + timedelta(days=365))

    availabilities = list(
        CourtAvailability.objects.filter(
            court__tournament=tournament,
            court__is_available=True,
            is_active=True,
        ).select_related("court").order_by("court_id", "weekday", "start_time")
    )
    if availabilities:
        for availability in availabilities:
            range_start = max(base_date, availability.start_date or base_date)
            range_end = availability.end_date or open_fallback
            current_date = range_start
            while current_date <= range_end:
                if current_date.weekday() == availability.weekday:
                    if availability.additional_start_times:
                        explicit_times = [availability.start_time]
                        for part in availability.additional_start_times.split(","):
                            part = part.strip()
                            if not part:
                                continue
                            try:
                                explicit_times.append(datetime.strptime(part, "%H:%M").time())
                            except ValueError:
                                continue
                        explicit_times = sorted(set(explicit_times))
                        for start_time in explicit_times:
                            slot_start = timezone.make_aware(datetime.combine(current_date, start_time))
                            slot_end = slot_start + duration
                            if slot_end.date() != current_date:
                                continue
                            slot_key = (slot_start, availability.court_id)
                            if slot_key not in seen:
                                slots.append((slot_start, slot_end, availability.court))
                                seen.add(slot_key)
                    else:
                        slot_start = timezone.make_aware(datetime.combine(current_date, availability.start_time))
                        slot_limit = timezone.make_aware(datetime.combine(current_date, availability.end_time))
                        current = slot_start
                        duration_minutes = duration.total_seconds() / 60
                        max_daily_slots = int((slot_limit - slot_start).total_seconds() / 60 // duration_minutes)
                        slots_to_allocate = max_daily_slots
                        if availability.matches_per_court_per_day and availability.matches_per_court_per_day < max_daily_slots:
                            slots_to_allocate = availability.matches_per_court_per_day
                        slot_count = 0
                        while current + duration <= slot_limit and slot_count < slots_to_allocate:
                            slot_key = (current, availability.court_id)
                            if slot_key not in seen:
                                slots.append((current, current + duration, availability.court))
                                seen.add(slot_key)
                                slot_count += 1
                            current += duration
                current_date += timedelta(days=1)
        return sorted(slots, key=lambda item: (item[0], item[2].id))

    time_slots = list(tournament.time_slots.select_related("court").order_by("start_time"))
    if time_slots:
        for ts in time_slots:
            current = ts.start_time
            target_courts = [ts.court] if ts.court else courts
            while current + duration <= ts.end_time:
                for court in target_courts:
                    slot_key = (current, court.id)
                    if slot_key not in seen:
                        slots.append((current, current + duration, court))
                        seen.add(slot_key)
                current += duration
        return sorted(slots, key=lambda item: (item[0], item[2].id))

    start = timezone.make_aware(datetime.combine(base_date, time(hour=9, minute=0)))
    for day_offset in range(30):
        day_start = start + timedelta(days=day_offset)
        for hour_offset in range(12):
            slot_start = day_start + timedelta(minutes=30 * hour_offset)
            slot_end = slot_start + duration
            for court in courts:
                slot_key = (slot_start, court.id)
                if slot_key not in seen:
                    slots.append((slot_start, slot_end, court))
                    seen.add(slot_key)
    return sorted(slots, key=lambda item: (item[0], item[2].id))


def _assign_schedule_to_matches(tournament, matches):
    """Assign schedule to already-created Match objects, respecting court preferences."""
    courts = list(tournament.courts.filter(is_available=True))
    if not courts:
        return

    slots = _build_slots(tournament, courts)
    used_slots = set()
    scheduled_matches = []

    for match in matches:
        if match.status == "bye":
            continue

        result = _find_preferred_slot(
            match.team1, match.team2, slots, used_slots, scheduled_matches, tournament
        )
        if result:
            start_t, end_t, court = result
            match.scheduled_time = start_t
            match.scheduled_end_time = end_t
            match.court = court
            used_slots.add((start_t, court.id))
            match.save(update_fields=["scheduled_time", "scheduled_end_time", "court"])
        scheduled_matches.append(match)


def _assign_schedule_to_group_stage(tournament):
    """Schedule only the group-stage (group != '') matches for a hybrid tournament.

    Intentionally excludes knockout placeholder matches so they are not given
    slots from the group-stage pool.
    """
    matches = list(
        tournament.matches
        .filter(status="upcoming")
        .exclude(group="")
        .select_related("team1", "team2")
        .order_by("group", "round_number", "match_number")
    )
    courts = list(tournament.courts.filter(is_available=True))
    if not courts or not matches:
        return

    slots = _build_slots(tournament, courts)
    used_slots = set()
    scheduled_matches = []

    for match in matches:
        result = _find_preferred_slot(match.team1, match.team2, slots, used_slots, scheduled_matches, tournament)
        if result:
            start_t, end_t, court = result
            match.scheduled_time = start_t
            match.scheduled_end_time = end_t
            match.court = court
            used_slots.add((start_t, court.id))
            match.save(update_fields=["scheduled_time", "scheduled_end_time", "court"])
        scheduled_matches.append(match)


def _assign_hybrid_knockout_schedule(tournament):
    """Schedule knockout placeholder matches for a hybrid tournament.

    Rules:
    - Each knockout match is scheduled on its own separate day (one game per day).
    - The final round gets a 1-day rest gap from the previous knockout round.
    - Scheduling starts the day after the last group-stage match.
    """
    courts = list(tournament.courts.filter(is_available=True))
    if not courts:
        return

    ko_matches = list(
        tournament.matches
        .filter(group="", bracket_type="winners", status="upcoming")
        .order_by("round_number", "match_number")
    )
    if not ko_matches:
        return

    # Determine the day after the last scheduled group-stage match
    last_group = (
        tournament.matches
        .filter(scheduled_time__isnull=False)
        .exclude(group="")
        .order_by("-scheduled_time")
        .first()
    )
    if last_group and last_group.scheduled_time:
        last_group_date = (
            timezone.localtime(last_group.scheduled_time).date()
            if timezone.is_aware(last_group.scheduled_time)
            else last_group.scheduled_time.date()
        )
    else:
        last_group_date = tournament.start_date or timezone.localdate()

    _assign_round_based_schedule(
        tournament,
        ko_matches,
        phase_start_date=last_group_date + timedelta(days=1),
    )


def _assign_schedule_to_existing(tournament, knockout_only=False):
    """Assign schedule to unscheduled upcoming matches, respecting existing assignments.

    When knockout_only=True, only knockout/consolation rounds (group='') are scheduled.
    """
    matches_qs = tournament.matches.filter(
        status="upcoming",
        scheduled_time__isnull=True,
    )
    if knockout_only:
        matches_qs = matches_qs.filter(group="", bracket_type__in=["winners", "losers", "consolation"])

    matches = list(
        matches_qs.select_related("team1", "team2").order_by("group", "round_number", "match_number")
    )
    courts = list(tournament.courts.filter(is_available=True))
    if not courts or not matches:
        return

    slots = _build_slots(tournament, courts)
    occupied_matches = list(
        tournament.matches
        .filter(scheduled_time__isnull=False)
        .exclude(status__in=["cancelled", "bye"])
        .select_related("team1", "team2")
    )
    used_slots = {
        (m.scheduled_time, m.court_id)
        for m in occupied_matches
        if m.scheduled_time and m.court_id
    }
    scheduled_matches = occupied_matches[:]

    for match in matches:
        result = _find_preferred_slot(
            match.team1, match.team2, slots, used_slots, scheduled_matches, tournament
        )
        if result:
            start_t, end_t, court = result
            match.scheduled_time = start_t
            match.scheduled_end_time = end_t
            match.court = court
            used_slots.add((start_t, court.id))
            match.save(update_fields=["scheduled_time", "scheduled_end_time", "court"])
        scheduled_matches.append(match)
