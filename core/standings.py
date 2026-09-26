"""Standings calculation and bracket progression logic."""
from collections import defaultdict
from django.db import models
from django.db.models import F, Max
from .models import Match, Team


def _tournament_teams(tournament, statuses):
    """Return teams for a tournament filtered by participation status."""
    return Team.objects.filter(
        participations__tournament=tournament,
        participations__status__in=statuses,
    ).annotate(
        group=F("participations__group"),
        participation_status=F("participations__status"),
    ).distinct()


def _stage_matches(tournament, matches, group=None):
    """The matches that count for a standings table: one group's, or, in a
    hybrid, only the group stage's. A hybrid's knockout matches carry no group
    letter and earn no points (AI_STRUCTURE_PLAN.md ST-1, gap G-2)."""
    if group:
        return matches.filter(group=group)
    if tournament.format == "hybrid":
        return matches.exclude(group="")
    return matches


def calculate_standings(tournament, group=None):
    """Calculate standings for round-robin or group stage."""
    teams = _tournament_teams(tournament, ["active", "withdrawn"])
    if group:
        teams = teams.filter(participations__group=group)

    matches = _stage_matches(tournament, tournament.matches.filter(status="confirmed"), group)

    standings = {}
    for team in teams:
        standings[team.id] = {
            "team": team,
            "played": 0,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "games_won": 0,
            "games_lost": 0,
            "game_diff": 0,
            "points": 0,
            # Withdrawn teams keep their row (and the points they earned) but
            # never advance or take a placing (AI_STRUCTURE_PLAN.md ST-2).
            "withdrawn": team.participation_status == "withdrawn",
        }

    for match in matches:
        if not match.team1_id or not match.team2_id:
            continue
        if match.score_team1 is None or match.score_team2 is None:
            continue

        t1 = match.team1_id
        t2 = match.team2_id

        if t1 not in standings or t2 not in standings:
            continue

        standings[t1]["played"] += 1
        standings[t2]["played"] += 1
        standings[t1]["games_won"] += match.score_team1
        standings[t1]["games_lost"] += match.score_team2
        standings[t2]["games_won"] += match.score_team2
        standings[t2]["games_lost"] += match.score_team1

        if match.score_team1 > match.score_team2:
            standings[t1]["wins"] += 1
            standings[t1]["points"] += tournament.points_per_win
            standings[t2]["losses"] += 1
            standings[t2]["points"] += tournament.points_per_loss
        elif match.score_team1 < match.score_team2:
            standings[t2]["wins"] += 1
            standings[t2]["points"] += tournament.points_per_win
            standings[t1]["losses"] += 1
            standings[t1]["points"] += tournament.points_per_loss
        else:
            standings[t1]["draws"] += 1
            standings[t2]["draws"] += 1
            standings[t1]["points"] += tournament.points_per_draw
            standings[t2]["points"] += tournament.points_per_draw

    # Also count forfeited matches
    forfeits = _stage_matches(tournament, tournament.matches.filter(status="forfeited"), group)

    for match in forfeits:
        if match.winner_id and match.winner_id in standings:
            standings[match.winner_id]["played"] += 1
            standings[match.winner_id]["wins"] += 1
            standings[match.winner_id]["points"] += tournament.points_per_win
        loser = None
        if match.team1_id and match.team1_id != match.winner_id:
            loser = match.team1_id
        elif match.team2_id and match.team2_id != match.winner_id:
            loser = match.team2_id
        if loser and loser in standings:
            standings[loser]["played"] += 1
            standings[loser]["losses"] += 1
            standings[loser]["points"] += tournament.points_per_loss

    for s in standings.values():
        s["game_diff"] = s["games_won"] - s["games_lost"]

    return rank_standings(tournament, list(standings.values()), group=group)


