"""Analytics calculations, independent of HTTP (AI_ANALYTICS_PLAN.md AI-1).

Everything the analytics page computes lives here as plain functions of
model objects and values: no HTTP objects, no rendering. `analytics_view`
parses the query string, picks defaults and renders; the AI layer calls the
same functions for the teams a question names, so both always agree.

The bodies were moved verbatim from `core/views/reporting.py`; query counts
are pinned by the A-13 tests in `core/tests_analytics.py`.
"""
from collections import defaultdict
from datetime import timedelta

from django.db import models as db_models
from django.db.models import Q
from django.utils import timezone

from .models import Team
from .standings import rank_standings
from .views.helpers import (
    _can_manage_tournament,
    _is_user_enrolled_in_tournament,
    _team_display_label,
    _team_display_map,
)

# Rolling-form pills: accessible name and theme-aware CSS class per result.
FORM_RESULT_LABELS = {"W": "Win", "L": "Loss", "D": "Draw"}
FORM_RESULT_CLASSES = {"W": "is-win", "L": "is-loss", "D": "is-draw"}

# The what-if simulator offers at most this many upcoming matches.
SIMULATOR_MATCH_LIMIT = 8

# Schedule density switches from one bar per day to one per week beyond this.
SCHEDULE_DENSITY_DAILY_MAX_SPAN_DAYS = 45

# Formats whose standings table (points, simulator) is meaningful.
STANDINGS_FORMATS = ("round_robin", "double_round_robin", "hybrid")


def can_view_analytics(user, tournament):
    """Return (allowed, can_manage) under A-1's rule.

    Organizers are independent parties: owning *a* tournament does not grant
    reads of another organizer's. Managers and enrolled players only.
    """
    can_manage = _can_manage_tournament(user, tournament)
    allowed = can_manage or _is_user_enrolled_in_tournament(user, tournament)
    return allowed, can_manage


def label_standings(tournament, standings):
    """Set "display_label" on every calculate_standings row; return the label map.

    In individual-registration mode the teams are internal shadows whose
    names must never reach the page (A-3).
    """
    label_map = _team_display_map(tournament, [row["team"].pk for row in standings])
    for row in standings:
        row["display_label"] = label_map.get(row["team"].pk, row["team"].name)
    return label_map


def active_teams(tournament, label_map):
    """Active teams by name, each with `.display_label` set."""
    teams = list(
        Team.objects.filter(
            participations__tournament=tournament,
            participations__status="active",
        ).distinct().order_by("name")
    )
    for team in teams:
        team.display_label = label_map.get(team.pk) or _team_display_label(tournament, team)
    return teams


def match_status_counts(matches):
    return {
        "total": matches.count(),
        "confirmed": matches.filter(status="confirmed").count(),
        "upcoming": matches.filter(status="upcoming").count(),
        "in_progress": matches.filter(status="in_progress").count(),
        "pending": matches.filter(status="pending_confirmation").count(),
        "disputed": matches.filter(status="disputed").count(),
        "forfeited": matches.filter(status="forfeited").count(),
        "cancelled": matches.filter(status="cancelled").count(),
    }


def court_progress(tournament, matches):
    """Per-court totals and the share already played (A-9)."""
    # One grouped query for every court's counts, not two per court.
    court_counts = {
        row["court"]: row
        for row in matches.filter(court__isnull=False).values("court").annotate(
            total=db_models.Count("id"),
            confirmed=db_models.Count("id", filter=Q(status="confirmed")),
        )
    }
    court_stats = []
    for court in tournament.courts.all():
        counts = court_counts.get(court.pk, {})
        total = counts.get("total", 0)
        confirmed = counts.get("confirmed", 0)
        court_stats.append({
            "court": court, "total_matches": total, "confirmed_matches": confirmed,
            # Share of this court's scheduled matches already played.
            "completion_pct": round(confirmed / total * 100, 1) if total > 0 else 0,
        })
    return court_stats


