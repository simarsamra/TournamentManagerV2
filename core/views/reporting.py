"""Standings, analytics, backups, notifications, search and public pages."""
import os
from datetime import timedelta
from collections import defaultdict

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import models as db_models
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..models import (
    AuditLog,
    BackupRecord,
    Match,
    Notification,
    OrganizerApplication,
    OrganizerProfile,
    Team,
    TeamMembership,
    TeamTournamentParticipation,
    Tournament,
    TournamentIndividualRegistration,
)
from ..standings import (
    calculate_standings,
    get_bracket_data,
    get_grand_final_matches,
    get_losers_bracket_data,
    get_third_place_match,
    rank_standings,
)
from ..backup import (
    create_backup,
    delete_backup,
    list_backups,
    restore_backup,
    validate_backup,
)
from ..audit import log_action

from .helpers import (
    SEARCH_RESULT_LIMIT,
    _can_manage_tournament,
    _expire_no_show_reports,
    _expire_pending_score_disputes,
    _get_tournament,
    _is_htmx_request,
    _is_organizer,
    _is_site_admin,
    _is_user_enrolled_in_tournament,
    _notify,
    _public_tournament_context,
    _render_refreshable_page,
    _safe_page_param,
    _team_display_label,
    _team_display_map,
    _tournament_context,
)



# -- Standings --

@login_required
def standings_view(request):
    tournament = _get_tournament(request)
    if tournament:
        _expire_no_show_reports(tournament)
        _expire_pending_score_disputes(tournament)
    context = {"tournament": tournament}
    if tournament:
        if tournament.format in ("round_robin", "double_round_robin", "hybrid"):
            if tournament.format == "hybrid":
                groups = sorted(set(
                    tournament.team_participations.exclude(group="").values_list("group", flat=True)
                ))
                group_standings = {}
                for g in groups:
                    group_rows = calculate_standings(tournament, group=g)
                    for row in group_rows:
                        row["display_label"] = _team_display_label(tournament, row["team"])
                    group_standings[g] = group_rows
                context["group_standings"] = group_standings
                group_matches = tournament.matches.exclude(group="")
                context["hybrid_group_complete"] = (
                    group_matches.exists()
                    and not group_matches.exclude(status__in=["confirmed", "forfeited", "cancelled", "bye"]).exists()
                )
                ko_matches = tournament.matches.filter(group="", bracket_type="winners")
                if ko_matches.exists():
                    context["bracket"] = get_bracket_data(tournament)
            else:
                standings = calculate_standings(tournament)
                for row in standings:
                    row["display_label"] = _team_display_label(tournament, row["team"])
                context["standings"] = standings
        if tournament.format in ("knockout", "double_elimination", "consolation"):
            context["bracket"] = get_bracket_data(tournament)
        if tournament.format == "double_elimination":
            context["losers_bracket"] = get_losers_bracket_data(tournament)
            context["grand_final_matches"] = get_grand_final_matches(tournament)
        if tournament.format in ("knockout", "double_elimination", "consolation", "hybrid"):
            context["third_place_match"] = get_third_place_match(tournament)

        team_ids = set()
        bracket_sources = [context.get("bracket") or {}, context.get("losers_bracket") or {}]
        for source in bracket_sources:
            for round_matches in source.values():
                for match in round_matches:
                    for tid in (match.team1_id, match.team2_id, match.winner_id):
                        if tid:
                            team_ids.add(tid)
        for match in context.get("grand_final_matches") or []:
            for tid in (match.team1_id, match.team2_id, match.winner_id):
                if tid:
                    team_ids.add(tid)
        tpm = context.get("third_place_match")
        if tpm:
            for tid in [tpm.team1_id, tpm.team2_id, tpm.winner_id]:
                if tid:
                    team_ids.add(tid)
        context["team_name_map"] = _team_display_map(tournament, team_ids)
        context["tournament_champion_label"] = (
            _team_display_label(tournament, tournament.champion) if tournament.champion else ""
        )
    context.update(_tournament_context(request, tournament))
    return _render_refreshable_page(
        request,
        "core/standings.html",
        "core/partials/standings_content.html",
        context,
    )


# -- Analytics --

# Rolling-form pills: accessible name and theme-aware CSS class per result.
_FORM_RESULT_LABELS = {"W": "Win", "L": "Loss", "D": "Draw"}
_FORM_RESULT_CLASSES = {"W": "is-win", "L": "is-loss", "D": "is-draw"}

# The what-if simulator offers at most this many upcoming matches.
SIMULATOR_MATCH_LIMIT = 8

# Schedule density switches from one bar per day to one per week beyond this.
SCHEDULE_DENSITY_DAILY_MAX_SPAN_DAYS = 45

# Query parameters each analytics widget's form owns. The simulator owns
# sim_<match pk>. Every form carries the *other* widgets' current values as
# hidden inputs, so submitting one widget doesn't reset the rest.
ANALYTICS_WIDGET_PARAMS = {
    "h2h": ("h2h_team1", "h2h_team2"),
    "form": ("form_team", "form_window"),
    "prep": ("prep_team",),
    "sim": (),
}