def rank_standings(tournament, rows, group=None):
    """Sort standings rows by the tournament's tiebreakers and number them.

    `rows` are calculate_standings-shaped dicts (at least "team", "points",
    "game_diff" and "games_won"). Shared by calculate_standings and the
    analytics what-if simulator, so a projected table breaks ties exactly the
    way the real one would. Returns a new list; sets "rank" on each row.

    Head-to-head, when configured, reads *real* results from the database: a
    projected outcome has no score, so it can't take part in head-to-head.
    """
    # Head-to-head is meaningful only *between* the teams that are tied, so it
    # cannot be a per-team scalar computed before sorting. Sort on the scalar
    # tiebreakers first, then re-order each run of still-tied teams using their
    # mutual results.
    tiebreakers = tournament.get_tiebreaker_order()
    scalar_tiebreakers = [tb for tb in tiebreakers if tb != "head_to_head"]

    def scalar_key(standing):
        return _sort_key(standing, scalar_tiebreakers)

    # -team.id so that, with reverse=True, a lower id ranks first. Without a
    # final deterministic component the order of fully-tied teams came out of
    # dict iteration and was not stable.
    result = sorted(
        rows,
        key=lambda s: (scalar_key(s), -s["team"].id),
        reverse=True,
    )
    if "head_to_head" in tiebreakers:
        result = _apply_head_to_head(tournament, result, scalar_key, group=group)

    for idx, s in enumerate(result):
        s["rank"] = idx + 1
    return result


def _sort_key(standing, tiebreakers):
    """Build a tuple sort key from the scalar tiebreakers.

    Only tiebreakers that reduce to a per-team number belong here.
    "head_to_head" does not, and is handled by _apply_head_to_head after this
    key has been sorted on.
    """
    key = [standing["points"]]
    for tb in tiebreakers:
        if tb == "game_diff":
            key.append(standing["game_diff"])
        elif tb == "games_won":
            key.append(standing["games_won"])
    return tuple(key)


def _head_to_head_matches(tournament, group=None):
    """All finished (confirmed or forfeited) matches that head-to-head reads.

    Loaded once per ranking and passed to _head_to_head_points, so the
    tiebreaker costs one query however many tied groups there are.
    """
    matches = tournament.matches.filter(status__in=("confirmed", "forfeited"))
    return list(_stage_matches(tournament, matches, group))


def _head_to_head_points(tournament, team_ids, group=None, matches=None):
    """Return {team_id: points} counting only matches among `team_ids`.

    Scoring mirrors calculate_standings exactly, including points_per_loss and
    points_per_draw, so a head-to-head table is the same table restricted to
    the tied teams' mutual fixtures.

    `matches` is an optional preloaded _head_to_head_matches() list; without
    it the matches are read from the database.
    """
    points = {tid: 0 for tid in team_ids}
    if matches is None:
        matches = _head_to_head_matches(tournament, group=group)
    mutual = [m for m in matches if m.team1_id in points and m.team2_id in points]
    confirmed = [m for m in mutual if m.status == "confirmed"]
    forfeits = [m for m in mutual if m.status == "forfeited"]

    for match in confirmed:
        if match.score_team1 is None or match.score_team2 is None:
            continue
        if match.score_team1 > match.score_team2:
            points[match.team1_id] += tournament.points_per_win
            points[match.team2_id] += tournament.points_per_loss
        elif match.score_team2 > match.score_team1:
            points[match.team2_id] += tournament.points_per_win
            points[match.team1_id] += tournament.points_per_loss
        else:
            points[match.team1_id] += tournament.points_per_draw
            points[match.team2_id] += tournament.points_per_draw

    for match in forfeits:
        if match.winner_id not in points:
            continue
        points[match.winner_id] += tournament.points_per_win
        loser_id = (
            match.team2_id if match.winner_id == match.team1_id else match.team1_id
        )
        if loser_id in points:
            points[loser_id] += tournament.points_per_loss

    return points


def _apply_head_to_head(tournament, ordered_rows, scalar_key, group=None):
    """Re-order runs of rows that tie on `scalar_key` using mutual results.

    Note the ordering semantics: head-to-head is applied *after* the scalar
    tiebreakers, matching the configured order "points, game_diff, games_won,
    head_to_head". Some competition rules apply head-to-head before game
    difference; that would be a different tiebreaker_order, not a change here.
    """
    result = []
    index = 0
    finished_matches = None  # loaded on the first tie, then reused
    while index < len(ordered_rows):
        end = index + 1
        while end < len(ordered_rows) and scalar_key(ordered_rows[end]) == scalar_key(
            ordered_rows[index]
        ):
            end += 1
        run = ordered_rows[index:end]
        if len(run) > 1:
            ids = [row["team"].id for row in run]
            if finished_matches is None:
                finished_matches = _head_to_head_matches(tournament, group=group)
            h2h = _head_to_head_points(tournament, ids, group=group, matches=finished_matches)
            run.sort(
                key=lambda r: (h2h.get(r["team"].id, 0), -r["team"].id),
                reverse=True,
            )
        result.extend(run)
        index = end
    return result