def team_performance(tournament, standings, active):
    """Return (team_stats, show_draws_column) for the active teams (A-2).

    Built from calculate_standings rows so draws and forfeits are counted the
    same way as on the standings page.
    """
    active_ids = {team.pk for team in active}
    team_stats = [
        {
            "team": row["team"],
            "display_label": row["display_label"],
            "played": row["played"], "wins": row["wins"],
            "draws": row["draws"], "losses": row["losses"],
            "win_rate": round(row["wins"] / row["played"] * 100, 1) if row["played"] else 0,
        }
        for row in standings
        if row["team"].pk in active_ids
    ]
    if tournament.format not in STANDINGS_FORMATS:
        # Standings points mean nothing in a bracket; rank on results instead.
        team_stats.sort(key=lambda s: (
            -s["wins"], -s["win_rate"], s["losses"], s["display_label"].lower(),
        ))
    return team_stats, any(s["draws"] for s in team_stats)


def set_points_pct(standings):
    """Points Overview bar widths, relative to the leader (A-11).

    All zero when nobody has points yet, rather than leaning on widthratio's
    divide-by-zero behaviour.
    """
    max_points = max((row["points"] for row in standings), default=0)
    for row in standings:
        row["points_pct"] = (
            min(100, max(0, round(row["points"] / max_points * 100))) if max_points > 0 else 0
        )


def schedule_density(scheduled_times):
    """Return ([[label, count], ...] in date order, "day" | "week") (A-10).

    Daily buckets ("2026-03-02") while the schedule spans at most
    SCHEDULE_DENSITY_DAILY_MAX_SPAN_DAYS; beyond that, Monday-based weeks
    ("Week of Mar 2", with the year added when the span crosses one), so a
    months-long league doesn't render hundreds of bars.
    """
    days = sorted(timezone.localtime(t).date() for t in scheduled_times)
    if not days:
        return [], "day"
    if (days[-1] - days[0]).days <= SCHEDULE_DENSITY_DAILY_MAX_SPAN_DAYS:
        counts = defaultdict(int)
        for day in days:
            counts[day] += 1
        return [[day.isoformat(), n] for day, n in sorted(counts.items())], "day"

    show_year = days[0].year != days[-1].year
    counts = defaultdict(int)
    for day in days:
        counts[day - timedelta(days=day.weekday())] += 1
    buckets = []
    for week_start, n in sorted(counts.items()):
        label = f"Week of {week_start:%b} {week_start.day}"
        if show_year:
            label += f", {week_start.year}"
        buckets.append([label, n])
    return buckets, "week"


def withdrawal_summary(tournament, matches, label_map):
    """Withdrawn teams with their forfeited/cancelled match counts (A-13).

    One query for the participations (with their teams) and one for the
    matches they forfeited or had cancelled, not two per team.
    """
    withdrawn_participations = list(
        tournament.team_participations.filter(status="withdrawn")
        .select_related("team").order_by("team__name")
    )
    withdrawn_ids = {p.team_id for p in withdrawn_participations}
    affected_counts = defaultdict(int)
    if withdrawn_ids:
        for team1_id, team2_id in matches.filter(
            Q(team1_id__in=withdrawn_ids) | Q(team2_id__in=withdrawn_ids),
            status__in=["forfeited", "cancelled"],
        ).values_list("team1_id", "team2_id"):
            for team_id in {team1_id, team2_id} & withdrawn_ids:
                affected_counts[team_id] += 1
    return [
        {
            "team": participation.team,
            "display_label": label_map.get(participation.team_id)
            or _team_display_label(tournament, participation.team),
            "affected_matches": affected_counts[participation.team_id],
            "withdrawn_at": participation.withdrawn_at,
        }
        for participation in withdrawn_participations
    ]