def _analytics_hidden_state(request, tournament, simulator_matches):
    """Return {widget: [(name, value), ...]}: the hidden inputs each widget's
    form needs to carry every other widget's state (plus the tournament).

    Only known parameters are echoed, and sim_* only for matches the
    simulator is actually offering with a valid pick.
    """
    state = [("tournament", str(tournament.pk))]
    for params in ANALYTICS_WIDGET_PARAMS.values():
        for name in params:
            value = request.GET.get(name)
            if value:
                state.append((name, value))
    for match in simulator_matches:
        if match.selected_outcome:
            state.append((f"sim_{match.pk}", match.selected_outcome))

    def owner(name):
        if name.startswith("sim_"):
            return "sim"
        return next(
            (widget for widget, params in ANALYTICS_WIDGET_PARAMS.items() if name in params),
            None,
        )

    return {
        widget: [(name, value) for name, value in state if owner(name) != widget]
        for widget in ANALYTICS_WIDGET_PARAMS
    }


def _schedule_density(scheduled_times):
    """Return ([[label, count], ...] in date order, "day" | "week").

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

@login_required
def analytics_view(request):
    tournament = _get_tournament(request)
    if not tournament:
        return render(request, "core/analytics.html", _tournament_context(request, tournament))
    # Organizers are independent parties: owning *a* tournament does not grant
    # reads of another organizer's. Managers and enrolled players only.
    can_manage = _can_manage_tournament(request.user, tournament)
    if not can_manage and not _is_user_enrolled_in_tournament(request.user, tournament):
        messages.error(request, "You do not have access to that tournament.")
        return redirect("dashboard")
    _expire_pending_score_disputes(tournament)
    matches = tournament.matches.all()
    teams = Team.objects.filter(participations__tournament=tournament).distinct()
    match_stats = {
        "total": matches.count(),
        "confirmed": matches.filter(status="confirmed").count(),
        "upcoming": matches.filter(status="upcoming").count(),
        "in_progress": matches.filter(status="in_progress").count(),
        "pending": matches.filter(status="pending_confirmation").count(),
        "disputed": matches.filter(status="disputed").count(),
        "forfeited": matches.filter(status="forfeited").count(),
        "cancelled": matches.filter(status="cancelled").count(),
    }
    courts = tournament.courts.all()
    court_stats = []
    for court in courts:
        total = matches.filter(court=court).count()
        confirmed = matches.filter(court=court, status="confirmed").count()
        court_stats.append({
            "court": court, "total_matches": total, "confirmed_matches": confirmed,
            # Share of this court's scheduled matches already played.
            "completion_pct": round(confirmed / total * 100, 1) if total > 0 else 0,
        })
    # Built from calculate_standings so draws and forfeits are counted the same
    # way as on the standings page (this used to set losses = played - wins).
    standings = calculate_standings(tournament)
    # Every row needs a display label: in individual-registration mode the
    # teams are internal shadows whose names must never reach the page.
    label_map = _team_display_map(tournament, [row["team"].pk for row in standings])
    for row in standings:
        row["display_label"] = label_map.get(row["team"].pk, row["team"].name)
    active_teams = list(
        teams.filter(
            participations__tournament=tournament,
            participations__status="active",
        ).distinct().order_by("name")
    )
    for team in active_teams:
        team.display_label = label_map.get(team.pk) or _team_display_label(tournament, team)
    active_ids = {team.pk for team in active_teams}
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
    if tournament.format not in ("round_robin", "double_round_robin", "hybrid"):
        # Standings points mean nothing in a bracket; rank on results instead.
        team_stats.sort(key=lambda s: (
            -s["wins"], -s["win_rate"], s["losses"], s["display_label"].lower(),
        ))
    show_draws_column = any(s["draws"] for s in team_stats)
    # Points Overview bar widths, relative to the leader; all zero when nobody
    # has points yet (rather than leaning on widthratio's divide-by-zero).
    max_points = max((row["points"] for row in standings), default=0)
    for row in standings:
        row["points_pct"] = (
            min(100, max(0, round(row["points"] / max_points * 100))) if max_points > 0 else 0
        )
    schedule_density, schedule_density_unit = _schedule_density(
        matches.filter(scheduled_time__isnull=False).values_list("scheduled_time", flat=True)
    )
    withdrawn = teams.filter(
        participations__tournament=tournament,
        participations__status="withdrawn",
    ).distinct()
    withdrawal_info = []
    for team in withdrawn:
        affected = matches.filter(Q(team1=team) | Q(team2=team), status__in=["forfeited", "cancelled"]).count()
        participation = team.participations.filter(tournament=tournament).first()
        withdrawal_info.append({
            "team": team,
            "display_label": _team_display_label(tournament, team),
            "affected_matches": affected,
            "withdrawn_at": participation.withdrawn_at if participation else None,
        })
    recent_logs = (
        AuditLog.objects.filter(tournament=tournament).order_by("-timestamp")[:20]
        if can_manage
        else AuditLog.objects.none()
    )
    context = {
        "tournament": tournament, "can_manage": can_manage,
        "match_stats": match_stats, "court_stats": court_stats,
        "team_stats": team_stats, "show_draws_column": show_draws_column,
        "schedule_density": schedule_density,
        "schedule_density_unit": schedule_density_unit,
        "withdrawal_info": withdrawal_info, "recent_logs": recent_logs,
    }
    if tournament.format in ("round_robin", "double_round_robin", "hybrid"):
        context["standings"] = standings


    # --- Head-to-head matchup card ---
    h2h_team1 = None
    h2h_team2 = None
    h2h_card = None
    h2h_team1_id = request.GET.get("h2h_team1")
    h2h_team2_id = request.GET.get("h2h_team2")
    if h2h_team1_id:
        h2h_team1 = next((t for t in active_teams if str(t.pk) == str(h2h_team1_id)), None)
    if h2h_team2_id:
        h2h_team2 = next((t for t in active_teams if str(t.pk) == str(h2h_team2_id)), None)
    if not h2h_team1 and active_teams:
        h2h_team1 = active_teams[0]
    if not h2h_team2 and len(active_teams) > 1:
        h2h_team2 = active_teams[1]
    if h2h_team1 and h2h_team2 and h2h_team1 != h2h_team2:
        h2h_matches = list(
            matches.filter(
                (
                    Q(team1=h2h_team1) & Q(team2=h2h_team2)
                ) | (
                    Q(team1=h2h_team2) & Q(team2=h2h_team1)
                ),
                status__in=["confirmed", "forfeited"],
            ).select_related("winner", "team1", "team2").order_by("-match_number")
        )
        h2h_t1_wins = 0
        h2h_t2_wins = 0
        h2h_draws = 0
        h2h_t1_score_total = 0
        h2h_t2_score_total = 0
        h2h_scored_matches = 0
        for m in h2h_matches:
            if m.winner_id == h2h_team1.pk:
                h2h_t1_wins += 1
            elif m.winner_id == h2h_team2.pk:
                h2h_t2_wins += 1
            else:
                h2h_draws += 1
            if m.score_team1 is not None and m.score_team2 is not None:
                if m.team1_id == h2h_team1.pk:
                    h2h_t1_score_total += m.score_team1
                    h2h_t2_score_total += m.score_team2
                else:
                    h2h_t1_score_total += m.score_team2
                    h2h_t2_score_total += m.score_team1
                h2h_scored_matches += 1
        h2h_card = {
            "total_matches": len(h2h_matches),
            "team1_wins": h2h_t1_wins,
            "team2_wins": h2h_t2_wins,
            "draws": h2h_draws,
            "team1_avg_score": round(h2h_t1_score_total / h2h_scored_matches, 1) if h2h_scored_matches > 0 else None,
            "team2_avg_score": round(h2h_t2_score_total / h2h_scored_matches, 1) if h2h_scored_matches > 0 else None,
            "last_match": h2h_matches[0] if h2h_matches else None,
        }

    h2h_team1_label = _team_display_label(tournament, h2h_team1) if h2h_team1 else ""
    h2h_team2_label = _team_display_label(tournament, h2h_team2) if h2h_team2 else ""

    # --- Rolling form trend ---
    form_team = None
    form_team_id = request.GET.get("form_team")
    if form_team_id:
        form_team = next((t for t in active_teams if str(t.pk) == str(form_team_id)), None)
    if not form_team and active_teams:
        form_team = active_teams[0]
    try:
        form_window = int(request.GET.get("form_window", 5))
    except (TypeError, ValueError):
        form_window = 5
    form_window = max(3, min(form_window, 15))
    rolling_form_rows = []
    if form_team:
        recent_form_matches = list(
            matches.filter(
                Q(team1=form_team) | Q(team2=form_team),
                status__in=["confirmed", "forfeited"],
            ).select_related("team1", "team2", "winner").order_by("-match_number")[:form_window]
        )[::-1]
        wins = 0
        for idx, m in enumerate(recent_form_matches, start=1):
            opponent = m.get_opponent(form_team)
            if m.winner_id == form_team.pk:
                result = "W"
                wins += 1
            elif m.winner_id:
                result = "L"
            else:
                result = "D"
            rolling_form_rows.append({
                "match_number": m.match_number,
                "opponent": _team_display_label(tournament, opponent) if opponent else "TBD",
                "result": result,
                "result_label": _FORM_RESULT_LABELS[result],
                "result_class": _FORM_RESULT_CLASSES[result],
                "sequence": idx,
                "win_rate": round(wins / idx * 100, 1),
            })

    # --- Next-opponent prep sheet ---
    prep_team = None
    prep_team_id = request.GET.get("prep_team")
    if prep_team_id:
        prep_team = next((t for t in active_teams if str(t.pk) == str(prep_team_id)), None)
    if not prep_team:
        prep_team = form_team
    next_opponent_prep = None
    if prep_team:
        prep_match = matches.filter(
            Q(team1=prep_team) | Q(team2=prep_team),
            status__in=["upcoming", "in_progress"],
        ).select_related("team1", "team2", "court").order_by("scheduled_time", "match_number").first()
        if prep_match:
            opponent = prep_match.get_opponent(prep_team)
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
                        Q(team1=prep_team) & Q(team2=opponent)
                    ) | (
                        Q(team1=opponent) & Q(team2=prep_team)
                    ),
                    status__in=["confirmed", "forfeited"],
                ):
                    if m.winner_id == prep_team.pk:
                        h2h_record["wins"] += 1
                    elif m.winner_id == opponent.pk:
                        h2h_record["losses"] += 1
                    else:
                        h2h_record["draws"] += 1
            next_opponent_prep = {
                "team": prep_team,
                "team_label": _team_display_label(tournament, prep_team),
                "match": prep_match,
                "opponent": opponent,
                "opponent_label": _team_display_label(tournament, opponent) if opponent else "",
                "opponent_recent": opponent_recent,
                "opponent_record": opponent_record,
                "h2h": h2h_record,
                "opponent_key_players": list(opponent.players.values_list("name", flat=True)[:3]) if opponent else [],
            }

    # --- What-if standings simulator ---
    simulator_matches = []
    simulator_enabled = tournament.format in ("round_robin", "double_round_robin", "hybrid")
    simulated_standings = None
    simulator_has_choices = False
    simulator_total = 0
    if simulator_enabled:
        candidates = matches.filter(
            status="upcoming",
            team1__isnull=False,
            team2__isnull=False,
        )
        if tournament.format == "hybrid":
            # Only group-stage matches earn standings points; knockout matches
            # carry no group letter.
            candidates = candidates.exclude(group="")
        simulator_total = candidates.count()
        simulator_matches = list(
            candidates.select_related("team1", "team2").order_by(
                "scheduled_time", "match_number"
            )[:SIMULATOR_MATCH_LIMIT]
        )
        if simulator_matches:
            by_team_id = {}
            for row in standings:
                # Copy: the simulator adjusts points and re-ranks, and the real
                # rows are still shown in Points Overview.
                row_copy = dict(row)
                row_copy["point_change"] = 0
                by_team_id[row["team"].pk] = row_copy

            for m in simulator_matches:
                m.team1_label = _team_display_label(tournament, m.team1)
                m.team2_label = _team_display_label(tournament, m.team2)
                outcome = request.GET.get(f"sim_{m.pk}")
                # A draw is only possible in a group / round-robin match. Checked
                # here as well as in the dropdown so a forged value is ignored.
                m.draw_allowed = tournament.format != "hybrid" or bool(m.group)
                allowed = ("team1", "team2", "draw") if m.draw_allowed else ("team1", "team2")
                if outcome not in allowed:
                    outcome = None
                m.selected_outcome = outcome or ""
                if outcome is None:
                    continue
                simulator_has_choices = True
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

            simulated_standings = list(by_team_id.values())
            for row in simulated_standings:
                row["points"] += row["point_change"]
            # Same tiebreakers as the real table (rank_standings sets "rank").
            simulated_standings = rank_standings(tournament, simulated_standings)

    context.update({
        "analytics_teams": active_teams,
        "h2h_team1": h2h_team1,
        "h2h_team2": h2h_team2,
        "h2h_team1_label": h2h_team1_label,
        "h2h_team2_label": h2h_team2_label,
        "h2h_card": h2h_card,
        "form_team": form_team,
        "form_window": form_window,
        "rolling_form_rows": rolling_form_rows,
        "prep_team": prep_team,
        "next_opponent_prep": next_opponent_prep,
        "simulator_enabled": simulator_enabled,
        "simulator_matches": simulator_matches,
        "simulator_total": simulator_total,
        "simulator_limit": SIMULATOR_MATCH_LIMIT,
        "simulated_standings": simulated_standings,
        "simulator_has_choices": simulator_has_choices,
        "analytics_hidden": _analytics_hidden_state(request, tournament, simulator_matches),
    })
    context.update(_tournament_context(request, tournament))
    return render(request, "core/analytics.html", context)


# -- Audit Log --

@login_required
def audit_log_view(request):
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can view the audit log.")
        return redirect("dashboard")
    tournament = _get_tournament(request)
    if tournament and not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")
    logs = AuditLog.objects.select_related("user")
    # Rows with no tournament (logins, registrations, account management) are
    # site-wide events about other people's accounts: site admins only.
    if _is_site_admin(request.user):
        if tournament:
            logs = logs.filter(Q(tournament=tournament) | Q(tournament__isnull=True))
    elif tournament:
        logs = logs.filter(tournament=tournament)
    else:
        logs = logs.none()
    visible_logs = logs
    action_filter = request.GET.get("action", "")
    if action_filter:
        logs = logs.filter(action=action_filter)
    page = _safe_page_param(request)
    per_page = 50
    total = logs.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    logs = logs[(page - 1) * per_page : page * per_page]
    actions = visible_logs.order_by("action").values_list("action", flat=True).distinct()
    return render(request, "core/audit_log.html", {
        "logs": logs, "actions": actions, "action_filter": action_filter,
        "page": page, "total_pages": total_pages, "page_range": range(1, total_pages + 1),
        **_tournament_context(request, tournament),
    })


# -- Backup & Restore --

@login_required
def backup_view(request):
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can manage backups.")
        return redirect("dashboard")
    tournament = _get_tournament(request)
    return render(request, "core/backup.html", {
        "backups": list_backups(), "records": BackupRecord.objects.all()[:20],
        **_tournament_context(request, tournament),
    })


@login_required
@require_POST
def create_backup_view(request):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    notes = request.POST.get("notes", "")
    record = create_backup(user=request.user, notes=notes)
    log_action(request, "backup_created", f"Backup created: {record.filename}", tournament=_get_tournament(request))
    messages.success(request, f"Backup created: {record.filename}")
    return redirect("backup")


@login_required
@require_POST
def restore_backup_view(request):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    filename = request.POST.get("filename", "")
    backup_dir = settings.BACKUP_DIR.resolve()
    filepath = (backup_dir / filename).resolve()
    # Guard against path traversal
    if not str(filepath).startswith(str(backup_dir) + os.sep):
        messages.error(request, "Invalid backup file.")
        return redirect("backup")
    if not filepath.exists() or filepath.suffix != ".json":
        messages.error(request, "Invalid backup file.")
        return redirect("backup")
    valid, msg = validate_backup(filepath)
    if not valid:
        messages.error(request, f"Backup validation failed: {msg}")
        return redirect("backup")
    create_backup(user=request.user, is_auto=True, notes="Auto-backup before restore")
    restore_backup(filepath)
    log_action(request, "backup_restored", f"Restored from: {filename}")
    messages.success(request, f"Data restored from {filename}")
    return redirect("dashboard")


@login_required
@require_POST
def delete_backup_view(request):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    filename = request.POST.get("filename", "")
    if delete_backup(filename):
        log_action(request, "backup_deleted", f"Backup deleted: {filename}")
        messages.success(request, f"Backup deleted: {filename}")
    else:
        messages.error(request, "Backup not found.")
    return redirect("backup")


# -- Public Views --

def public_home(request):
    tournament = _get_tournament(request)
    context = {
        "tournament": tournament,
        "standings_snapshot": [],
        "upcoming_matches": [],
        **_public_tournament_context(tournament),
    }

    if tournament:
        _expire_pending_score_disputes(tournament)
        if tournament.format in ("round_robin", "double_round_robin"):
            standings_snapshot = calculate_standings(tournament)[:5]
            for row in standings_snapshot:
                row["display_label"] = _team_display_label(tournament, row["team"])
            context["standings_snapshot"] = standings_snapshot
        matches = tournament.matches.select_related("team1", "team2", "court", "winner").order_by(
            "scheduled_time", "match_number"
        )
        upcoming_matches = list(matches.filter(status__in=["upcoming", "in_progress"])[:8])
        for match in upcoming_matches:
            match.team1_label = _team_display_label(tournament, match.team1)
            match.team2_label = _team_display_label(tournament, match.team2)
        context["upcoming_matches"] = upcoming_matches

    return render(request, "core/public_home.html", context)

def public_standings(request):
    tournament = _get_tournament(request)
    if not tournament:
        return render(request, "core/public_standings.html", _public_tournament_context())
    _expire_pending_score_disputes(tournament)
    context = {"tournament": tournament, **_public_tournament_context(tournament)}
    if tournament.format in ("round_robin", "double_round_robin", "hybrid"):
        if tournament.format == "hybrid":
            groups = sorted(set(tournament.team_participations.exclude(group="").values_list("group", flat=True)))
            group_standings = {g: calculate_standings(tournament, group=g) for g in groups}
            for rows in group_standings.values():
                for row in rows:
                    row["display_label"] = _team_display_label(tournament, row["team"])
            context["group_standings"] = group_standings
            ko_matches = tournament.matches.filter(group="", bracket_type="winners")
            if ko_matches.exists():
                context["bracket"] = get_bracket_data(tournament)
        else:
            standings = calculate_standings(tournament)
            for row in standings:
                row["display_label"] = _team_display_label(tournament, row["team"])
            context["standings"] = standings
    if tournament.format in ("knockout", "double_elimination", "consolation"):
        context["bracket"] = get_bracket_data(tournament)
    if tournament.format == "double_elimination":
        context["losers_bracket"] = get_losers_bracket_data(tournament)
        context["grand_final_matches"] = get_grand_final_matches(tournament)
    if tournament.format in ("knockout", "double_elimination", "consolation", "hybrid"):
        context["third_place_match"] = get_third_place_match(tournament)

    team_ids = set()
    for source in (context.get("bracket") or {}, context.get("losers_bracket") or {}):
        for round_matches in source.values():
            for match in round_matches:
                for tid in (match.team1_id, match.team2_id, match.winner_id):
                    if tid:
                        team_ids.add(tid)
    for match in context.get("grand_final_matches") or []:
        for tid in (match.team1_id, match.team2_id, match.winner_id):
            if tid:
                team_ids.add(tid)
    tpm = context.get("third_place_match")
    if tpm:
        for tid in [tpm.team1_id, tpm.team2_id, tpm.winner_id]:
            if tid:
                team_ids.add(tid)
    context["team_name_map"] = _team_display_map(tournament, team_ids)
    context["tournament_champion_label"] = (
        _team_display_label(tournament, tournament.champion) if tournament.champion else ""
    )
    return render(request, "core/public_standings.html", context)


def public_fixtures(request):
    tournament = _get_tournament(request)
    if not tournament:
        return render(request, "core/public_fixtures.html", {"matches": [], **_public_tournament_context()})
    _expire_pending_score_disputes(tournament)
    matches = tournament.matches.select_related("team1", "team2", "court", "winner").order_by("scheduled_time", "match_number")
    team_ids = {
        m.team1_id for m in matches if m.team1_id
    } | {
        m.team2_id for m in matches if m.team2_id
    } | {
        m.winner_id for m in matches if m.winner_id
    }
    team_name_map = _team_display_map(tournament, team_ids)
    return render(request, "core/public_fixtures.html", {
        "tournament": tournament,
        "matches": matches,
        "team_name_map": team_name_map,
        **_public_tournament_context(tournament),
    })


# =============================================================================
# SECTION 8 — NOTIFICATION VIEWS
# =============================================================================

@login_required
def notifications_view(request):
    """List all notifications for the current user (8.1, 8.3)."""
    notifications = Notification.objects.filter(user=request.user).order_by("-created_at")[:100]
    unread_count = Notification.objects.filter(user=request.user, is_read=False).count()
    tournament = _get_tournament(request)
    context = {
        "notifications": notifications,
        "unread_count": unread_count,
        **_tournament_context(request, tournament),
    }
    return _render_refreshable_page(
        request,
        "core/notifications.html",
        "core/partials/notifications_content.html",
        context,
    )


@login_required
@require_POST
def mark_notifications_read(request):
    """Mark all notifications as read (8.3)."""
    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    if _is_htmx_request(request):
        return notifications_view(request)
    return redirect("notifications")


@login_required
@require_POST
def mark_notification_read(request, pk):
    """Mark a single notification as read and redirect to its link (8.3)."""
    notif = get_object_or_404(Notification, pk=pk, user=request.user)
    notif.is_read = True
    notif.save(update_fields=["is_read"])
    if notif.link:
        if _is_htmx_request(request):
            return HttpResponse(status=204, headers={"HX-Redirect": notif.link})
        return redirect(notif.link)
    if _is_htmx_request(request):
        return notifications_view(request)
    return redirect("notifications")


# =============================================================================
# SECTION 1.5 — USER PUBLIC PROFILE
# =============================================================================

def user_public_profile(request, username):
    """Public profile page for any user (1.5)."""
    profile_user = get_object_or_404(User, username=username)
    # Gather teams the user is/was part of (non-internal)
    memberships = (
        TeamMembership.objects.filter(user=profile_user, team__is_internal=False)
        .select_related("team")
        .order_by("team__name")
    )
    # Tournament history via team participations
    participations = (
        TeamTournamentParticipation.objects.filter(
            team__memberships__user=profile_user,
            team__is_internal=False,
        )
        .select_related("team", "tournament")
        .order_by("-tournament__created_at")
        .distinct()
    )
    # Individual registrations
    individual_regs = (
        TournamentIndividualRegistration.objects.filter(user=profile_user)
        .select_related("tournament")
        .order_by("-tournament__created_at")
    )
    # Win / loss counts from confirmed matches
    # Count only matches played after this user joined each team — otherwise a
    # newcomer inherits the team's entire history.
    wins = 0
    losses = 0
    for membership in memberships:
        since = membership.joined_at
        played = (
            Match.objects.filter(status="confirmed", winner__isnull=False)
            .filter(Q(team1_id=membership.team_id) | Q(team2_id=membership.team_id))
            .filter(
                Q(scheduled_time__gte=since)
                | Q(scheduled_time__isnull=True, created_at__gte=since)
            )
        )
        wins += played.filter(winner_id=membership.team_id).count()
        losses += played.exclude(winner_id=membership.team_id).count()
    tournament = _get_tournament(request) if request.user.is_authenticated else None
    ctx = {
        "profile_user": profile_user,
        "memberships": memberships,
        "participations": participations,
        "individual_regs": individual_regs,
        "wins": wins,
        "losses": losses,
    }
    if request.user.is_authenticated:
        ctx.update(_tournament_context(request, tournament))
    return render(request, "core/user_public_profile.html", ctx)


# =============================================================================
# FLOW 1 — Search users (9.2)
# =============================================================================

@login_required
def user_search_view(request):
    """Search users by username, email, or first name (9.2)."""
    q = request.GET.get("q", "").strip()
    results = []
    if q:
        from django.db.models import Count, Prefetch
        qs = User.objects.filter(
            Q(username__icontains=q) | Q(email__icontains=q) | Q(first_name__icontains=q),
            is_superuser=False,
            is_active=True,
        ).annotate(
            participation_count=Count("memberships__team__participations", distinct=True),
        ).prefetch_related(
            Prefetch("memberships", queryset=TeamMembership.objects.order_by("joined_at").select_related("team"))
        )[:SEARCH_RESULT_LIMIT]
        for u in qs:
            # Get first team from prefetched memberships (ordering done at DB level)
            first_membership = next(iter(u.memberships.all()), None)
            team = first_membership.team if first_membership else None
            results.append({
                "username": u.username,
                "display_name": u.get_full_name() or u.username,
                "team_name": team.name if team else None,
                "participation_count": u.participation_count,
            })

    want_json = (
        request.headers.get("Accept") == "application/json"
        or request.GET.get("format") == "json"
    )
    if want_json:
        return JsonResponse(results, safe=False)

    tournament = _get_tournament(request)
    return render(request, "core/user_search.html", {
        "results": results,
        "q": q,
        **_tournament_context(request, tournament),
    })


# =============================================================================
# FLOW 2 — Search teams (9.3)
# =============================================================================

@login_required
def team_search_view(request):
    """Search teams by name (9.3)."""
    q = request.GET.get("q", "").strip()
    results = []
    if q:
        from django.db.models import Count
        qs = (
            Team.objects.filter(name__icontains=q, status="active")
            .annotate(
                member_count=Count("memberships", distinct=True),
                active_tournament_count=Count(
                    "participations",
                    filter=db_models.Q(participations__status="active"),
                    distinct=True,
                ),
            )[:SEARCH_RESULT_LIMIT]
        )
        for t in qs:
            results.append({
                "pk": t.pk,
                "name": t.name,
                "member_count": t.member_count,
                "active_tournament_count": t.active_tournament_count,
            })

    want_json = (
        request.headers.get("Accept") == "application/json"
        or request.GET.get("format") == "json"
    )
    if want_json:
        return JsonResponse(results, safe=False)

    tournament = _get_tournament(request)
    return render(request, "core/team_search.html", {
        "results": results,
        "q": q,
        **_tournament_context(request, tournament),
    })


# =============================================================================
# FLOW 3 — Organizer public page (9.4)
# =============================================================================

def organizer_public_page(request, pk):
    """Public profile page for a verified organizer (9.4)."""
    from ..models import AuditLog as _AuditLog
    organizer = get_object_or_404(User, pk=pk)
    try:
        profile = organizer.organizer_profile
        if not profile.verified and not organizer.is_staff:
                raise Http404
    except OrganizerProfile.DoesNotExist:
        if not organizer.is_staff:
                raise Http404
        profile = None

    # Tournament.created_by is the authoritative record of authorship. Fall back
    # to the audit log only for legacy rows the backfill could not attribute.
    tournaments = Tournament.objects.filter(created_by=organizer)
    if not tournaments.exists():
        legacy_pks = _AuditLog.objects.filter(
            user=organizer, action="tournament_created"
        ).values_list("tournament_id", flat=True).distinct()
        tournaments = Tournament.objects.filter(
            pk__in=[pk for pk in legacy_pks if pk is not None], created_by__isnull=True
        )
    tournaments = tournaments.order_by("-created_at")

    return render(request, "core/organizer_public_page.html", {
        "organizer": organizer,
        "profile": profile,
        "tournaments": tournaments,
    })


# =============================================================================
# SECTION 4.1–4.2 — PUBLIC TOURNAMENT LIST & DETAIL
# =============================================================================

def tournament_list_view(request):
    """Public tournament browse page (4.1)."""
    qs = Tournament.objects.exclude(status="cancelled")

    # Filters
    mode_filter = request.GET.get("mode", "")
    status_filter = request.GET.get("status", "")
    sport_filter = request.GET.get("sport", "")
    search_q = request.GET.get("q", "").strip()

    if mode_filter:
        qs = qs.filter(registration_mode=mode_filter)
    if status_filter:
        qs = qs.filter(status=status_filter)
    if sport_filter:
        qs = qs.filter(sport_type=sport_filter)
    if search_q:
        qs = qs.filter(name__icontains=search_q)

    qs = qs.order_by("start_date", "-created_at")

    tournament = _get_tournament(request) if request.user.is_authenticated else None
    ctx = {
        "tournaments": qs,
        "mode_filter": mode_filter,
        "status_filter": status_filter,
        "sport_filter": sport_filter,
        "search_q": search_q,
        "sport_choices": Tournament.SPORT_CHOICES,
        "status_choices": [
            ("registration_open", "Open Registration"),
            ("active", "In Progress"),
            ("completed", "Completed"),
            ("setup", "Coming Soon"),
        ],
    }
    if request.user.is_authenticated:
        ctx.update(_tournament_context(request, tournament))
    return render(request, "core/tournament_list.html", ctx)


def tournament_public_detail(request, pk):
    """Public tournament detail page (4.2)."""
    tournament = get_object_or_404(Tournament, pk=pk)

    # Participants
    if tournament.registration_mode == "individual":
        participants = list(
            TournamentIndividualRegistration.objects.filter(tournament=tournament, status="active")
            .select_related("user")
            .order_by("display_name")
        )
    else:
        participants = list(
            TeamTournamentParticipation.objects.filter(tournament=tournament, status="active", team__is_internal=False)
            .select_related("team")
            .order_by("team__name")
        )

    # Bracket / standings for completed / active
    matches = list(
        tournament.matches.filter(team1__isnull=False, team2__isnull=False)
        .select_related("team1", "team2", "court")
        .order_by("round_number", "match_number")
    )
    for m in matches:
        m.team1_label = _team_display_label(tournament, m.team1)
        m.team2_label = _team_display_label(tournament, m.team2)

    is_registered = False
    if request.user.is_authenticated:
        is_registered = _is_user_enrolled_in_tournament(request.user, tournament)

    ctx = {
        "tournament": tournament,
        "participants": participants,
        "matches": matches,
        "is_registered": is_registered,
        "participant_count": len(participants),
    }
    if request.user.is_authenticated:
        ctx.update(_tournament_context(request, tournament))
    return render(request, "core/tournament_public_detail.html", ctx)


# =============================================================================
# SECTION 1.6 — ORGANIZER APPLICATION
# =============================================================================

@login_required
def organizer_apply_view(request):
    """Apply for an organizer account (1.6)."""
    # Already an organizer
    if _is_organizer(request.user):
        messages.info(request, "You are already an approved organizer.")
        return redirect("dashboard")

    # Already applied
    existing = OrganizerApplication.objects.filter(user=request.user).first()
    tournament = _get_tournament(request)

    if request.method == "POST":
        if existing and existing.status == "pending":
            messages.warning(request, "Your application is already pending review.")
            return redirect("organizer_apply")

        org_name = request.POST.get("org_name", "").strip()
        description = request.POST.get("description", "").strip()

        if not org_name or not description:
            messages.error(request, "Please fill in all fields.")
        else:
            if existing:
                existing.org_name = org_name
                existing.description = description
                existing.status = "pending"
                existing.save()
            else:
                OrganizerApplication.objects.create(
                    user=request.user,
                    org_name=org_name,
                    description=description,
                )
            # Notify all existing admins/organizers
            admin_users = User.objects.filter(is_superuser=True)
            _notify(
                admin_users,
                "organizer_application_result",
                f"{request.user.username} has applied for an organizer account.",
                link="/settings/",
            )
            log_action(request, "organizer_applied", f"User '{request.user.username}' applied for organizer status")
            messages.success(request, "Your application has been submitted. An admin will review it soon.")
            return redirect("dashboard")
    return render(request, "core/organizer_apply.html", {
        "existing": existing,
        **_tournament_context(request, tournament),
    })


@login_required
@require_POST
def review_organizer_application(request, pk):
    """Admin action to approve or reject an organizer application (1.7)."""
    if not _is_site_admin(request.user):
        messages.error(
            request, "Only site administrators can review organizer applications."
        )
        return redirect("settings")

    application = get_object_or_404(OrganizerApplication, pk=pk)
    action = request.POST.get("action", "")

    if action == "approve":
        application.status = "approved"
        application.reviewed_by = request.user
        application.reviewed_at = timezone.now()
        application.save()
        # Create or update OrganizerProfile
        profile, _ = OrganizerProfile.objects.get_or_create(user=application.user)
        profile.org_name = application.org_name
        profile.verified = True
        profile.save()
        _notify(
            application.user,
            "organizer_application_result",
            "Your organizer application has been approved! You can now create and manage tournaments.",
            link="/dashboard/",
        )
        log_action(request, "organizer_approved", f"Organizer application approved for '{application.user.username}'")
        messages.success(request, f"Application for '{application.user.username}' approved.")
    elif action == "reject":
        application.status = "rejected"
        application.reviewed_by = request.user
        application.reviewed_at = timezone.now()
        application.save()
        _notify(
            application.user,
            "organizer_application_result",
            "Your organizer application has been rejected. Please contact an admin for more information.",
            link="/organizer/apply",
        )
        log_action(request, "organizer_rejected", f"Organizer application rejected for '{application.user.username}'")
        messages.success(request, f"Application for '{application.user.username}' rejected.")
    else:
        messages.error(request, "Invalid action.")

    if _is_htmx_request(request):
        return HttpResponse(status=204, headers={"HX-Redirect": reverse("settings")})
    return redirect("settings")


# =============================================================================
# SECTION 8.4 — ORGANIZER ANNOUNCEMENT
# =============================================================================

@login_required
def organizer_announce_view(request, pk):
    """Organizer sends a message to all approved participants (8.4)."""
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can make announcements.")
        return redirect("dashboard")

    tournament = get_object_or_404(Tournament, pk=pk)
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")

    if request.method == "POST":
        message_text = request.POST.get("message", "").strip()
        if not message_text:
            messages.error(request, "Please enter an announcement message.")
        else:
            # Collect enrolled users
            if tournament.registration_mode == "individual":
                enrolled_users = list(
                    User.objects.filter(
                        individual_registrations__tournament=tournament,
                        individual_registrations__status="active",
                    ).distinct()
                )
            else:
                enrolled_users = list(
                    User.objects.filter(
                        memberships__team__participations__tournament=tournament,
                        memberships__team__is_internal=False,
                    ).distinct()
                )

            if enrolled_users:
                _notify(
                    enrolled_users,
                    "organizer_announcement",
                    f"[{tournament.name}] {message_text}",
                    link=f"/tournaments/{tournament.pk}/",
                    tournament=tournament,
                )

            log_action(
                request, "organizer_announcement",
                f"Announcement sent for '{tournament.name}': {message_text[:100]}",
                tournament=tournament,
            )
            messages.success(request, f"Announcement sent to {len(enrolled_users)} participant(s).")
            return redirect("organizer_announce", pk=pk)

    return render(request, "core/organizer_announce.html", {
        "tournament": tournament,
        **_tournament_context(request, tournament),
    })