def _match_loser(match):
    """Return the team that lost `match`, or None if it cannot be determined."""
    if not match.winner_id:
        return None
    if match.winner_id == match.team1_id:
        return match.team2
    if match.winner_id == match.team2_id:
        return match.team1
    return None


def _place_team(target, team, slot=None, _depth=0):
    """Put `team` into `target`, walking it straight through a walkover.

    A losers-bracket match marked "bye" has at most one feeder that can ever
    deliver a team -- the others were byes in the winners bracket -- so the
    single arrival advances without playing. That can cascade through several
    rounds, hence the recursion, bounded because each step moves strictly
    forward through a finite bracket.
    """
    if team is None or target is None or _depth > 64:
        return

    if slot == 1:
        target.team1 = team
    elif slot == 2:
        target.team2 = team
    elif not target.team1_id:
        target.team1 = team
    else:
        target.team2 = team
    target.save(update_fields=["team1", "team2"])

    if target.status == "bye" and not target.winner_id:
        target.winner = team
        target.save(update_fields=["winner"])
        if target.next_match_id:
            _place_team(
                Match.objects.get(pk=target.next_match_id), team,
                target.next_match_slot, _depth + 1,
            )


def advance_winner(match):
    """Propagate a decided match to whatever comes next.

    The winner moves to `next_match`, and in double elimination the loser moves
    to `next_loser_match`. Both are handled here rather than in a separate
    call, because the six places that finalise a match all call this one and a
    seventh call at each of them is exactly the kind of drift this codebase has
    already been bitten by.
    """
    if match.next_loser_match_id:
        loser = _match_loser(match)
        if loser is not None:
            _place_team(
                Match.objects.get(pk=match.next_loser_match_id), loser,
                match.next_loser_match_slot,
            )

    _resolve_grand_final(match)

    if not match.next_match:
        return

    next_match = match.next_match
    if match.next_match_slot in (1, 2):
        # Explicit routing: a losers-bracket match is fed from two directions,
        # so previous_matches ordering cannot say which slot this is.
        _place_team(next_match, match.winner, match.next_match_slot)
        return

    # Determine which slot (team1 or team2) the winner fills
    prev_matches = list(next_match.previous_matches.order_by("bracket_position"))
    if len(prev_matches) >= 1 and prev_matches[0].id == match.id:
        next_match.team1 = match.winner
    elif len(prev_matches) >= 2 and prev_matches[1].id == match.id:
        next_match.team2 = match.winner
    else:
        # Fallback
        if not next_match.team1:
            next_match.team1 = match.winner
        else:
            next_match.team2 = match.winner
    next_match.save(update_fields=["team1", "team2"])


def _resolve_grand_final(match):
    """Decide whether a double-elimination decider is played or cancelled.

    The grand final is the one match where the two sides arrive unequal: the
    winners-bracket champion has no defeats, the losers-bracket champion has
    one. If the winners champion wins, the title is settled and the decider is
    cancelled. If the losers champion wins, both have one defeat and the
    decider is played.
    """
    if match.bracket_type != "grand_final" or match.round_number != 1:
        return
    if not match.winner_id:
        return

    decider = (
        Match.objects.filter(
            tournament_id=match.tournament_id,
            bracket_type="grand_final",
            round_number=2,
        )
        .exclude(pk=match.pk)
        .first()
    )
    if decider is None:
        return

    # team1 is the winners-bracket champion; see generate_double_elimination.
    if match.winner_id == match.team1_id:
        if decider.status not in ("confirmed", "forfeited"):
            decider.status = "cancelled"
            decider.save(update_fields=["status"])
        return

    decider.team1 = match.team1
    decider.team2 = match.team2
    if decider.status == "cancelled":
        decider.status = "upcoming"
    decider.save(update_fields=["team1", "team2", "status"])


def get_bracket_data(tournament):
    """Build bracket structure for display."""
    matches = tournament.matches.filter(bracket_type="winners", group="").order_by("round_number", "bracket_position")
    rounds = defaultdict(list)
    for m in matches:
        rounds[m.round_number].append(m)
    return dict(sorted(rounds.items()))