def head_to_head(tournament, team_a, team_b):
    """Meetings between two different teams, or None."""
    if not team_a or not team_b or team_a == team_b:
        return None
    h2h_matches = list(
        tournament.matches.filter(
            (
                Q(team1=team_a) & Q(team2=team_b)
            ) | (
                Q(team1=team_b) & Q(team2=team_a)
            ),
            status__in=["confirmed", "forfeited"],
        ).select_related("winner", "team1", "team2").order_by("-match_number")
    )
    a_wins = 0
    b_wins = 0
    draws = 0
    a_score_total = 0
    b_score_total = 0
    scored_matches = 0
    for m in h2h_matches:
        if m.winner_id == team_a.pk:
            a_wins += 1
        elif m.winner_id == team_b.pk:
            b_wins += 1
        else:
            draws += 1
        if m.score_team1 is not None and m.score_team2 is not None:
            if m.team1_id == team_a.pk:
                a_score_total += m.score_team1
                b_score_total += m.score_team2
            else:
                a_score_total += m.score_team2
                b_score_total += m.score_team1
            scored_matches += 1
    return {
        "total_matches": len(h2h_matches),
        "team1_wins": a_wins,
        "team2_wins": b_wins,
        "draws": draws,
        "team1_avg_score": round(a_score_total / scored_matches, 1) if scored_matches > 0 else None,
        "team2_avg_score": round(b_score_total / scored_matches, 1) if scored_matches > 0 else None,
        "last_match": h2h_matches[0] if h2h_matches else None,
    }


def rolling_form(tournament, team, window):
    """The team's last `window` finished matches, oldest first, with a
    running win rate."""
    if not team:
        return []
    recent = list(
        tournament.matches.filter(
            Q(team1=team) | Q(team2=team),
            status__in=["confirmed", "forfeited"],
        ).select_related("team1", "team2", "winner").order_by("-match_number")[:window]
    )[::-1]
    rows = []
    wins = 0
    for idx, m in enumerate(recent, start=1):
        opponent = m.get_opponent(team)
        if m.winner_id == team.pk:
            result = "W"
            wins += 1
        elif m.winner_id:
            result = "L"
        else:
            result = "D"
        rows.append({
            "match_number": m.match_number,
            "opponent": _team_display_label(tournament, opponent) if opponent else "TBD",
            "result": result,
            "result_label": FORM_RESULT_LABELS[result],
            "result_class": FORM_RESULT_CLASSES[result],
            "sequence": idx,
            "win_rate": round(wins / idx * 100, 1),
        })
    return rows


def next_opponent_prep(tournament, team):
    """The team's next match, its opponent's recent record, and their
    head-to-head; None when there is no upcoming match."""
    if not team:
        return None
    matches = tournament.matches.all()
    prep_match = matches.filter(
        Q(team1=team) | Q(team2=team),
        status__in=["upcoming", "in_progress"],
    ).select_related("team1", "team2", "court").order_by("scheduled_time", "match_number").first()
    if not prep_match:
        return None
    opponent = prep_match.get_opponent(team)
    opponent_recent = []
    opponent_record = {"wins": 0, "losses": 0, "draws": 0}
    h2h_record = {"wins": 0, "losses": 0, "draws": 0}
    if opponent:
        recent_opp_matches = list(
            matches.filter(
                Q(team1=opponent) | Q(team2=opponent),
                status__in=["confirmed", "forfeited"],
            ).select_related("team1", "team2", "winner").order_by("-match_number")[:5]
        )
        for m in recent_opp_matches:
            opp_match_opp = m.get_opponent(opponent)
            if m.winner_id == opponent.pk:
                opp_result = "W"
                opponent_record["wins"] += 1
            elif m.winner_id:
                opp_result = "L"
                opponent_record["losses"] += 1
            else:
                opp_result = "D"
                opponent_record["draws"] += 1
            opponent_recent.append({
                "match_number": m.match_number,
                "opponent": _team_display_label(tournament, opp_match_opp) if opp_match_opp else "TBD",
                "result": opp_result,
            })

        for m in matches.filter(
            (
                Q(team1=team) & Q(team2=opponent)
            ) | (
                Q(team1=opponent) & Q(team2=team)
            ),
            status__in=["confirmed", "forfeited"],
        ):
            if m.winner_id == team.pk:
                h2h_record["wins"] += 1
            elif m.winner_id == opponent.pk:
                h2h_record["losses"] += 1
            else:
                h2h_record["draws"] += 1
    return {
        "team": team,
        "team_label": _team_display_label(tournament, team),
        "match": prep_match,
        "opponent": opponent,
        "opponent_label": _team_display_label(tournament, opponent) if opponent else "",
        "opponent_recent": opponent_recent,
        "opponent_record": opponent_record,
        "h2h": h2h_record,
        "opponent_key_players": list(opponent.players.values_list("name", flat=True)[:3]) if opponent else [],
    }


