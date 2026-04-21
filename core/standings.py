"""Standings calculation and bracket progression logic."""
from collections import defaultdict
from django.db import models
from django.db.models import Max
from .models import Match, Team


def calculate_standings(tournament, group=None):
    """Calculate standings for round-robin or group stage."""
    teams = tournament.teams.filter(status__in=["active", "withdrawn"])
    if group:
        teams = teams.filter(group=group)

    matches = tournament.matches.filter(status="confirmed")
    if group:
        matches = matches.filter(group=group)

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
    forfeits = tournament.matches.filter(status="forfeited")
    if group:
        forfeits = forfeits.filter(group=group)

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

    # Sort by tiebreaker
    tiebreakers = tournament.get_tiebreaker_order()
    result = sorted(standings.values(), key=lambda s: _sort_key(s, tiebreakers), reverse=True)

    for idx, s in enumerate(result):
        s["rank"] = idx + 1
    return result


def _sort_key(standing, tiebreakers):
    """Build a tuple sort key from tiebreaker config."""
    key = [standing["points"]]
    for tb in tiebreakers:
        if tb == "game_diff":
            key.append(standing["game_diff"])
        elif tb == "games_won":
            key.append(standing["games_won"])
        elif tb == "head_to_head":
            key.append(0)  # Simplified; would need pairwise comparison
    return tuple(key)


def advance_winner(match):
    """After a match is confirmed, advance winner in knockout bracket."""
    if not match.next_match:
        return

    next_match = match.next_match
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


def get_bracket_data(tournament):
    """Build bracket structure for display."""
    matches = tournament.matches.filter(bracket_type="winners", group="").order_by("round_number", "bracket_position")
    rounds = defaultdict(list)
    for m in matches:
        rounds[m.round_number].append(m)
    return dict(sorted(rounds.items()))


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
        tournament.teams.filter(status="active").update(group="A")
        group_matches = tournament.matches.filter(group="A")

    if not group_matches.exists():
        return False

    incomplete = group_matches.exclude(status__in=["confirmed", "forfeited", "cancelled", "bye"])
    if incomplete.exists():
        return False

    # Group stage complete – generate knockout from top teams
    groups = set(
        tournament.teams.filter(status="active").exclude(group="").values_list("group", flat=True)
    )
    advancing = []
    for group_name in sorted(groups):
        standings = calculate_standings(tournament, group=group_name)
        top = standings[:tournament.teams_per_group_advance]
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