def get_losers_bracket_data(tournament):
    """Build the losers-bracket structure for display, keyed by round.

    Walkover and vestigial matches (status "bye") are left out: they exist so
    the bracket's shape stays regular when the field is not a power of two, but
    nobody plays them and showing them reads as a bug.
    """
    matches = (
        tournament.matches
        .filter(bracket_type="losers")
        .exclude(status="bye")
        .select_related("team1", "team2", "winner", "court")
        .order_by("round_number", "bracket_position")
    )
    rounds = defaultdict(list)
    for match in matches:
        rounds[match.round_number].append(match)
    return dict(sorted(rounds.items()))


def get_grand_final_matches(tournament):
    """Return the grand final, plus the decider when one is still live."""
    return list(
        tournament.matches
        .filter(bracket_type="grand_final")
        .exclude(status="cancelled")
        .select_related("team1", "team2", "winner", "court")
        .order_by("round_number")
    )


def get_third_place_match(tournament):
    """Return the third-place match for a tournament, or None."""
    if not getattr(tournament, "enable_third_place_match", False):
        return None
    return (
        tournament.matches
        .filter(bracket_type="third_place")
        .select_related("team1", "team2", "court", "winner")
        .first()
    )


def advance_loser_to_third_place(match):
    """After a semi-final is confirmed, place the loser in the third-place match."""
    if not getattr(match.tournament, "enable_third_place_match", False):
        return
    if match.bracket_type != "winners":
        return
    if not match.winner_id:
        return
    # Only act on semi-finals: their next_match must be the final (which has no next_match)
    if not match.next_match_id:
        return
    next_match = Match.objects.filter(pk=match.next_match_id).values("next_match_id").first()
    if not next_match or next_match["next_match_id"] is not None:
        return
    # Determine loser
    if match.winner_id == match.team1_id:
        loser = match.team2
    else:
        loser = match.team1
    if not loser:
        return
    third_place = match.tournament.matches.filter(bracket_type="third_place").first()
    if not third_place:
        return
    if not third_place.team1_id:
        third_place.team1 = loser
        third_place.save(update_fields=["team1"])
    elif not third_place.team2_id:
        third_place.team2 = loser
        third_place.save(update_fields=["team2"])