def simulator_matches(tournament):
    """Return (the upcoming matches the what-if simulator offers, how many
    exist in total); ([], 0) for formats without a standings table."""
    if tournament.format not in STANDINGS_FORMATS:
        return [], 0
    candidates = tournament.matches.filter(
        status="upcoming",
        team1__isnull=False,
        team2__isnull=False,
    )
    if tournament.format == "hybrid":
        # Only group-stage matches earn standings points; knockout matches
        # carry no group letter.
        candidates = candidates.exclude(group="")
    total = candidates.count()
    offered = list(
        candidates.select_related("team1", "team2").order_by(
            "scheduled_time", "match_number"
        )[:SIMULATOR_MATCH_LIMIT]
    )
    return offered, total


def simulate(tournament, standings, offered, picks):
    """Apply `picks` ({match pk: "team1" | "team2" | "draw"}) to a copy of the
    standings and re-rank with the tournament's tiebreakers (A-8).

    Annotates each offered match with team labels, `draw_allowed` and
    `selected_outcome` for the template. Returns (simulated standings or None
    when nothing is offered, whether any valid pick was applied).
    """
    if not offered:
        return None, False
    has_choices = False
    by_team_id = {}
    for row in standings:
        # Copy: the simulator adjusts points and re-ranks, and the real
        # rows are still shown in Points Overview.
        row_copy = dict(row)
        row_copy["point_change"] = 0
        by_team_id[row["team"].pk] = row_copy

    for m in offered:
        m.team1_label = _team_display_label(tournament, m.team1)
        m.team2_label = _team_display_label(tournament, m.team2)
        outcome = picks.get(m.pk)
        # A draw is only possible in a group / round-robin match. Checked
        # here as well as in the dropdown so a forged value is ignored (A-4).
        m.draw_allowed = tournament.format != "hybrid" or bool(m.group)
        allowed = ("team1", "team2", "draw") if m.draw_allowed else ("team1", "team2")
        if outcome not in allowed:
            outcome = None
        m.selected_outcome = outcome or ""
        if outcome is None:
            continue
        has_choices = True
        if m.team1_id not in by_team_id or m.team2_id not in by_team_id:
            continue
        if outcome == "team1":
            by_team_id[m.team1_id]["point_change"] += tournament.points_per_win
            by_team_id[m.team2_id]["point_change"] += tournament.points_per_loss
        elif outcome == "team2":
            by_team_id[m.team2_id]["point_change"] += tournament.points_per_win
            by_team_id[m.team1_id]["point_change"] += tournament.points_per_loss
        else:
            by_team_id[m.team1_id]["point_change"] += tournament.points_per_draw
            by_team_id[m.team2_id]["point_change"] += tournament.points_per_draw

    simulated = list(by_team_id.values())
    for row in simulated:
        row["points"] += row["point_change"]
    # Same tiebreakers as the real table (rank_standings sets "rank").
    return rank_standings(tournament, simulated), has_choices