def _seed_order(size):
    """Return standard bracket seed positions for a power-of-two size."""
    if size == 1:
        return [1]
    half = _seed_order(size // 2)
    return [item for seed in half for item in (seed, size + 1 - seed)]


def check_group_stage_complete(tournament):
    """Check if all group matches are done, generate knockout if so."""
    from .scheduling import generate_knockout

    group_matches = tournament.matches.filter(group__gt="").exclude(group="")

    # Fallback for hybrid tournaments where schedule was generated without group labels
    if not group_matches.exists() and tournament.format == "hybrid":
        # Only proceed if there are knockout placeholder matches (team1=None, team2=None)
        has_ko_placeholders = tournament.matches.filter(
            team1__isnull=True, team2__isnull=True, group=""
        ).exists()
        if not has_ko_placeholders:
            return False
        # Group stage = all matches with both teams assigned
        rr_qs = tournament.matches.filter(team1__isnull=False, team2__isnull=False)
        if not rr_qs.exists():
            return False
        # Only proceed if all such matches are terminal
        if rr_qs.exclude(status__in=["confirmed", "forfeited", "cancelled", "bye"]).exists():
            return False
        # Assign group "A" to these matches and to all active teams so existing logic can proceed
        rr_qs.update(group="A")
        tournament.team_participations.filter(status="active").update(group="A")
        group_matches = tournament.matches.filter(group="A")

    if not group_matches.exists():
        return False

    incomplete = group_matches.exclude(status__in=["confirmed", "forfeited", "cancelled", "bye"])
    if incomplete.exists():
        return False

    # Group stage complete – generate knockout from top teams
    groups = set(
        tournament.team_participations.filter(status="active").exclude(group="").values_list("group", flat=True)
    )
    advancing = []
    for group_name in sorted(groups):
        standings = calculate_standings(tournament, group=group_name)
        # A withdrawn team keeps its table row but can't go through: the next
        # team moves up into its place (X-1).
        top = [s for s in standings if not s["withdrawn"]][:tournament.teams_per_group_advance]
        for s in top:
            advancing.append(s["team"])

    if len(advancing) >= 2:
        ko_matches = tournament.matches.filter(group="", bracket_type="winners")
        if ko_matches.exists():
            # If slots are already filled, bracket has already been initialized.
            if ko_matches.filter(
                models.Q(team1__isnull=False) | models.Q(team2__isnull=False)
            ).exists():
                return False

            first_round_number = ko_matches.order_by("round_number").values_list("round_number", flat=True).first()
            first_round = list(
                ko_matches.filter(round_number=first_round_number).order_by("bracket_position", "match_number")
            )
            if not first_round:
                return False

            bracket_size = len(first_round) * 2
            seed_order = _seed_order(bracket_size)
            seeded = [None] * bracket_size
            for pos, seed_num in enumerate(seed_order):
                if seed_num <= len(advancing):
                    seeded[pos] = advancing[seed_num - 1]

            non_first_round = ko_matches.exclude(round_number=first_round_number)
            for match in non_first_round:
                match.team1 = None
                match.team2 = None
                match.winner = None
                match.score_team1 = None
                match.score_team2 = None
                match.submitted_by = None
                match.confirmed_by = None
                match.status = "upcoming"
                match.save(update_fields=[
                    "team1", "team2", "winner", "score_team1", "score_team2",
                    "submitted_by", "confirmed_by", "status",
                ])

            bye_matches = []
            confirmed_matches = []
            for idx, match in enumerate(first_round):
                slot1 = idx * 2
                slot2 = slot1 + 1
                t1 = seeded[slot1] if slot1 < len(seeded) else None
                t2 = seeded[slot2] if slot2 < len(seeded) else None
                is_bye = t1 is None or t2 is None
                match.team1 = t1
                match.team2 = t2
                match.winner = None
                if is_bye:
                    if t1:
                        match.winner = t1
                    elif t2:
                        match.winner = t2
                match.score_team1 = None
                match.score_team2 = None
                match.submitted_by = None
                match.confirmed_by = None
                match.status = "bye" if is_bye else "upcoming"
                match.save(update_fields=[
                    "team1", "team2", "winner", "score_team1", "score_team2",
                    "submitted_by", "confirmed_by", "status",
                ])
                if match.status == "bye" and match.winner:
                    bye_matches.append(match)

            for bye_match in bye_matches:
                advance_winner(bye_match)

            # Propagate winners for any first-round matches that are already confirmed
            # (handles the case where results were entered before knockout was seeded)
            for match in first_round:
                match.refresh_from_db()
                if match.status in ("confirmed", "forfeited") and match.winner:
                    confirmed_matches.append(match)
            for confirmed_match in confirmed_matches:
                advance_winner(confirmed_match)

            return True

        max_match = tournament.matches.aggregate(m=Max("match_number"))["m"] or 0
        max_round = tournament.matches.aggregate(m=Max("round_number"))["m"] or 0
        generate_knockout(
            tournament,
            teams=advancing,
            start_match=max_match + 1,
            round_offset=max_round,
        )
        return True
    return False


def _determine_champion(tournament):
    """Return the champion Team for a completed tournament, or None."""
    fmt = tournament.format

    if fmt in ("round_robin", "double_round_robin"):
        standings = calculate_standings(tournament)
        if standings:
            return standings[0]["team"]
        return None

    if fmt == "double_elimination":
        # The winners-bracket final decides nothing on its own: its loser drops
        # into the losers bracket. The title is the last grand-final match
        # actually played -- the decider when there was one, otherwise the
        # grand final itself.
        decided = (
            tournament.matches
            .filter(bracket_type="grand_final",
                    status__in=["confirmed", "forfeited"],
                    winner__isnull=False)
            .order_by("-round_number")
            .first()
        )
        if decided:
            return decided.winner
        return None

    # Bracket-based formats: winner of the winners-bracket final
    # (the highest-round match with next_match=None and both teams set)
    final = (
        tournament.matches
        .filter(bracket_type="winners", next_match__isnull=True,
                group="",
                team1__isnull=False, team2__isnull=False,
                status="confirmed")
        .order_by("-round_number")
        .first()
    )
    if final and final.winner:
        return final.winner

    # Fallback: any confirmed match with no next match
    final = (
        tournament.matches
        .filter(next_match__isnull=True, team1__isnull=False,
                group="",
                team2__isnull=False, status="confirmed")
        .order_by("-round_number")
        .first()
    )
    return final.winner if final else None
