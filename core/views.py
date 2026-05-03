"""Core views for tournament management."""
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timedelta

from django.core.cache import cache as django_cache
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.db import models as db_models
from django.db.models import Q, Count, Avg, F
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import (
    Tournament, Court, TimeSlot, Team, Match,
    RescheduleRequest, NoShowReport, OpenSlot, AuditLog, BackupRecord, Player, CourtAvailability,
    TeamMembership,
)
from .forms import (
    TournamentForm, CourtForm, TimeSlotForm, TeamRegistrationForm,
    AccountRegistrationForm, CreateTeamForm,
    ScoreSubmitForm, RescheduleForm, TeamPreferencesForm, BulkTeamForm,
    BulkTeamFileForm, CourtAvailabilityForm, TeamMemberInviteForm,
)
from .scheduling import (
    generate_fixtures,
    generate_consolation_if_ready,
    estimate_required_matches,
    count_available_slots,
)
from .standings import calculate_standings, advance_winner, get_bracket_data, check_group_stage_complete, _determine_champion
from .withdrawals import handle_withdrawal
from .backup import create_backup, validate_backup, restore_backup, list_backups, delete_backup
from .audit import log_action

DEFAULT_DISPUTE_WINDOW_HOURS = 24
CRITICAL_STAGE_DISPUTE_WINDOW_HOURS = 12
CRITICAL_STAGE_MATCHES_THRESHOLD = 2


def _get_available_tournaments():
    return Tournament.objects.annotate(
        team_count=Count("teams", distinct=True),
        match_count=Count("matches", distinct=True),
    ).annotate(
        status_rank=db_models.Case(
            db_models.When(status="active", then=db_models.Value(0)),
            db_models.When(status="registration_open", then=db_models.Value(1)),
            db_models.When(status="ready", then=db_models.Value(2)),
            db_models.When(status="scheduled", then=db_models.Value(3)),
            db_models.When(status="setup", then=db_models.Value(4)),
            db_models.When(status="completed", then=db_models.Value(5)),
            default=db_models.Value(6),
            output_field=db_models.IntegerField(),
        )
    ).order_by("status_rank", "-created_at")


def _get_tournament(request=None):
    tournaments = Tournament.objects.all()
    if request and getattr(request, "user", None) and request.user.is_authenticated:
        if _is_organizer(request.user):
            # Check for explicit selection via GET param first
            selected_id = request.GET.get("tournament")
            if selected_id and tournaments.filter(pk=selected_id).exists():
                selected = tournaments.get(pk=selected_id)
                request.session["selected_tournament_id"] = selected.pk
                return selected
            
            # Try session-stored selection if it exists and is still valid
            selected_id = request.session.get("selected_tournament_id")
            if selected_id and tournaments.filter(pk=selected_id).exists():
                return tournaments.get(pk=selected_id)
            
            # Default to active tournament; fall back to most recently created
            active_tournament = tournaments.filter(status="active").first()
            if active_tournament:
                request.session["selected_tournament_id"] = active_tournament.pk
                return active_tournament
            
            # Fall back to any available tournament (status-ranked)
            fallback = _get_available_tournaments().first()
            if fallback:
                request.session["selected_tournament_id"] = fallback.pk
            return fallback
        else:
            # Non-organiser: prioritize active tournament they're enrolled in
            active_tournaments = tournaments.filter(status="active")
            user_active = None
            for t in active_tournaments:
                if request.user.memberships.filter(team__tournament=t).exists():
                    user_active = t
                    break
            if user_active:
                request.session["selected_tournament_id"] = user_active.pk
                return user_active
            
            # Honour session selection if they have a membership there
            selected_id = request.session.get("selected_tournament_id")
            if selected_id:
                has_membership = request.user.memberships.filter(
                    team__tournament_id=selected_id
                ).exists()
                if has_membership:
                    t = tournaments.filter(pk=selected_id).first()
                    if t:
                        return t
            
            # Fall back to the user's first team's tournament
            team = _get_team(request.user)
            if team:
                return team.tournament
    return _get_available_tournaments().first()


def _tournament_context(request, tournament=None):
    if not request.user.is_authenticated:
        return {}
    
    # Check for dual-role users
    has_dual_roles = _has_dual_roles(request.user)
    view_mode = request.session.get("view_mode", "team") if has_dual_roles else None
    
    ctx = {
        "has_dual_roles": has_dual_roles,
        "view_mode": view_mode,
    }
    
    if _is_organizer(request.user):
        ctx.update({
            "available_tournaments": _get_available_tournaments(),
            "selected_tournament": tournament,
        })
        return ctx
    
    # Non-organiser: supply switcher data when enrolled in multiple tournaments
    user_tournament_ids = list(
        request.user.memberships.values_list("team__tournament_id", flat=True).distinct()
    )
    if len(user_tournament_ids) > 1:
        user_tournaments = list(
            Tournament.objects.filter(pk__in=user_tournament_ids).order_by("-created_at")
        )
        ctx["user_tournaments"] = user_tournaments
        ctx["selected_tournament"] = tournament
    # Open tournaments the user has NOT yet joined — for sidebar prompt
    joinable = list(
        Tournament.objects.filter(status="registration_open")
        .exclude(pk__in=user_tournament_ids)
        .order_by("-created_at")
    )
    if joinable:
        ctx["joinable_tournaments"] = joinable
    return ctx


def _get_team(user, tournament=None):
    """Return the team the user belongs to, optionally scoped to a tournament."""
    if tournament is not None:
        membership = user.memberships.filter(team__tournament=tournament).select_related("team").first()
        return membership.team if membership else None
    membership = (
        user.memberships
        .filter(team__status="active")
        .select_related("team__tournament")
        .order_by("role", "joined_at")
        .first()
    )
    if membership:
        return membership.team
    membership = user.memberships.select_related("team__tournament").order_by("role", "joined_at").first()
    return membership.team if membership else None


def _is_captain(user, team):
    """Return True only if user is the registered captain account for this team."""
    return team is not None and team.user_id == user.pk


def _is_organizer(user):
    return user.is_staff or user.is_superuser


def _has_dual_roles(user):
    """Check if user is both organizer and team member."""
    if not _is_organizer(user):
        return False
    # Check if user has any team memberships
    return user.memberships.exists()


def _organizer_count(exclude_user_id=None):
    qs = User.objects.filter(Q(is_staff=True) | Q(is_superuser=True))
    if exclude_user_id is not None:
        qs = qs.exclude(pk=exclude_user_id)
    return qs.count()


def _safe_page_param(request, default=1):
    """Return a safe positive page number from query params."""
    raw = request.GET.get("page", default)
    try:
        page = int(raw)
    except (TypeError, ValueError):
        return default
    return page if page > 0 else default


def _is_partial_refresh(request):
    return (
        request.GET.get("partial") == "1"
        and request.headers.get("x-requested-with", "").lower() == "xmlhttprequest"
    )


def _render_refreshable_page(request, full_template, partial_template, context):
    template_name = partial_template if _is_partial_refresh(request) else full_template
    return render(request, template_name, context)


def _can_override_match(match):
    """Return True if an organizer can override this match's result.

    Allowed for:
    - Pure round-robin / double round-robin tournaments (any confirmed/forfeited match)
    - Hybrid tournament group-stage matches (match.group != "") BUT only while
      the knockout phase has not yet started (no knockout match has teams assigned).
    """
    if match.status not in ("confirmed", "forfeited"):
        return False
    tournament = match.tournament
    if tournament.format in ("round_robin", "double_round_robin"):
        return True
    if tournament.format == "hybrid" and match.group:
        ko_started = tournament.matches.filter(
            group="", bracket_type="winners", team1__isnull=False
        ).exists()
        return not ko_started
    return False


def _finalize_no_show_match(match, loser, winner, reason_text, report=None, report_status="resolved"):
    if not loser or not winner:
        return False

    match.status = "forfeited"
    match.winner = winner
    match.notes = (match.notes + "\n" if match.notes else "") + reason_text
    match.save()
    _create_open_slot_for_completed_match(match, f"Completed early by no-show: {match}")

    tournament = match.tournament
    if tournament.format in ("knockout", "double_elimination", "consolation", "hybrid"):
        advance_winner(match)
    if tournament.format == "consolation":
        generate_consolation_if_ready(tournament)
    if tournament.format == "hybrid" and match.group:
        check_group_stage_complete(tournament)
    _check_and_finalize_tournament(tournament)

    if report and report.status == "pending":
        report.status = report_status
        report.resolved_at = timezone.now()
        report.save(update_fields=["status", "resolved_at"])

    return True


def _check_and_finalize_tournament(tournament):
    """
    Detect whether all matches are done and, if so, mark the tournament
    completed and store the champion.  Safe to call after every score
    confirmation — it is a no-op if the tournament is not yet active or
    if matches remain.
    """
    if tournament.status != "active":
        return False

    fmt = tournament.format

    if fmt in ("round_robin", "double_round_robin"):
        # Complete when every match that has both teams assigned is terminal
        pending = (
            tournament.matches
            .filter(team1__isnull=False, team2__isnull=False)
            .exclude(status__in=["confirmed", "forfeited", "cancelled", "bye"])
        )
        if pending.exists():
            return False

    else:
        # Bracket formats: complete when the winners-bracket final is confirmed
        # (highest-round match with next_match=None and both teams filled)
        final = (
            tournament.matches
            .filter(bracket_type="winners", next_match__isnull=True,
                    group="",
                    team1__isnull=False, team2__isnull=False)
            .order_by("-round_number")
            .first()
        )
        if not final or final.status != "confirmed":
            return False

    # All done — mark completed
    tournament.status = "completed"
    tournament.completed_at = timezone.now()
    tournament.champion = _determine_champion(tournament)
    tournament.save(update_fields=["status", "completed_at", "champion"])
    log_action(
        None,
        "tournament_completed",
        f"Tournament '{tournament.name}' completed."
        + (f" Champion: {tournament.champion.name}" if tournament.champion else ""),
        tournament=tournament,
    )
    return True


def _expire_no_show_reports(tournament=None):
    pending_reports = NoShowReport.objects.filter(status="pending").select_related(
        "match", "absent_team", "present_team"
    )
    if tournament is not None:
        pending_reports = pending_reports.filter(match__tournament=tournament)

    now = timezone.now()
    for report in pending_reports:
        match = report.match
        if match.status not in ("upcoming", "in_progress", "pending_confirmation"):
            report.status = "resolved"
            report.resolved_at = now
            report.save(update_fields=["status", "resolved_at"])
            continue

        if match.reschedule_requests.filter(status="pending", requested_by=report.absent_team).exists():
            report.status = "resolved"
            report.resolved_at = now
            report.save(update_fields=["status", "resolved_at"])
            continue

        if report.deadline_at <= now:
            _finalize_no_show_match(
                match,
                loser=report.absent_team,
                winner=report.present_team,
                reason_text=f"Auto no-show forfeit: {report.absent_team.name}",
                report=report,
                report_status="auto_forfeited",
            )


def _is_critical_stage_match(match):
    """Return True for late hybrid group-stage matches close to knockout transition."""
    tournament = match.tournament
    if tournament.format != "hybrid" or not match.group:
        return False
    remaining_group_matches = tournament.matches.filter(group__gt="").exclude(
        status__in=["confirmed", "forfeited", "cancelled", "bye"]
    ).count()
    return remaining_group_matches <= CRITICAL_STAGE_MATCHES_THRESHOLD


def _dispute_window_hours_for_match(match):
    return (
        CRITICAL_STAGE_DISPUTE_WINDOW_HOURS
        if _is_critical_stage_match(match)
        else DEFAULT_DISPUTE_WINDOW_HOURS
    )


def _is_within_dispute_window(match):
    return bool(match.dispute_deadline_at and timezone.now() <= match.dispute_deadline_at)


def _lock_match_score(match, confirmed_by_team=None, lock_note=""):
    """Lock score permanently, mark confirmed, and execute completion side-effects.

    Args:
        match: Match whose submitted score should be finalized.
        confirmed_by_team: Team that explicitly locked the score, or None for
            organizer/automatic locks.
        lock_note: Optional note appended to match notes (e.g., auto-lock reason).
    """
    tournament = match.tournament
    is_elimination = tournament.format in ("knockout", "double_elimination", "consolation") or (
        tournament.format == "hybrid" and not match.group
    )
    if is_elimination and match.score_team1 == match.score_team2:
        return False

    match.confirmed_by = confirmed_by_team
    match.status = "confirmed"
    match.score_locked_at = timezone.now()
    match.disputed_by = None
    match.critical_dispute = False
    match.dispute_resolved_at = None
    if match.score_team1 > match.score_team2:
        match.winner = match.team1
    elif match.score_team2 > match.score_team1:
        match.winner = match.team2
    else:
        match.winner = None
    if lock_note:
        match.notes = (match.notes + "\n" if match.notes else "") + lock_note
    match.save()

    _create_open_slot_for_completed_match(match, f"Completed early: {match}")
    if tournament.format in ("knockout", "double_elimination", "consolation", "hybrid"):
        advance_winner(match)
    if tournament.format == "consolation":
        generate_consolation_if_ready(tournament)
    if tournament.format == "hybrid" and match.group:
        check_group_stage_complete(tournament)
    _check_and_finalize_tournament(tournament)
    return True


def _expire_pending_score_disputes(tournament=None):
    pending_scores = Match.objects.filter(
        status="pending_confirmation",
        dispute_deadline_at__isnull=False,
    ).select_related("team1", "team2", "tournament")
    if tournament is not None:
        pending_scores = pending_scores.filter(tournament=tournament)

    now = timezone.now()
    for match in pending_scores:
        if match.dispute_deadline_at and match.dispute_deadline_at <= now:
            if _lock_match_score(match, confirmed_by_team=None, lock_note="Auto-locked after dispute deadline."):
                log_action(
                    None,
                    "score_auto_locked",
                    f"Score auto-locked for {match} after deadline: {match.score_team1}-{match.score_team2}",
                    tournament=match.tournament,
                )


def _validate_tournament_ready(tournament):
    """Return a list of human-friendly reasons a tournament cannot start yet."""
    errors = []
    active_teams = list(
        tournament.teams.filter(status="active").prefetch_related("memberships", "preferred_courts")
    )
    active_count = len(active_teams)

    if active_count < 2:
        errors.append("Need at least 2 active teams.")

    if tournament.expected_teams_count and active_count != tournament.expected_teams_count:
        errors.append(
            f"Registered teams ({active_count}) must match the expected team count ({tournament.expected_teams_count})."
        )

    required_players = max(1, tournament.players_per_team or 1)
    insufficient_players = [team.name for team in active_teams if team.memberships.count() < required_players]
    if insufficient_players:
        errors.append(
            "These teams do not have enough members: " + ", ".join(insufficient_players[:5]) + "."
        )

    if not tournament.courts.filter(is_available=True).exists():
        errors.append("Add at least one available court before starting.")
    else:
        missing_preferences = [team.name for team in active_teams if not team.preferred_courts.exists()]
        if missing_preferences:
            errors.append(
                "These teams still need court preferences: " + ", ".join(missing_preferences[:5]) + "."
            )

    has_schedule_source = (
        CourtAvailability.objects.filter(court__tournament=tournament, is_active=True).exists()
        or tournament.time_slots.exists()
    )
    if not has_schedule_source:
        errors.append("Add court availability or manual time slots before starting.")
    else:
        required_matches = estimate_required_matches(tournament, team_count=active_count)
        available_slots = count_available_slots(tournament)
        if required_matches and available_slots < required_matches:
            errors.append(
                f"Not enough court availability to schedule this tournament ({available_slots} available slots for about {required_matches} matches)."
            )

    return errors


def _create_open_slot_for_completed_match(match, reason):
    """Create an open slot if a scheduled match finished before its reserved slot ended."""
    if not match.scheduled_time or not match.court:
        return None

    slot_end = match.scheduled_end_time or match.scheduled_time
    now = timezone.now()
    if slot_end <= now:
        return None

    slot_start = match.scheduled_time
    if slot_end <= slot_start:
        return None

    slot, _ = OpenSlot.objects.get_or_create(
        tournament=match.tournament,
        court=match.court,
        start_time=slot_start,
        end_time=slot_end,
        defaults={"reason": reason},
    )
    return slot


def _sync_open_slots_for_tournament(tournament):
    """Ensure future completed matches expose their freed slots without duplicates."""
    if not tournament:
        return

    matches = tournament.matches.filter(
        status__in=["confirmed", "forfeited", "cancelled"],
        scheduled_time__isnull=False,
        court__isnull=False,
    )
    for match in matches:
        _create_open_slot_for_completed_match(match, f"Completed early: {match}")


def _build_open_slot_choices(match, slots):
    slots = list(slots)
    if not slots:
        return []

    teams = [team for team in (match.team1, match.team2) if team]
    slot_dates = {timezone.localtime(slot.start_time).date() for slot in slots}
    schedule_by_team_day = defaultdict(list)

    if teams:
        team_ids = [team.pk for team in teams]
        related_matches = (
            Match.objects.filter(
                tournament=match.tournament,
                scheduled_time__isnull=False,
            )
            .exclude(pk=match.pk)
            .exclude(status__in=["cancelled", "bye", "confirmed", "forfeited"])
            .filter(Q(team1_id__in=team_ids) | Q(team2_id__in=team_ids))
            .select_related("team1", "team2", "court")
            .order_by("scheduled_time", "match_number")
        )

        for related_match in related_matches:
            local_start = timezone.localtime(related_match.scheduled_time)
            match_day = local_start.date()
            if match_day not in slot_dates:
                continue

            local_end = (
                timezone.localtime(related_match.scheduled_end_time)
                if related_match.scheduled_end_time else None
            )

            for team in teams:
                if related_match.team1_id == team.pk or related_match.team2_id == team.pk:
                    opponent = related_match.get_opponent(team)
                    schedule_by_team_day[(team.pk, match_day)].append({
                        "match_number": related_match.match_number,
                        "time_label": (
                            f"{local_start.strftime('%H:%M')} - {local_end.strftime('%H:%M')}"
                            if local_end else local_start.strftime("%H:%M")
                        ),
                        "court_name": related_match.court.name if related_match.court else "TBD court",
                        "opponent_name": opponent.name if opponent else "TBD",
                    })

    return [
        {
            "slot": slot,
            "team_schedules": [
                {
                    "team_name": team.name,
                    "matches": schedule_by_team_day.get(
                        (team.pk, timezone.localtime(slot.start_time).date()),
                        [],
                    ),
                }
                for team in teams
            ],
        }
        for slot in slots
    ]


# -- Auth Views --

def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if request.method == "POST":
        ip = request.META.get("REMOTE_ADDR", "unknown")
        cache_key = f"login_attempts_{ip}"
        attempts = django_cache.get(cache_key, 0)
        if attempts >= 5:
            messages.error(request, "Too many failed login attempts. Please wait 5 minutes before trying again.")
            return render(request, "core/login.html")
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user:
            django_cache.delete(cache_key)
            login(request, user)
            # Clear tournament selection on login to ensure dashboard defaults to active tournament
            if "selected_tournament_id" in request.session:
                del request.session["selected_tournament_id"]
            log_action(request, "login", f"User '{username}' logged in")
            return redirect("dashboard")
        django_cache.set(cache_key, attempts + 1, timeout=300)
        messages.error(request, "Invalid credentials.")
    return render(request, "core/login.html")


def logout_view(request):
    if request.method == "POST" and request.user.is_authenticated:
        log_action(request, "logout", f"User '{request.user.username}' logged out")
        logout(request)
    elif request.method == "GET" and request.user.is_authenticated:
        # Silently log out on GET (browser pre-fetch protection) — redirect only
        logout(request)
    return redirect("login")


@login_required
def toggle_view_preference(request):
    """Toggle between organizer and team view for dual-role users."""
    if not _has_dual_roles(request.user):
        messages.error(request, "This action is only available for users with dual roles.")
        return redirect("dashboard")
    
    # Get current preference (default to 'team' if organizer just got a team)
    current_mode = request.session.get("view_mode", "team")
    new_mode = "organizer" if current_mode == "team" else "team"
    
    request.session["view_mode"] = new_mode
    log_action(request, "view_mode_toggled", f"View mode switched to '{new_mode}'")
    
    return redirect("dashboard")


def account_register_view(request):
    """Create a user account only — no team created here."""
    if request.user.is_authenticated:
        return redirect("join_tournament_list")
    if request.method == "POST":
        form = AccountRegistrationForm(request.POST)
        if form.is_valid():
            user = User.objects.create_user(
                username=form.cleaned_data["username"].strip(),
                password=form.cleaned_data["password"],
                first_name=form.cleaned_data["full_name"].strip(),
            )
            login(request, user)
            log_action(request, "account_registered", f"Account '{user.username}' created")
            return redirect("join_tournament_list")
    else:
        form = AccountRegistrationForm()
    return render(request, "core/register.html", {"form": form})


# Keep old name as alias so any hard-coded URL still works
def register_view(request, pk=None):
    if pk is not None:
        return redirect("join_tournament", pk=pk)
    return redirect("account_register")


@login_required
def join_tournament_list_view(request):
    """Show all open tournaments the user can join."""
    # Organizers cannot join tournaments directly — they must use a separate team account
    if _is_organizer(request.user):
        messages.info(
            request,
            "As an organizer, you cannot join tournaments with this account. "
            "If you want to participate, please create a separate team account."
        )
        return redirect("dashboard")
    
    open_tournaments = Tournament.objects.filter(
        status="registration_open"
    ).order_by("start_date", "created_at")

    # Annotate each tournament with the user's current team (if any)
    user_tournament_ids = set(
        request.user.memberships.values_list("team__tournament_id", flat=True)
    )

    tournament_list = []
    for t in open_tournaments:
        tournament_list.append({
            "tournament": t,
            "already_joined": t.pk in user_tournament_ids,
            "team_count": t.teams.filter(status="active").count(),
        })

    return render(request, "core/join_tournament_list.html", {
        "tournament_list": tournament_list,
    })


@login_required
def join_tournament_view(request, pk):
    """Browse teams in a tournament — join an existing one or create a new one."""
    # Organizers cannot join tournaments directly — they must use a separate team account
    if _is_organizer(request.user):
        messages.info(
            request,
            "As an organizer, you cannot join tournaments with this account. "
            "If you want to participate, please create a separate team account."
        )
        return redirect("dashboard")
    
    tournament = get_object_or_404(Tournament, pk=pk)

    if tournament.status != "registration_open":
        messages.error(request, "Registration is currently closed for this tournament.")
        return redirect("join_tournament_list")

    # Check if this user already belongs to a team in this tournament
    existing_membership = request.user.memberships.filter(
        team__tournament=tournament
    ).select_related("team").first()
    user_team = existing_membership.team if existing_membership else None

    # Build annotated team list
    teams = (
        tournament.teams
        .filter(status="active")
        .prefetch_related("memberships")
        .order_by("name")
    )
    players_per_team = tournament.players_per_team

    team_list = []
    for team in teams:
        count = team.memberships.count()
        is_full = count >= players_per_team
        is_user_member = existing_membership and existing_membership.team_id == team.pk
        team_list.append({
            "team": team,
            "member_count": count,
            "players_per_team": players_per_team,
            "is_full": is_full,
            "is_user_member": is_user_member,
        })

    return render(request, "core/join_tournament.html", {
        "tournament": tournament,
        "team_list": team_list,
        "user_team": user_team,
        "players_per_team": players_per_team,
        "registration_full": bool(
            tournament.expected_teams_count
            and tournament.teams.filter(status="active").count() >= tournament.expected_teams_count
        ),
    })


@login_required
@require_POST
def join_team_view(request, tournament_pk, team_pk):
    """Join an existing team in a tournament."""
    # Organizers cannot join tournaments directly — they must use a separate team account
    if _is_organizer(request.user):
        messages.info(
            request,
            "As an organizer, you cannot join tournaments with this account. "
            "If you want to participate, please create a separate team account."
        )
        return redirect("dashboard")
    
    tournament = get_object_or_404(Tournament, pk=tournament_pk)
    team = get_object_or_404(Team, pk=team_pk, tournament=tournament)

    if tournament.status != "registration_open":
        messages.error(request, "Registration is currently closed for this tournament.")
        return redirect("join_tournament_list")

    # Already in a team in this tournament?
    if request.user.memberships.filter(team__tournament=tournament).exists():
        messages.error(request, "You are already in a team for this tournament.")
        return redirect("join_tournament", pk=tournament_pk)

    # Team full?
    if team.memberships.count() >= tournament.players_per_team:
        messages.error(request, "That team is already full.")
        return redirect("join_tournament", pk=tournament_pk)

    TeamMembership.objects.create(team=team, user=request.user, role="member")
    log_action(
        request,
        "team_joined",
        f"User '{request.user.username}' joined team '{team.name}'",
        tournament=tournament,
    )
    messages.success(request, f"You have joined {team.name}!")
    return redirect("dashboard")


@login_required
def create_team_view(request, pk):
    """Create a brand-new team in an open tournament."""
    # Organizers cannot join tournaments directly — they must use a separate team account
    if _is_organizer(request.user):
        messages.info(
            request,
            "As an organizer, you cannot create teams with this account. "
            "If you want to participate, please create a separate team account."
        )
        return redirect("dashboard")
    
    tournament = get_object_or_404(Tournament, pk=pk)

    if tournament.status != "registration_open":
        messages.error(request, "Registration is currently closed for this tournament.")
        return redirect("join_tournament_list")

    # Already in a team in this tournament?
    if request.user.memberships.filter(team__tournament=tournament).exists():
        messages.error(request, "You are already in a team for this tournament.")
        return redirect("join_tournament", pk=pk)

    # Check registration limit
    if tournament.expected_teams_count:
        active_count = tournament.teams.filter(status="active").count()
        if active_count >= tournament.expected_teams_count:
            messages.error(
                request,
                f"Registration is full. This tournament only allows {tournament.expected_teams_count} {tournament.participant_label_plural.lower()}.",
            )
            return redirect("join_tournament_list")

    if request.method == "POST":
        form = CreateTeamForm(request.POST, tournament=tournament)
        if form.is_valid():
            team_name = form.cleaned_data["team_name"]
            if Team.objects.filter(tournament=tournament, name=team_name).exists():
                form.add_error("team_name", "A team with that name already exists in this tournament.")
            else:
                # Re-check limit inside transaction to prevent race condition
                if tournament.expected_teams_count:
                    current_count = tournament.teams.filter(status="active").count()
                    if current_count >= tournament.expected_teams_count:
                        messages.error(
                            request,
                            f"Registration just filled up. This tournament only allows {tournament.expected_teams_count} {tournament.participant_label_plural.lower()}.",
                        )
                        return redirect("join_tournament_list")
                team = Team.objects.create(
                    user=request.user,
                    tournament=tournament,
                    name=team_name,
                    department=form.cleaned_data.get("department", "").strip(),
                )
                TeamMembership.objects.create(team=team, user=request.user, role="captain")
                team.preferred_courts.set(form.cleaned_data.get("preferred_courts") or [])
                log_action(
                    request,
                    "team_created",
                    f"Team '{team_name}' created by '{request.user.username}'",
                    tournament=tournament,
                )
                messages.success(request, f"Team '{team_name}' created!")
                # Auto-close registration when limit is reached
                if (
                    tournament.expected_teams_count
                    and tournament.teams.filter(status="active").count() >= tournament.expected_teams_count
                    and tournament.status == "registration_open"
                ):
                    tournament.status = "ready"
                    tournament.save(update_fields=["status"])
                    log_action(
                        request,
                        "registration_auto_closed",
                        f"Registration auto-closed: expected {tournament.expected_teams_count} {tournament.participant_label_plural.lower()} reached",
                        tournament=tournament,
                    )
                return redirect("dashboard")
    else:
        form = CreateTeamForm(tournament=tournament)

    return render(request, "core/create_team.html", {
        "form": form,
        "tournament": tournament,
    })




# -- Dashboard --

@login_required
def dashboard_view(request):
    tournament = _get_tournament(request)
    if tournament:
        _expire_no_show_reports(tournament)
        _expire_pending_score_disputes(tournament)
    team = _get_team(request.user, tournament=tournament)
    is_organizer = _is_organizer(request.user)
    
    # For dual-role users, check view preference
    # Default: if organizer + team, show team view first; can toggle to organizer view
    has_dual_roles = _has_dual_roles(request.user)
    view_mode = request.session.get("view_mode", "team") if has_dual_roles else None

    # Determine effective view: controls which blocks render in the template
    # - Pure organizer (no team): always 'organizer'
    # - Pure team user (not staff): always 'team'
    # - Dual-role: follows session preference (default 'team')
    if has_dual_roles:
        effective_view = view_mode  # 'team' or 'organizer'
    elif is_organizer:
        effective_view = "organizer"
    else:
        effective_view = "team"

    # Non-organiser with no team yet → send to join list
    if not team and not is_organizer:
        open_count = Tournament.objects.filter(status="registration_open").count()
        if open_count > 0:
            return redirect("join_tournament_list")

    context = {
        "tournament": tournament,
        "team": team,
        "is_organizer": is_organizer,
        "is_captain": _is_captain(request.user, team),
        "has_dual_roles": has_dual_roles,
        "view_mode": view_mode,
        "effective_view": effective_view,
    }
    if tournament and team:
        team_matches_qs = Match.objects.filter(
            tournament=tournament
        ).filter(Q(team1=team) | Q(team2=team))

        # Full upcoming schedule (no cap) — split into first-5 and rest for template toggle
        all_upcoming = list(
            team_matches_qs.filter(
                status__in=["upcoming", "in_progress"]
            ).select_related("team1", "team2", "court").order_by("scheduled_time", "match_number")
        )
        context["upcoming_matches"] = all_upcoming[:5]
        context["remaining_upcoming"] = all_upcoming[5:]
        context["remaining_matches_count"] = len(all_upcoming)

        pending_matches = team_matches_qs.filter(
            status="pending_confirmation"
        ).exclude(submitted_by=team).select_related("team1", "team2", "submitted_by")
        context["pending_matches"] = pending_matches
        context["dispute_window_matches"] = pending_matches

        # Completed matches in chronological order (for trajectory)
        completed_chrono = list(
            team_matches_qs.filter(
                status__in=["confirmed", "forfeited"]
            ).select_related("team1", "team2", "winner", "court").order_by("match_number")
        )

        # Recent results: last 5, most recent first (for display)
        context["recent_matches"] = team_matches_qs.filter(
            status__in=["confirmed", "forfeited"]
        ).select_related("team1", "team2", "winner").order_by("-updated_at")[:5]

        context["pending_reschedules"] = RescheduleRequest.objects.filter(
            match__in=team_matches_qs, status="pending",
        ).exclude(requested_by=team)
        context["pending_no_show_reports"] = NoShowReport.objects.filter(
            match__in=team_matches_qs,
            status="pending",
        ).filter(Q(absent_team=team) | Q(present_team=team)).select_related(
            "match", "absent_team", "present_team"
        )

        # --- Team Analytics ---

        # 1. Standings (round-robin / group stage formats only)
        standings = []
        team_standing = None
        if tournament.format in ("round_robin", "double_round_robin", "hybrid"):
            standings = calculate_standings(tournament)
            team_standing = next((s for s in standings if s["team"].pk == team.pk), None)
        context["team_standing"] = team_standing
        
        # Add runner-ups context for completed tournaments
        if tournament.status == "completed":
            if standings:
                # For round-robin formats, get top 3 from standings
                context["tournament_champion"] = standings[0]["team"] if standings else tournament.champion
                context["tournament_runner_up_1"] = standings[1]["team"] if len(standings) > 1 else None
                context["tournament_runner_up_2"] = standings[2]["team"] if len(standings) > 2 else None
            else:
                # For bracket formats, use tournament.champion
                context["tournament_champion"] = tournament.champion
                # For bracket formats, we might not have clear 2nd/3rd, so leave empty
                context["tournament_runner_up_1"] = None
                context["tournament_runner_up_2"] = None

        # Nearby standings rows: up to 2 above + self + 2 below
        if standings and team_standing:
            team_rank_idx = next(
                (i for i, s in enumerate(standings) if s["team"].pk == team.pk), None
            )
            if team_rank_idx is not None:
                start = max(0, team_rank_idx - 2)
                end = min(len(standings), team_rank_idx + 3)
                context["standings_nearby"] = [
                    (s, s["team"].pk == team.pk) for s in standings[start:end]
                ]

        # 2. Win/loss summary (all formats)
        wins = sum(1 for m in completed_chrono if m.winner_id == team.pk)
        losses = sum(
            1 for m in completed_chrono
            if m.winner_id is not None and m.winner_id != team.pk
        )
        draws = len(completed_chrono) - wins - losses
        played = len(completed_chrono)
        context["team_record"] = {
            "played": played,
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "win_rate": round(wins / played * 100) if played else 0,
        }

        # 3. Form strip: last 5 results, most recent first
        form_strip = []
        for m in completed_chrono[-5:][::-1]:
            if m.winner_id == team.pk:
                form_strip.append("W")
            elif m.winner_id is not None:
                form_strip.append("L")
            else:
                form_strip.append("D")
        context["form_strip"] = form_strip

        # 4. Points trajectory
        running_points = 0
        trajectory = []
        for m in completed_chrono:
            opponent = m.get_opponent(team)
            if m.winner_id == team.pk:
                pts_earned = tournament.points_per_win
                result = "W"
            elif m.winner_id is not None:
                pts_earned = tournament.points_per_loss
                result = "L"
            else:
                pts_earned = tournament.points_per_draw
                result = "D"
            running_points += pts_earned
            if m.score_team1 is not None and m.score_team2 is not None:
                score = (
                    f"{m.score_team1}–{m.score_team2}"
                    if m.team1_id == team.pk
                    else f"{m.score_team2}–{m.score_team1}"
                )
            else:
                score = "–"
            trajectory.append({
                "match_number": m.match_number,
                "opponent": opponent.name if opponent else "TBD",
                "result": result,
                "score": score,
                "pts_earned": pts_earned,
                "cumulative_points": running_points,
            })
        context["points_trajectory"] = trajectory

        # 5. Next opponent intelligence
        next_match = all_upcoming[0] if all_upcoming else None
        context["next_match"] = next_match
        next_opponent = None
        next_opponent_standing = None
        h2h = {"wins": 0, "losses": 0, "draws": 0}
        if next_match:
            next_opponent = next_match.get_opponent(team)
            if next_opponent and standings:
                next_opponent_standing = next(
                    (s for s in standings if s["team"].pk == next_opponent.pk), None
                )
            if next_opponent:
                for m in completed_chrono:
                    opp = m.get_opponent(team)
                    if opp and opp.pk == next_opponent.pk:
                        if m.winner_id == team.pk:
                            h2h["wins"] += 1
                        elif m.winner_id is not None:
                            h2h["losses"] += 1
                        else:
                            h2h["draws"] += 1
        context["next_opponent"] = next_opponent
        context["next_opponent_standing"] = next_opponent_standing
        context["h2h"] = h2h

        # 6. Qualification / points gap to first place
        if team_standing and standings:
            leader_pts = standings[0]["points"]
            team_pts = team_standing["points"]
            max_possible = team_pts + len(all_upcoming) * tournament.points_per_win
            context["points_gap_to_first"] = leader_pts - team_pts
            context["max_possible_points"] = max_possible
            context["can_reach_first"] = (
                team_standing["rank"] == 1 or max_possible >= leader_pts
            )

        # 7. Court preference match rate
        preferred_court_ids = set(team.preferred_courts.values_list("id", flat=True))
        if preferred_court_ids:
            scheduled_matches = [
                m for m in (all_upcoming + completed_chrono) if m.court_id is not None
            ]
            total_scheduled = len(scheduled_matches)
            preferred_count = sum(
                1 for m in scheduled_matches if m.court_id in preferred_court_ids
            )
            context["court_pref_total"] = total_scheduled
            context["court_pref_matched"] = preferred_count
            context["court_pref_rate"] = (
                round(preferred_count / total_scheduled * 100) if total_scheduled else None
            )
    # Pre-tournament registration context for team members
    if tournament and team and tournament.status not in ("active", "completed"):
        member_count = team.memberships.count()
        context["team_member_count"] = member_count
        context["players_needed"] = max(0, tournament.players_per_team - member_count)
        context["is_team_full"] = member_count >= tournament.players_per_team
        context["registered_teams_count"] = tournament.teams.count()
        context["team_members"] = list(
            team.memberships.select_related("user").order_by("role", "joined_at")
        )

    if is_organizer:
        all_tournaments = _get_available_tournaments()
        context["all_tournaments"] = all_tournaments
        context["active_tournaments_count"] = all_tournaments.filter(status="active").count()
        context["setup_tournaments_count"] = all_tournaments.filter(
            status__in=["setup", "registration_open", "ready", "scheduled"]
        ).count()
        context["completed_tournaments_count"] = all_tournaments.filter(status="completed").count()
    if tournament and is_organizer:
        context["total_teams"] = tournament.teams.count()
        context["total_matches"] = tournament.matches.count()
        context["confirmed_matches"] = tournament.matches.filter(status="confirmed").count()
        context["pending_matches_count"] = tournament.matches.filter(status="pending_confirmation").count()
        context["disputed_matches"] = tournament.matches.filter(status="disputed").count()
        context["critical_disputes"] = tournament.matches.filter(status="disputed", critical_dispute=True).select_related(
            "team1", "team2", "disputed_by"
        )
    context.update(_tournament_context(request, tournament))
    return _render_refreshable_page(
        request,
        "core/dashboard.html",
        "core/partials/dashboard_content.html",
        context,
    )


# -- Tournament Setup --

@login_required
def tournament_setup(request):
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can set up tournaments.")
        return redirect("dashboard")
    if request.method == "POST":
        form = TournamentForm(request.POST)
        if form.is_valid():
            t = form.save()
            request.session["selected_tournament_id"] = t.pk
            log_action(request, "tournament_created",
                       f"Tournament '{t.name}' created ({t.get_format_display()})",
                       tournament=t)
            messages.success(request, f"Tournament '{t.name}' created.")
            return redirect("tournament_config", pk=t.pk)
    else:
        form = TournamentForm()
    return render(request, "core/tournament_setup.html", {
        "form": form,
        **_tournament_context(request, _get_tournament(request)),
    })


@login_required
def tournament_config(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    request.session["selected_tournament_id"] = tournament.pk
    teams = tournament.teams.prefetch_related("players", "preferred_courts").all()

    # Determine whether to show the "Proceed to Knockout Phase" button
    show_proceed_knockout = False
    if tournament.format == "hybrid" and tournament.status == "active":
        group_qs = tournament.matches.filter(group__gt="")
        if not group_qs.exists():
            # Fallback: treat all matches with teams as group stage
            group_qs = tournament.matches.filter(team1__isnull=False, team2__isnull=False)
        pending = group_qs.exclude(status__in=["confirmed", "forfeited", "cancelled", "bye"])
        ko_tbd = tournament.matches.filter(team1__isnull=True, team2__isnull=True, group="").exists()
        show_proceed_knockout = group_qs.exists() and not pending.exists() and ko_tbd

    active_teams_count = tournament.teams.filter(status="active").count()
    return render(request, "core/tournament_config.html", {
        "tournament": tournament,
        "courts": tournament.courts.all(),
        "court_availabilities": CourtAvailability.objects.filter(court__tournament=tournament).select_related("court"),
        "teams": teams,
        "active_teams_count": active_teams_count,
        "remaining_spots": max(0, (tournament.expected_teams_count or 0) - active_teams_count),
        "time_slots": tournament.time_slots.select_related("court").all(),
        "court_form": CourtForm(),
        "timeslot_form": TimeSlotForm(tournament=tournament),
        "court_availability_form": CourtAvailabilityForm(tournament=tournament),
        "bulk_team_form": BulkTeamForm(),
        "bulk_team_file_form": BulkTeamFileForm(),
        "show_proceed_knockout": show_proceed_knockout,
        **_tournament_context(request, tournament),
    })


@login_required
@require_POST
def proceed_to_knockout_view(request, pk):
    """Admin action: seed advancing teams into the knockout bracket."""
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    if tournament.format != "hybrid" or tournament.status != "active":
        messages.error(request, "Knockout phase can only be triggered for an active hybrid tournament.")
        return redirect("tournament_config", pk=pk)

    result = check_group_stage_complete(tournament)
    if result:
        messages.success(request, "Knockout phase started! Teams have been seeded into the bracket based on group standings.")
        log_action(request, "knockout_phase_started", "Admin triggered knockout phase progression", tournament=tournament)
    else:
        messages.error(request, "Cannot proceed: group stage is not yet complete, or the knockout bracket is already populated.")
    return redirect("tournament_config", pk=pk)


@login_required
@require_POST
def add_court(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    form = CourtForm(request.POST, tournament=tournament)
    if form.is_valid():
        court = form.save(commit=False)
        court.tournament = tournament
        if "availability_present" not in request.POST:
            court.is_available = True
        try:
            court.save()
        except IntegrityError:
            messages.error(
                request,
                "A court with this name already exists for this tournament.",
            )
            return redirect("tournament_config", pk=pk)
        log_action(request, "court_added", f"Court '{court.name}' added", tournament=tournament)
        messages.success(request, f"Court '{court.name}' added.")
    else:
        for error in form.errors.get("name", []):
            messages.error(request, error)
    return redirect("tournament_config", pk=pk)


@login_required
@require_POST
def add_court_availability(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    form = CourtAvailabilityForm(request.POST, tournament=tournament)
    if form.is_valid():
        courts = list(form.cleaned_data["courts"])
        weekdays = [int(day) for day in form.cleaned_data["weekdays"]]
        start_time = form.cleaned_data["start_time"]
        end_time = form.cleaned_data["end_time"]
        start_date = form.cleaned_data.get("start_date")
        end_date = form.cleaned_data.get("end_date")
        is_active = form.cleaned_data.get("is_active", False)

        existing_keys = set(
            CourtAvailability.objects.filter(
                court__in=courts,
                weekday__in=weekdays,
                start_time=start_time,
                end_time=end_time,
                start_date=start_date,
                end_date=end_date,
            ).values_list("court_id", "weekday")
        )

        to_create = []
        skipped_count = 0
        for court in courts:
            for weekday in weekdays:
                key = (court.id, weekday)
                if key in existing_keys:
                    skipped_count += 1
                    continue
                to_create.append(CourtAvailability(
                    court=court,
                    weekday=weekday,
                    start_time=start_time,
                    end_time=end_time,
                    start_date=start_date,
                    end_date=end_date,
                    is_active=is_active,
                ))
                existing_keys.add(key)

        created_count = len(to_create)
        if is_active and courts:
            Court.objects.filter(id__in=[court.id for court in courts]).update(is_available=True)

        if to_create:
            CourtAvailability.objects.bulk_create(to_create)
            log_action(
                request,
                "court_availability_added",
                f"Added {created_count} availability entries across {len(courts)} court(s)",
                tournament=tournament,
            )
            messages.success(request, f"Added {created_count} availability entr{'y' if created_count == 1 else 'ies'}.")
        if skipped_count:
            messages.warning(request, f"Skipped {skipped_count} duplicate entr{'y' if skipped_count == 1 else 'ies'}.")
        if not created_count and not skipped_count:
            messages.warning(request, "No court availability was added.")
    else:
        for errs in form.errors.values():
            for err in errs:
                messages.error(request, err)
    return redirect("tournament_config", pk=pk)


@login_required
@require_POST
def add_timeslot(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    form = TimeSlotForm(request.POST, tournament=tournament)
    if form.is_valid():
        date = form.cleaned_data["date"]
        start = form.cleaned_data["start_time"]
        end = form.cleaned_data["end_time"]
        court = form.cleaned_data.get("court")
        if end <= start:
            messages.error(request, "End time must be after start time.")
            return redirect("tournament_config", pk=pk)
        start_dt = timezone.make_aware(datetime.combine(date, start))
        end_dt = timezone.make_aware(datetime.combine(date, end))
        TimeSlot.objects.create(
            tournament=tournament,
            court=court,
            start_time=start_dt,
            end_time=end_dt,
        )
        details = f"Time slot added: {start_dt} - {end_dt}"
        if court:
            details += f" on {court.name}"
        log_action(request, "timeslot_added", details, tournament=tournament)
        messages.success(request, "Time slot added.")
    return redirect("tournament_config", pk=pk)


def _parse_team_line(line):
    """Parse a single team line: team_name,username,password[,player1;player2;...]."""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 3:
        return None
    team_name, username, password = parts[0], parts[1], parts[2]
    player_names = []
    if len(parts) >= 4 and parts[3]:
        player_names = [p.strip() for p in parts[3].split(";") if p.strip()]
    return {"team_name": team_name, "username": username, "password": password, "player_names": player_names}


def _create_teams_from_data(tournament, team_data_list, request):
    """Create teams and players from parsed data. Returns count of added teams."""
    added = 0
    for data in team_data_list:
        team_name = data["team_name"]
        username = data["username"]
        password = data["password"]
        player_names = data.get("player_names", [])
        if User.objects.filter(username=username).exists():
            messages.warning(request, f"Username '{username}' already exists, skipped.")
            continue
        if tournament.teams.filter(name=team_name).exists():
            messages.warning(request, f"Team '{team_name}' already exists, skipped.")
            continue
        # Enforce registration limit
        if tournament.expected_teams_count:
            current_count = tournament.teams.filter(status="active").count()
            if current_count >= tournament.expected_teams_count:
                messages.warning(
                    request,
                    f"Registration limit of {tournament.expected_teams_count} {tournament.participant_label_plural.lower()} reached. '{team_name}' and subsequent entries were skipped.",
                )
                break
        user = User.objects.create_user(username=username, password=password)
        team = Team.objects.create(user=user, tournament=tournament, name=team_name)
        TeamMembership.objects.create(team=team, user=user, role="captain")
        for pname in player_names:
            Player.objects.create(team=team, name=pname)
        added += 1
    return added


@login_required
@require_POST
def add_teams_bulk(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)

    team_data_list = []

    # Handle text input
    form = BulkTeamForm(request.POST)
    if form.is_valid():
        text = form.cleaned_data.get("teams_text", "").strip()
        if text:
            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                parsed = _parse_team_line(line)
                if parsed:
                    team_data_list.append(parsed)

    # Handle file upload
    file_form = BulkTeamFileForm(request.POST, request.FILES)
    if file_form.is_valid() and request.FILES.get("file"):
        uploaded = request.FILES["file"]
        MAX_UPLOAD_BYTES = 512 * 1024  # 512 KB
        MAX_LINES = 500
        if uploaded.size > MAX_UPLOAD_BYTES:
            messages.error(request, "File too large. Maximum size is 512 KB.")
            return redirect("tournament_config", pk=pk)
        content = uploaded.read(MAX_UPLOAD_BYTES + 1).decode("utf-8", errors="ignore")
        lines = content.split("\n")
        if len(lines) > MAX_LINES:
            messages.error(request, f"File has too many lines. Maximum is {MAX_LINES} teams.")
            return redirect("tournament_config", pk=pk)
        for line in lines:
            line = line.strip()
            if not line:
                continue
            parsed = _parse_team_line(line)
            if parsed:
                team_data_list.append(parsed)

    if team_data_list:
        added = _create_teams_from_data(tournament, team_data_list, request)
        log_action(request, "teams_bulk_added", f"Added {added} teams", tournament=tournament)
        messages.success(request, f"{added} teams added.")
    else:
        messages.warning(request, "No valid team data found.")

    return redirect("tournament_config", pk=pk)


@login_required
def estimate_tournament_end_date(request, pk):
    """Return a JSON estimate of when the tournament will finish."""
    if not _is_organizer(request.user):
        return JsonResponse({"error": "Unauthorized"}, status=403)
    tournament = get_object_or_404(Tournament, pk=pk)

    # Determine team count: prefer actual active teams, fall back to expected count
    team_count = tournament.teams.filter(status="active").count()
    if team_count < 2 and (tournament.expected_teams_count or 0) >= 2:
        team_count = tournament.expected_teams_count

    if team_count < 2:
        return JsonResponse({"error": "Need at least 2 teams to estimate."})

    required_matches = estimate_required_matches(tournament, team_count=team_count)
    if required_matches == 0:
        return JsonResponse({"error": "Unable to estimate matches for this format."})

    # Matches per court per day — honour the stored value or auto-detect from availability
    courts = list(tournament.courts.filter(is_available=True))
    court_count = len(courts)

    matches_per_court_per_day = tournament.matches_per_court_per_day  # stored preference
    if not matches_per_court_per_day:
        # Derive from CourtAvailability: how many match slots fit in a typical day?
        availabilities = CourtAvailability.objects.filter(
            court__tournament=tournament,
            court__is_available=True,
            is_active=True,
        )
        if availabilities.exists():
            total_minutes = 0
            entries = 0
            for av in availabilities:
                day_minutes = (
                    datetime.combine(timezone.localdate(), av.end_time)
                    - datetime.combine(timezone.localdate(), av.start_time)
                ).seconds // 60
                if day_minutes > 0:
                    total_minutes += day_minutes
                    entries += 1
            if entries:
                avg_minutes = total_minutes / entries
                matches_per_court_per_day = max(1, int(avg_minutes // tournament.default_match_duration))
        if not matches_per_court_per_day:
            matches_per_court_per_day = 4  # sensible default: 4 matches per court per day

    matches_per_day = max(1, matches_per_court_per_day * max(1, court_count))
    days_needed = math.ceil(required_matches / matches_per_day)

    start = tournament.start_date or timezone.localdate()
    estimated_end = start + timedelta(days=days_needed - 1)

    return JsonResponse({
        "team_count": team_count,
        "required_matches": required_matches,
        "court_count": court_count,
        "matches_per_court_per_day": matches_per_court_per_day,
        "matches_per_day": matches_per_day,
        "days_needed": days_needed,
        "start_date": str(start),
        "estimated_end_date": str(estimated_end),
        "format_display": tournament.get_format_display(),
        "participant_label_plural": tournament.participant_label_plural,
    })


@login_required
@require_POST
def open_registration(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    tournament.status = "registration_open"
    tournament.save(update_fields=["status"])
    log_action(request, "registration_opened", f"Registration opened for '{tournament.name}'", tournament=tournament)
    messages.success(request, "Registration is now open.")
    return redirect("tournament_config", pk=pk)


@login_required
@require_POST
def close_registration(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    tournament.status = "ready"
    tournament.save(update_fields=["status"])
    log_action(request, "registration_closed", f"Registration closed for '{tournament.name}'", tournament=tournament)
    messages.success(request, "Registration closed. The tournament is ready for scheduling checks.")
    return redirect("tournament_config", pk=pk)


@login_required
@require_POST
def generate_schedule(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    readiness_errors = _validate_tournament_ready(tournament)
    if readiness_errors:
        for error in readiness_errors:
            messages.error(request, error)
        return redirect("tournament_config", pk=pk)
    generate_fixtures(tournament)
    tournament.status = "scheduled"
    tournament.save(update_fields=["status"])
    log_action(request, "schedule_generated", f"Draft schedule generated for '{tournament.name}'", tournament=tournament)
    messages.success(request, "Draft schedule generated. Review fixtures before publishing.")
    return redirect("fixtures")


@login_required
@require_POST
def start_tournament(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    if tournament.status != "scheduled":
        readiness_errors = _validate_tournament_ready(tournament)
        if readiness_errors:
            for error in readiness_errors:
                messages.error(request, error)
            return redirect("tournament_config", pk=pk)
        generate_fixtures(tournament)
        tournament.status = "scheduled"
        tournament.save(update_fields=["status"])
        messages.info(request, "Draft schedule was generated automatically before publishing.")
    tournament.status = "active"
    tournament.started_at = timezone.now()
    tournament.save(update_fields=["status", "started_at"])
    log_action(request, "tournament_started",
               f"Tournament '{tournament.name}' started with "
               f"{tournament.teams.filter(status='active').count()} teams",
               tournament=tournament)
    messages.success(request, "Tournament started! Fixtures are now live.")
    return redirect("fixtures")


@login_required
@require_POST
def complete_tournament(request, pk):
    """Organizer manual override to mark a tournament completed."""
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    if tournament.status != "active":
        messages.error(request, "Only active tournaments can be marked as completed.")
        return redirect("tournament_config", pk=pk)
    tournament.status = "completed"
    tournament.completed_at = timezone.now()
    tournament.champion = _determine_champion(tournament)
    tournament.save(update_fields=["status", "completed_at", "champion"])
    log_action(
        request,
        "tournament_completed",
        f"Tournament '{tournament.name}' manually marked completed."
        + (f" Champion: {tournament.champion.name}" if tournament.champion else ""),
        tournament=tournament,
    )
    messages.success(
        request,
        "Tournament marked as completed."
        + (f" Champion: {tournament.champion.name}" if tournament.champion else ""),
    )
    return redirect("tournament_config", pk=pk)


@login_required
@require_POST
def select_tournament(request):
    tournament_id = request.POST.get("tournament_id")
    next_url = request.POST.get("next") or "dashboard"
    tournament = Tournament.objects.filter(pk=tournament_id).first()
    if not tournament:
        messages.error(request, "Tournament not found.")
        return redirect(next_url)

    if not _is_organizer(request.user):
        # Non-organisers can only switch to tournaments they're enrolled in
        enrolled = request.user.memberships.filter(team__tournament=tournament).exists()
        if not enrolled:
            messages.error(request, "You are not enrolled in that tournament.")
            return redirect(next_url)

    request.session["selected_tournament_id"] = tournament.pk
    messages.success(request, f"Now viewing '{tournament.name}'.")
    return redirect(next_url)


# -- Fixtures --

@login_required
def fixtures_view(request):
    tournament = _get_tournament(request)
    if tournament:
        _expire_no_show_reports(tournament)
        _expire_pending_score_disputes(tournament)
    if not tournament:
        return render(request, "core/fixtures.html", {
            "matches": [],
            **_tournament_context(request, tournament),
        })
    matches = tournament.matches.select_related("team1", "team2", "court", "winner")
    status_filter = request.GET.get("status", "")
    team_filter = request.GET.get("team", "")
    court_filter = request.GET.get("court", "")
    group_filter = request.GET.get("group", "")
    if status_filter:
        matches = matches.filter(status=status_filter)
    if team_filter:
        matches = matches.filter(Q(team1_id=team_filter) | Q(team2_id=team_filter))
    if court_filter:
        matches = matches.filter(court_id=court_filter)
    if group_filter:
        matches = matches.filter(group=group_filter)
    sort = request.GET.get("sort", "match_number")
    if sort == "time":
        matches = matches.order_by("scheduled_time", "match_number")
    elif sort == "status":
        matches = matches.order_by("status", "match_number")
    else:
        matches = matches.order_by("match_number")
    page = _safe_page_param(request)
    per_page = 25
    total = matches.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    matches = matches[(page - 1) * per_page : page * per_page]
    teams = tournament.teams.all()
    courts = tournament.courts.all()
    groups = sorted(set(tournament.teams.exclude(group="").values_list("group", flat=True)))
    context = {
        "tournament": tournament,
        "matches": matches,
        "teams": teams,
        "courts": courts,
        "groups": groups,
        "status_filter": status_filter,
        "team_filter": team_filter,
        "court_filter": court_filter,
        "group_filter": group_filter,
        "sort": sort,
        "page": page,
        "total_pages": total_pages,
        "page_range": range(1, total_pages + 1),
        "team": _get_team(request.user),
        **_tournament_context(request, tournament),
    }
    return _render_refreshable_page(
        request,
        "core/fixtures.html",
        "core/partials/fixtures_content.html",
        context,
    )


# -- Match Detail & Score Submission --

@login_required
def match_detail(request, pk):
    match = get_object_or_404(
        Match.objects.select_related("team1", "team2", "court", "winner", "submitted_by", "confirmed_by"),
        pk=pk,
    )
    _expire_no_show_reports(match.tournament)
    _expire_pending_score_disputes(match.tournament)
    match.refresh_from_db()
    _sync_open_slots_for_tournament(match.tournament)
    team = _get_team(request.user, match.tournament)
    is_organizer = _is_organizer(request.user)
    is_participant = team and (match.team1 == team or match.team2 == team)
    dispute_window_open = _is_within_dispute_window(match)
    is_critical_stage = _is_critical_stage_match(match)
    can_submit = (
        (is_participant and match.status in ("upcoming", "in_progress"))
        or (is_organizer and match.status in ("upcoming", "in_progress", "pending_confirmation", "disputed"))
    )
    can_confirm = (
        is_participant
        and match.status == "pending_confirmation"
        and match.submitted_by != team
        and dispute_window_open
    )
    can_dispute = can_confirm
    pending_no_show_report = match.no_show_reports.filter(status="pending").select_related(
        "absent_team", "present_team"
    ).first()
    no_show_window_open = bool(match.scheduled_time and match.scheduled_time <= timezone.now())
    can_mark_no_show = is_organizer and bool(match.team1_id and match.team2_id) and match.status in ("upcoming", "in_progress") and no_show_window_open
    can_report_no_show = is_participant and _is_captain(request.user, team) and bool(match.team1_id and match.team2_id) and match.status in ("upcoming", "in_progress") and no_show_window_open and not pending_no_show_report
    can_reschedule = is_participant and _is_captain(request.user, team)
    can_override_result = is_organizer and _can_override_match(match)
    reschedule_form = RescheduleForm(tournament=match.tournament)
    open_slot_choices = _build_open_slot_choices(match, reschedule_form.fields["open_slot"].queryset)
    context = {
        "match": match,
        "team": team,
        "tournament": match.tournament,
        "is_participant": is_participant,
        "can_submit": can_submit,
        "can_confirm": can_confirm,
        "can_dispute": can_dispute,
        "dispute_window_open": dispute_window_open,
        "is_critical_stage": is_critical_stage,
        "dispute_window_hours": _dispute_window_hours_for_match(match),
        "can_mark_no_show": can_mark_no_show,
        "can_report_no_show": can_report_no_show,
        "can_reschedule": can_reschedule,
        "can_override_result": can_override_result,
        "pending_no_show_report": pending_no_show_report,
        "score_form": ScoreSubmitForm(),
        "reschedule_form": reschedule_form,
        "open_slot_choices": open_slot_choices,
        "reschedule_requests": match.reschedule_requests.order_by("-created_at"),
        "is_organizer": is_organizer,
        **_tournament_context(request, match.tournament),
    }
    return _render_refreshable_page(
        request,
        "core/match_detail.html",
        "core/partials/match_detail_content.html",
        context,
    )


@login_required
@require_POST
def submit_score(request, pk):
    match = get_object_or_404(Match, pk=pk)
    _expire_pending_score_disputes(match.tournament)
    match.refresh_from_db()
    team = _get_team(request.user, match.tournament)
    is_organizer = _is_organizer(request.user)
    is_participant = team and (match.team1 == team or match.team2 == team)
    if not is_organizer and not is_participant:
        messages.error(request, "You are not a participant in this match.")
        return redirect("match_detail", pk=pk)
    if match.tournament.status == "completed":
        messages.error(request, "This tournament has already been completed.")
        return redirect("match_detail", pk=pk)
    allowed_statuses = ("upcoming", "in_progress", "pending_confirmation", "disputed") if is_organizer else ("upcoming", "in_progress")
    if match.status not in allowed_statuses:
        messages.error(request, "Score cannot be submitted for this match.")
        return redirect("match_detail", pk=pk)
    form = ScoreSubmitForm(request.POST)
    if form.is_valid():
        match.score_team1 = form.cleaned_data["score_team1"]
        match.score_team2 = form.cleaned_data["score_team2"]
        tournament = match.tournament
        is_elimination = tournament.format in ("knockout", "double_elimination", "consolation") or (
            tournament.format == "hybrid" and not match.group
        )
        if is_elimination and match.score_team1 == match.score_team2:
            messages.error(request, "Draws are not allowed in elimination matches.")
            return redirect("match_detail", pk=pk)
        if is_organizer:
            match.submitted_by = None
            match.confirmed_by = None
            match.status = "confirmed"
            match.score_submitted_at = timezone.now()
            match.dispute_deadline_at = None
            match.score_locked_at = timezone.now()
            match.disputed_by = None
            match.critical_dispute = False
            match.dispute_resolved_at = None
            match.dispute_resolution_notes = ""
            if match.score_team1 > match.score_team2:
                match.winner = match.team1
            elif match.score_team2 > match.score_team1:
                match.winner = match.team2
            else:
                match.winner = None
        else:
            match.submitted_by = team
            match.confirmed_by = None
            submitted_at = timezone.now()
            window_hours = _dispute_window_hours_for_match(match)
            match.score_submitted_at = submitted_at
            match.dispute_deadline_at = submitted_at + timedelta(hours=window_hours)
            match.score_locked_at = None
            match.disputed_by = None
            match.critical_dispute = False
            match.dispute_resolved_at = None
            match.dispute_resolution_notes = ""
            match.status = "pending_confirmation"
        if form.cleaned_data["notes"]:
            match.notes = form.cleaned_data["notes"]
        match.save()
        if is_organizer:
            _create_open_slot_for_completed_match(match, f"Completed by organizer: {match}")
            if tournament.format in ("knockout", "double_elimination", "consolation", "hybrid"):
                advance_winner(match)
            if tournament.format == "consolation":
                generate_consolation_if_ready(tournament)
            if tournament.format == "hybrid" and match.group:
                check_group_stage_complete(tournament)
            _check_and_finalize_tournament(tournament)
            log_action(
                request,
                "score_recorded_by_organizer",
                f"Organizer recorded score for {match}: {match.score_team1}-{match.score_team2}",
                tournament=tournament,
            )
            messages.success(request, "Score recorded and confirmed instantly.")
        else:
            log_action(request, "score_submitted",
                       f"Score submitted for {match}: {match.score_team1}-{match.score_team2}",
                       tournament=match.tournament)
            if _is_critical_stage_match(match):
                messages.success(
                    request,
                    f"Score submitted. Opponent has {CRITICAL_STAGE_DISPUTE_WINDOW_HOURS} hours to dispute before auto-lock."
                )
            else:
                messages.success(
                    request,
                    f"Score submitted. Opponent has {DEFAULT_DISPUTE_WINDOW_HOURS} hours to dispute before auto-lock."
                )
    return redirect("match_detail", pk=pk)


@login_required
@require_POST
def confirm_score(request, pk):
    match = get_object_or_404(Match, pk=pk)
    _expire_pending_score_disputes(match.tournament)
    match.refresh_from_db()
    team = _get_team(request.user, match.tournament)
    if not team or match.submitted_by == team:
        messages.error(request, "Cannot confirm your own submission.")
        return redirect("match_detail", pk=pk)
    if match.status != "pending_confirmation":
        messages.error(request, "Match is not pending confirmation.")
        return redirect("match_detail", pk=pk)
    if not _is_within_dispute_window(match):
        messages.error(request, "The dispute window has expired and the score is now locked.")
        return redirect("match_detail", pk=pk)
    if match.team1 != team and match.team2 != team:
        messages.error(request, "You are not a participant in this match.")
        return redirect("match_detail", pk=pk)
    tournament = match.tournament
    if not _lock_match_score(match, confirmed_by_team=team):
        messages.error(request, "Draws are not allowed in elimination matches.")
        return redirect("match_detail", pk=pk)
    log_action(request, "score_confirmed",
               f"Score confirmed for {match}: {match.score_team1}-{match.score_team2}",
               tournament=tournament)
    messages.success(request, "Score locked. Match marked done.")
    return redirect("match_detail", pk=pk)


@login_required
@require_POST
def dispute_score(request, pk):
    match = get_object_or_404(Match, pk=pk)
    _expire_pending_score_disputes(match.tournament)
    match.refresh_from_db()
    team = _get_team(request.user, match.tournament)
    if not team or match.submitted_by == team:
        messages.error(request, "Cannot dispute your own submission.")
        return redirect("match_detail", pk=pk)
    if match.status != "pending_confirmation":
        messages.error(request, "Match is not pending confirmation.")
        return redirect("match_detail", pk=pk)
    if not _is_within_dispute_window(match):
        messages.error(request, "Dispute window has expired; score is locked.")
        return redirect("match_detail", pk=pk)
    dispute_note = request.POST.get("dispute_notes", "").strip()
    match.status = "disputed"
    match.disputed_by = team
    match.critical_dispute = _is_critical_stage_match(match)
    prefix = "CRITICAL-STAGE DISPUTE" if match.critical_dispute else "DISPUTED"
    match.notes = f"{prefix} by {team.name}: {dispute_note}" if dispute_note else f"{prefix} by {team.name}"
    match.save()
    log_action(request, "score_disputed",
               f"Score disputed for {match} by {team.name}: {dispute_note}",
               tournament=match.tournament)
    if match.critical_dispute:
        messages.warning(request, "Critical-stage dispute filed. Organizers will review with priority.")
    else:
        messages.warning(request, "Score has been disputed. An organizer will review.")
    return redirect("match_detail", pk=pk)


@login_required
@require_POST
def resolve_dispute(request, pk):
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can resolve disputes.")
        return redirect("match_detail", pk=pk)
    match = get_object_or_404(Match, pk=pk)
    score1 = request.POST.get("final_score_team1")
    score2 = request.POST.get("final_score_team2")
    resolution_notes = request.POST.get("resolution_notes", "").strip()
    if match.critical_dispute and not resolution_notes:
        messages.error(request, "Critical-stage disputes require resolution notes.")
        return redirect("match_detail", pk=pk)
    if score1 is not None and score2 is not None:
        try:
            final_score1 = int(score1)
            final_score2 = int(score2)
        except (TypeError, ValueError):
            messages.error(request, "Scores must be valid whole numbers.")
            return redirect("match_detail", pk=pk)
        if final_score1 < 0 or final_score2 < 0:
            messages.error(request, "Scores cannot be negative.")
            return redirect("match_detail", pk=pk)
        tournament = match.tournament
        is_elimination = tournament.format in ("knockout", "double_elimination", "consolation") or (
            tournament.format == "hybrid" and not match.group
        )
        if is_elimination and final_score1 == final_score2:
            messages.error(request, "Draws are not allowed in elimination matches.")
            return redirect("match_detail", pk=pk)

        match.score_team1 = final_score1
        match.score_team2 = final_score2
        if not _lock_match_score(match, confirmed_by_team=None):
            messages.error(request, "Draws are not allowed in elimination matches.")
            return redirect("match_detail", pk=pk)
        match.dispute_resolution_notes = resolution_notes
        match.dispute_resolved_at = timezone.now()
        match.notes += f"\nResolved by organizer."
        if resolution_notes:
            match.notes += f"\nResolution notes: {resolution_notes}"
        match.save()
        log_action(request, "dispute_resolved",
                   f"Dispute resolved for {match}: {match.score_team1}-{match.score_team2}",
                   tournament=tournament)
        messages.success(request, "Dispute resolved. Match marked done.")
    return redirect("match_detail", pk=pk)


@login_required
@require_POST
def override_match_result(request, pk):
    """Organizer override of a completed/forfeited RR or hybrid group-stage match result."""
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can override match results.")
        return redirect("match_detail", pk=pk)
    match = get_object_or_404(Match, pk=pk)
    if not _can_override_match(match):
        messages.error(request, "This match cannot be overridden. It may be a knockout match or the knockout phase has already started.")
        return redirect("match_detail", pk=pk)

    score1 = request.POST.get("override_score_team1", "").strip()
    score2 = request.POST.get("override_score_team2", "").strip()
    reason = request.POST.get("override_reason", "").strip()

    try:
        s1, s2 = int(score1), int(score2)
    except (TypeError, ValueError):
        messages.error(request, "Scores must be valid whole numbers.")
        return redirect("match_detail", pk=pk)
    if s1 < 0 or s2 < 0:
        messages.error(request, "Scores cannot be negative.")
        return redirect("match_detail", pk=pk)

    old_status = match.get_status_display()
    old_score = f"{match.score_team1}-{match.score_team2}" if match.score_team1 is not None else "N/A"

    match.score_team1 = s1
    match.score_team2 = s2
    match.status = "confirmed"
    if s1 > s2:
        match.winner = match.team1
    elif s2 > s1:
        match.winner = match.team2
    else:
        match.winner = None  # draws are valid in round-robin

    note_parts = [f"Result overridden by organizer (was: {old_status}, {old_score})"]
    if reason:
        note_parts.append(f"Reason: {reason}")
    override_note = ". ".join(note_parts)
    match.notes = (match.notes.rstrip() + "\n" + override_note) if match.notes else override_note

    # Resolve any open no-show reports for this match
    match.no_show_reports.filter(status="pending").update(
        status="resolved", resolved_at=timezone.now()
    )
    match.save()

    log_action(
        request,
        "match_result_overridden",
        f"Match #{match.match_number} result overridden to {s1}-{s2}"
        + (f", winner={match.winner.name}" if match.winner else ", draw")
        + (f". Reason: {reason}" if reason else ""),
        tournament=match.tournament,
    )
    messages.success(request, f"Match result updated to {s1}–{s2}.")
    return redirect("match_detail", pk=pk)


# -- Rescheduling --

@login_required
@require_POST
def request_reschedule(request, pk):
    match = get_object_or_404(Match, pk=pk)
    _expire_no_show_reports(match.tournament)
    team = _get_team(request.user, match.tournament)
    if not team or (match.team1 != team and match.team2 != team):
        messages.error(request, "Not a participant.")
        return redirect("match_detail", pk=pk)
    if not _is_captain(request.user, team) and not _is_organizer(request.user):
        messages.error(request, "Only the team captain can request rescheduling.")
        return redirect("match_detail", pk=pk)
    if match.status not in ("upcoming",):
        messages.error(request, "Only upcoming matches can be rescheduled.")
        return redirect("match_detail", pk=pk)
    form = RescheduleForm(request.POST, tournament=match.tournament)
    if form.is_valid():
        open_slot = form.cleaned_data.get("open_slot")
        if open_slot:
            new_dt = open_slot.start_time
            new_court = open_slot.court
        else:
            new_dt = timezone.make_aware(
                datetime.combine(form.cleaned_data["new_date"], form.cleaned_data["new_time"])
            )
            new_court = form.cleaned_data.get("new_court") or match.court
        duration = timedelta(minutes=match.tournament.default_match_duration)
        end_dt = new_dt + duration
        active_match_statuses = ["upcoming", "in_progress", "pending_confirmation", "disputed"]
        conflicts = Match.objects.filter(
            tournament=match.tournament,
            court=new_court,
            scheduled_time__lt=end_dt,
            scheduled_end_time__gt=new_dt,
            status__in=active_match_statuses,
        ).exclude(pk=match.pk)
        if conflicts.exists():
            messages.error(request, "The selected slot has a conflict.")
            return redirect("match_detail", pk=pk)

        overlapping_team_conflicts = Match.objects.filter(
            tournament=match.tournament,
            scheduled_time__lt=end_dt,
            scheduled_end_time__gt=new_dt,
            status__in=active_match_statuses,
        ).filter(
            Q(team1=match.team1) | Q(team2=match.team1) | Q(team1=match.team2) | Q(team2=match.team2)
        ).exclude(pk=match.pk)
        if overlapping_team_conflicts.exists():
            messages.error(request, "A team in this match already has another match scheduled at that time.")
            return redirect("match_detail", pk=pk)
        RescheduleRequest.objects.create(
            match=match, requested_by=team, new_time=new_dt,
            new_court=new_court, reason=form.cleaned_data.get("reason", ""),
        )
        resolved = match.no_show_reports.filter(status="pending", absent_team=team)
        had_pending_no_show = resolved.exists()
        if had_pending_no_show:
            resolved.update(status="resolved", resolved_at=timezone.now())
        log_action(request, "reschedule_requested",
                   f"Reschedule requested for {match} to {new_dt}",
                   tournament=match.tournament)
        if had_pending_no_show:
            messages.success(request, "Reschedule request sent. The pending no-show notice has been cleared.")
        else:
            messages.success(request, "Reschedule request sent.")
    else:
        for errs in form.errors.values():
            for err in errs:
                messages.error(request, err)
    return redirect("match_detail", pk=pk)


@login_required
@require_POST
def respond_reschedule(request, pk):
    rr = get_object_or_404(RescheduleRequest, pk=pk)
    team = _get_team(request.user, rr.match.tournament)
    match = rr.match
    if not team or rr.requested_by == team:
        messages.error(request, "Cannot respond to your own request.")
        return redirect("match_detail", pk=match.pk)
    if match.team1 != team and match.team2 != team:
        messages.error(request, "Not a participant.")
        return redirect("match_detail", pk=match.pk)
    action = request.POST.get("action")
    if action == "approve":
        rr.status = "approved"
        rr.responded_at = timezone.now()
        rr.save()
        if match.scheduled_time and match.court:
            OpenSlot.objects.get_or_create(
                tournament=match.tournament, court=match.court,
                start_time=match.scheduled_time,
                end_time=match.scheduled_end_time or match.scheduled_time,
                defaults={"reason": f"Rescheduled: {match}"},
            )
        duration = timedelta(minutes=match.tournament.default_match_duration)
        target_court = rr.new_court or match.court
        OpenSlot.objects.filter(
            tournament=match.tournament,
            court=target_court,
            start_time=rr.new_time,
        ).delete()
        match.scheduled_time = rr.new_time
        match.scheduled_end_time = rr.new_time + duration
        if rr.new_court:
            match.court = rr.new_court
        match.save()
        log_action(request, "reschedule_approved", f"Reschedule approved for {match}",
                   tournament=match.tournament)
        messages.success(request, "Reschedule approved!")
    elif action == "reject":
        rr.status = "rejected"
        rr.responded_at = timezone.now()
        rr.save()
        log_action(request, "reschedule_rejected", f"Reschedule rejected for {match}",
                   tournament=match.tournament)
        messages.info(request, "Reschedule rejected.")
    return redirect("match_detail", pk=match.pk)


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
                    tournament.teams.exclude(group="").values_list("group", flat=True)
                ))
                group_standings = {}
                for g in groups:
                    group_standings[g] = calculate_standings(tournament, group=g)
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
                context["standings"] = calculate_standings(tournament)
        if tournament.format in ("knockout", "double_elimination", "consolation"):
            context["bracket"] = get_bracket_data(tournament)
    context.update(_tournament_context(request, tournament))
    return _render_refreshable_page(
        request,
        "core/standings.html",
        "core/partials/standings_content.html",
        context,
    )


# -- Teams --

@login_required
def teams_view(request):
    tournament = _get_tournament(request)
    if not tournament:
        return render(request, "core/teams.html", {
            "teams": [],
            **_tournament_context(request, tournament),
        })
    teams = tournament.teams.select_related("user").prefetch_related("players").order_by("name")
    is_organizer = _is_organizer(request.user)
    # Columns: Name + (Department + Players if multi-player) + (Group if hybrid) + Status + Account + Actions
    teams_colspan = 3  # Name, Status, Account/Actions
    if tournament.players_per_team > 1:
        teams_colspan += 2  # Department, Players
    if tournament.format == "hybrid":
        teams_colspan += 1  # Group
    return render(request, "core/teams.html", {
        "tournament": tournament, "teams": teams,
        "is_organizer": is_organizer,
        "teams_colspan": teams_colspan,
        **_tournament_context(request, tournament),
    })


@login_required
def team_detail(request, pk):
    team = get_object_or_404(Team.objects.select_related("tournament", "user").prefetch_related("players"), pk=pk)
    tournament = team.tournament
    matches = Match.objects.filter(tournament=tournament).filter(
        Q(team1=team) | Q(team2=team)
    ).select_related("team1", "team2", "court", "winner").order_by("match_number")
    stats = {
        "played": matches.filter(status__in=["confirmed", "forfeited"]).count(),
        "wins": matches.filter(winner=team).count(),
        "upcoming": matches.filter(status__in=["upcoming", "in_progress"]).count(),
    }
    stats["losses"] = stats["played"] - stats["wins"]
    is_organizer = _is_organizer(request.user)
    is_own_team = _get_team(request.user) == team
    is_captain = _is_captain(request.user, team)
    memberships = team.memberships.select_related("user").order_by("role", "joined_at")
    max_members = tournament.players_per_team if tournament else None
    members_full = max_members is not None and memberships.count() >= max_members
    return render(request, "core/team_detail.html", {
        "team": team, "tournament": tournament, "matches": matches, "stats": stats,
        "players": team.players.all(),
        "is_organizer": is_organizer,
        "is_own_team": is_own_team,
        "is_captain": is_captain,
        "memberships": memberships,
        "members_full": members_full,
        "max_members": max_members,
        "invite_form": TeamMemberInviteForm() if (is_captain or is_organizer) else None,
        **_tournament_context(request, tournament),
    })


@login_required
def manage_team_members(request, pk):
    team = get_object_or_404(Team, pk=pk)
    user_team = _get_team(request.user)
    is_organizer = _is_organizer(request.user)
    if not is_organizer and (user_team != team or not _is_captain(request.user, user_team)):
        messages.error(request, "Only the team captain can manage members.")
        return redirect("team_detail", pk=pk)
    max_members = team.tournament.players_per_team if team.tournament else None
    if max_members is not None and team.memberships.count() >= max_members:
        messages.error(request, f"Team is already at the maximum of {max_members} member(s).")
        return redirect("team_detail", pk=pk)
    if request.method == "POST":
        form = TeamMemberInviteForm(request.POST)
        if form.is_valid():
            new_user = User.objects.create_user(
                username=form.cleaned_data["username"],
                password=form.cleaned_data["password"],
            )
            TeamMembership.objects.create(team=team, user=new_user, role="member")
            log_action(
                request,
                "team_member_added",
                f"Member '{new_user.username}' added to team '{team.name}'",
                tournament=team.tournament,
            )
            messages.success(request, f"Account '{new_user.username}' created and added to {team.name}.")
        else:
            for error in form.non_field_errors():
                messages.error(request, error)
            for field in form:
                for error in field.errors:
                    messages.error(request, f"{field.label}: {error}")
    return redirect("team_detail", pk=pk)


@login_required
@require_POST
def reset_member_password(request, pk, user_pk):
    team = get_object_or_404(Team, pk=pk)
    user_team = _get_team(request.user)
    is_organizer = _is_organizer(request.user)
    if not is_organizer and (user_team != team or not _is_captain(request.user, user_team)):
        messages.error(request, "Only the team captain can reset member passwords.")
        return redirect("team_detail", pk=pk)
    membership = get_object_or_404(TeamMembership, team=team, user_id=user_pk)
    if membership.role == "captain":
        messages.error(request, "Cannot reset the captain's password this way.")
        return redirect("team_detail", pk=pk)
    new_password = request.POST.get("new_password", "").strip()
    confirm_password = request.POST.get("confirm_password", "").strip()
    if not new_password:
        messages.error(request, "New password cannot be empty.")
        return redirect("team_detail", pk=pk)
    if new_password != confirm_password:
        messages.error(request, "Passwords do not match.")
        return redirect("team_detail", pk=pk)
    if len(new_password) < 6:
        messages.error(request, "Password must be at least 6 characters.")
        return redirect("team_detail", pk=pk)
    member_user = membership.user
    member_user.set_password(new_password)
    member_user.save()
    log_action(
        request,
        "member_password_reset",
        f"Password reset for member '{member_user.username}' in team '{team.name}'",
        tournament=team.tournament,
    )
    messages.success(request, f"Password for '{member_user.username}' has been reset.")
    return redirect("team_detail", pk=pk)


@login_required
@require_POST
def reset_captain_password(request, pk):
    team = get_object_or_404(Team, pk=pk)
    if not _is_organizer(request.user):
        messages.error(request, "Only the organizer can reset a captain's password.")
        return redirect("team_detail", pk=pk)
    new_password = request.POST.get("new_password", "").strip()
    confirm_password = request.POST.get("confirm_password", "").strip()
    if not new_password:
        messages.error(request, "New password cannot be empty.")
        return redirect("team_detail", pk=pk)
    if new_password != confirm_password:
        messages.error(request, "Passwords do not match.")
        return redirect("team_detail", pk=pk)
    if len(new_password) < 6:
        messages.error(request, "Password must be at least 6 characters.")
        return redirect("team_detail", pk=pk)
    captain_user = team.user
    captain_user.set_password(new_password)
    captain_user.save()
    log_action(
        request,
        "captain_password_reset",
        f"Password reset for captain '{captain_user.username}' of team '{team.name}'",
        tournament=team.tournament,
    )
    messages.success(request, f"Password for captain '{captain_user.username}' has been reset.")
    return redirect("team_detail", pk=pk)


@login_required
@require_POST
def remove_team_member(request, pk, user_pk):
    team = get_object_or_404(Team, pk=pk)
    user_team = _get_team(request.user)
    is_organizer = _is_organizer(request.user)
    if not is_organizer and (user_team != team or not _is_captain(request.user, user_team)):
        messages.error(request, "Only the team captain can remove members.")
        return redirect("team_detail", pk=pk)
    membership = get_object_or_404(TeamMembership, team=team, user_id=user_pk)
    if membership.role == "captain":
        messages.error(request, "The captain account cannot be removed.")
        return redirect("team_detail", pk=pk)
    removed_username = membership.user.username
    member_user = membership.user
    membership.delete()
    member_user.delete()
    log_action(
        request,
        "team_member_removed",
        f"Member '{removed_username}' removed from team '{team.name}'",
        tournament=team.tournament,
    )
    messages.success(request, f"Member '{removed_username}' has been removed.")
    return redirect("team_detail", pk=pk)


@login_required
@require_POST
def withdraw_team(request, pk):
    team = get_object_or_404(Team, pk=pk)
    user_team = _get_team(request.user)
    is_organizer = _is_organizer(request.user)
    if team != user_team and not is_organizer:
        messages.error(request, "Not authorized.")
        return redirect("team_detail", pk=pk)
    if team == user_team and not is_organizer and not _is_captain(request.user, user_team):
        messages.error(request, "Only the team captain can withdraw the team.")
        return redirect("team_detail", pk=pk)
    if team.status == "withdrawn":
        messages.info(request, f"Team '{team.name}' is already withdrawn.")
        return redirect("team_detail", pk=pk)

    # Team self-withdrawal requires explicit confirmation + password check.
    if team == user_team and not is_organizer:
        if request.POST.get("confirm_withdraw") != "yes":
            messages.error(request, "Please confirm withdrawal before continuing.")
            return redirect("team_detail", pk=pk)
        password = request.POST.get("password", "")
        if not password or not request.user.check_password(password):
            messages.error(request, "Incorrect password. Withdrawal cancelled.")
            return redirect("team_detail", pk=pk)

    handle_withdrawal(request, team, team.tournament)
    messages.success(request, f"Team '{team.name}' has been withdrawn.")
    return redirect("teams")


@login_required
@require_POST
def organizer_remove_team(request, pk):
    """Organizer-only: permanently remove a team from a tournament before it goes active."""
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can remove teams.")
        return redirect("team_detail", pk=pk)
    team = get_object_or_404(Team, pk=pk)
    tournament = team.tournament
    if tournament.status in ("active", "completed"):
        messages.error(
            request,
            "Cannot remove a team from an active or completed tournament. Use 'Withdraw' instead to forfeit remaining matches.",
        )
        return redirect("team_detail", pk=pk)
    team_name = team.name
    captain_user = team.user
    team.delete()
    # Remove the captain account if they have no other teams
    if not captain_user.captained_teams.exists():
        captain_user.delete()
    log_action(
        request,
        "team_removed",
        f"Organizer removed team '{team_name}' from '{tournament.name}'",
        tournament=tournament,
    )
    messages.success(request, f"Team '{team_name}' has been removed from the tournament.")
    return redirect("tournament_config", pk=tournament.pk)


@login_required
@require_POST
def report_no_show(request, pk):
    match = get_object_or_404(
        Match.objects.select_related("team1", "team2", "tournament"),
        pk=pk,
    )
    team = _get_team(request.user, match.tournament)
    if not team or (match.team1 != team and match.team2 != team):
        messages.error(request, "Only participating teams can report a no-show.")
        return redirect("match_detail", pk=pk)
    if not _is_captain(request.user, team) and not _is_organizer(request.user):
        messages.error(request, "Only the team captain can report a no-show.")
        return redirect("match_detail", pk=pk)
    if match.status not in ("upcoming", "in_progress"):
        messages.error(request, "No-shows can only be reported for active or upcoming matches.")
        return redirect("match_detail", pk=pk)
    if not match.scheduled_time or match.scheduled_time > timezone.now():
        messages.error(request, "No-shows can only be reported after the scheduled match time has passed.")
        return redirect("match_detail", pk=pk)
    if match.no_show_reports.filter(status="pending").exists():
        messages.warning(request, "A no-show notice is already pending for this match.")
        return redirect("match_detail", pk=pk)

    no_show_team_id = request.POST.get("no_show_team")
    opponent = match.get_opponent(team)
    if not opponent or str(opponent.pk) != str(no_show_team_id):
        messages.error(request, "You can only report your opponent as a no-show.")
        return redirect("match_detail", pk=pk)

    NoShowReport.objects.create(
        match=match,
        reported_by=team,
        absent_team=opponent,
        present_team=team,
        note=request.POST.get("note", "").strip(),
        deadline_at=timezone.now() + timedelta(days=1),
    )
    log_action(
        request,
        "match_no_show_reported",
        f"No-show reported for {match}. Absent: {opponent.name}, Reporter: {team.name}",
        tournament=match.tournament,
    )
    messages.warning(request, f"No-show reported. {opponent.name} has 24 hours to request a reschedule.")
    return redirect("match_detail", pk=pk)


@login_required
@require_POST
def mark_no_show(request, pk):
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can mark no-shows.")
        return redirect("match_detail", pk=pk)

    match = get_object_or_404(Match, pk=pk)
    if match.status not in ("upcoming", "in_progress", "pending_confirmation"):
        messages.error(request, "No-show can only be recorded for active/upcoming matches.")
        return redirect("match_detail", pk=pk)
    if not match.scheduled_time or match.scheduled_time > timezone.now():
        messages.error(request, "No-show can only be recorded after the scheduled match time has passed.")
        return redirect("match_detail", pk=pk)

    no_show_team_id = request.POST.get("no_show_team")
    if str(match.team1_id) == str(no_show_team_id):
        loser = match.team1
        winner = match.team2
    elif str(match.team2_id) == str(no_show_team_id):
        loser = match.team2
        winner = match.team1
    else:
        messages.error(request, "Invalid team selected for no-show.")
        return redirect("match_detail", pk=pk)

    if not winner:
        messages.error(request, "Cannot mark no-show: opponent not assigned.")
        return redirect("match_detail", pk=pk)

    pending_report = match.no_show_reports.filter(status="pending").first()
    _finalize_no_show_match(
        match,
        loser=loser,
        winner=winner,
        reason_text=f"No-show: {loser.name}",
        report=pending_report,
        report_status="resolved",
    )

    tournament = match.tournament

    log_action(
        request,
        "match_no_show",
        f"No-show recorded for {match}. Loser: {loser.name}, Winner: {winner.name}",
        tournament=tournament,
    )
    messages.success(request, f"No-show recorded. {winner.name} wins by forfeit.")
    return redirect("match_detail", pk=pk)


@login_required
def team_preferences(request, pk):
    team = get_object_or_404(Team, pk=pk)
    user_team = _get_team(request.user)
    if (team != user_team or not _is_captain(request.user, user_team)) and not _is_organizer(request.user):
        messages.error(request, "Only the team captain or an organizer can update preferences.")
        return redirect("team_detail", pk=pk)
    if request.method == "POST":
        form = TeamPreferencesForm(request.POST, tournament=team.tournament)
        if form.is_valid():
            team.preferred_courts.set(form.cleaned_data["preferred_courts"])
            team.availability_notes = form.cleaned_data["availability_notes"]
            team.save(update_fields=["availability_notes"])
            messages.success(request, "Preferences saved.")
            return redirect("team_detail", pk=pk)
    else:
        form = TeamPreferencesForm(
            tournament=team.tournament,
            initial={"preferred_courts": team.preferred_courts.all(), "availability_notes": team.availability_notes},
        )
    return render(request, "core/team_preferences.html", {
        "team": team,
        "form": form,
        "tournament": team.tournament,
        **_tournament_context(request, team.tournament),
    })


# -- Open Slots --

@login_required
def open_slots_view(request):
    tournament = _get_tournament(request)
    if tournament:
        _expire_no_show_reports(tournament)
        _expire_pending_score_disputes(tournament)
    context = {
        "tournament": tournament,
        "slots": [],
        **_tournament_context(request, tournament),
    }
    if tournament:
        _sync_open_slots_for_tournament(tournament)
        context["slots"] = tournament.open_slots.select_related("court").filter(end_time__gt=timezone.now())
    return _render_refreshable_page(
        request,
        "core/open_slots.html",
        "core/partials/open_slots_content.html",
        context,
    )


# -- Analytics --

@login_required
def analytics_view(request):
    tournament = _get_tournament(request)
    if not tournament:
        return render(request, "core/analytics.html", _tournament_context(request, tournament))
    _expire_pending_score_disputes(tournament)
    matches = tournament.matches.all()
    teams = tournament.teams.all()
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
            "utilization": round(confirmed / total * 100, 1) if total > 0 else 0,
        })
    team_stats = []
    for team in teams.filter(status="active"):
        team_matches = matches.filter(Q(team1=team) | Q(team2=team))
        played_matches = team_matches.filter(status__in=["confirmed", "forfeited"])
        played = played_matches.count()

        # Derive wins from scores for confirmed matches; fall back to winner when needed.
        wins = 0
        for match in played_matches:
            if match.status == "forfeited":
                if match.winner_id == team.id:
                    wins += 1
                continue

            if match.score_team1 is not None and match.score_team2 is not None:
                if match.team1_id == team.id and match.score_team1 > match.score_team2:
                    wins += 1
                elif match.team2_id == team.id and match.score_team2 > match.score_team1:
                    wins += 1
            elif match.winner_id == team.id:
                wins += 1

        team_stats.append({
            "team": team, "played": played, "wins": wins, "losses": played - wins,
            "win_rate": round(wins / played * 100, 1) if played > 0 else 0,
        })
    team_stats.sort(key=lambda x: x["win_rate"], reverse=True)
    schedule_density = defaultdict(int)
    for m in matches.filter(scheduled_time__isnull=False):
        day = m.scheduled_time.strftime("%Y-%m-%d")
        schedule_density[day] += 1
    schedule_density = dict(sorted(schedule_density.items()))
    withdrawn = teams.filter(status="withdrawn")
    withdrawal_info = []
    for team in withdrawn:
        affected = matches.filter(Q(team1=team) | Q(team2=team), status__in=["forfeited", "cancelled"]).count()
        withdrawal_info.append({"team": team, "affected_matches": affected, "withdrawn_at": team.withdrawn_at})
    recent_logs = AuditLog.objects.filter(tournament=tournament).order_by("-timestamp")[:20]
    context = {
        "tournament": tournament, "match_stats": match_stats, "court_stats": court_stats,
        "team_stats": team_stats, "schedule_density": json.dumps(schedule_density),
        "withdrawal_info": withdrawal_info, "recent_logs": recent_logs,
    }
    if tournament.format in ("round_robin", "double_round_robin", "hybrid"):
        context["standings"] = calculate_standings(tournament)

    active_teams = list(teams.filter(status="active").order_by("name"))

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
                "opponent": opponent.name if opponent else "TBD",
                "result": result,
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
                        "opponent": opp_match_opp.name if opp_match_opp else "TBD",
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
                "match": prep_match,
                "opponent": opponent,
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
    if simulator_enabled:
        simulator_matches = list(
            matches.filter(
                status="upcoming",
                team1__isnull=False,
                team2__isnull=False,
            ).select_related("team1", "team2").order_by("scheduled_time", "match_number")[:8]
        )
        if simulator_matches:
            base_rows = calculate_standings(tournament)
            by_team_id = {}
            for row in base_rows:
                row_copy = dict(row)
                row_copy["point_change"] = 0
                by_team_id[row["team"].pk] = row_copy

            for m in simulator_matches:
                outcome = request.GET.get(f"sim_{m.pk}")
                m.selected_outcome = outcome or ""
                if outcome not in ("team1", "team2", "draw"):
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
            # Sort by projected points, then use standing metrics for deterministic tie-breaking.
            simulated_standings.sort(
                key=lambda s: (s["points"], s.get("game_diff", 0), s.get("games_won", 0), s.get("wins", 0)),
                reverse=True,
            )
            for idx, row in enumerate(simulated_standings, start=1):
                row["rank"] = idx

    context.update({
        "analytics_teams": active_teams,
        "h2h_team1": h2h_team1,
        "h2h_team2": h2h_team2,
        "h2h_card": h2h_card,
        "form_team": form_team,
        "form_window": form_window,
        "rolling_form_rows": rolling_form_rows,
        "prep_team": prep_team,
        "next_opponent_prep": next_opponent_prep,
        "simulator_enabled": simulator_enabled,
        "simulator_matches": simulator_matches,
        "simulated_standings": simulated_standings,
        "simulator_has_choices": simulator_has_choices,
    })
    context.update(_tournament_context(request, tournament))
    return render(request, "core/analytics.html", context)


# -- Rescheduling View --

@login_required
def rescheduling_view(request):
    tournament = _get_tournament(request)
    if not tournament:
        return render(request, "core/rescheduling.html", _tournament_context(request, tournament))
    _sync_open_slots_for_tournament(tournament)
    team = _get_team(request.user, tournament)
    requests_qs = RescheduleRequest.objects.filter(
        match__tournament=tournament
    ).select_related("match", "requested_by", "new_court").order_by("-created_at")
    if team and not _is_organizer(request.user):
        requests_qs = requests_qs.filter(
            Q(requested_by=team) | Q(match__team1=team) | Q(match__team2=team)
        )
    return render(request, "core/rescheduling.html", {
        "tournament": tournament, "requests": requests_qs,
        "open_slots": tournament.open_slots.select_related("court").filter(end_time__gt=timezone.now()), "team": team,
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


# -- Audit Log --

@login_required
def audit_log_view(request):
    tournament = _get_tournament(request)
    logs = AuditLog.objects.select_related("user")
    if tournament:
        logs = logs.filter(Q(tournament=tournament) | Q(tournament__isnull=True))
    action_filter = request.GET.get("action", "")
    if action_filter:
        logs = logs.filter(action=action_filter)
    page = _safe_page_param(request)
    per_page = 50
    total = logs.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    logs = logs[(page - 1) * per_page : page * per_page]
    actions = AuditLog.objects.values_list("action", flat=True).distinct()
    return render(request, "core/audit_log.html", {
        "logs": logs, "actions": actions, "action_filter": action_filter,
        "page": page, "total_pages": total_pages, "page_range": range(1, total_pages + 1),
        **_tournament_context(request, tournament),
    })


# -- Settings --

@login_required
@require_POST
def delete_tournament(request, pk):
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can delete tournaments.")
        return redirect("dashboard")

    tournament = get_object_or_404(Tournament, pk=pk)
    if request.POST.get("confirm_delete", "").strip().upper() != "DELETE":
        messages.error(request, "Tournament deletion was not confirmed.")
        return redirect("settings")

    tournament_name = tournament.name
    if request.session.get("selected_tournament_id") == tournament.pk:
        request.session.pop("selected_tournament_id", None)
    team_user_ids = list(
        tournament.teams.exclude(user__is_staff=True).values_list("user_id", flat=True)
    )
    tournament.delete()
    # After the cascade, only delete users who have NO remaining memberships
    # (i.e. they were solely registered for this tournament and have no other teams).
    # Users who joined another tournament must be preserved.
    if team_user_ids:
        users_still_active = set(
            TeamMembership.objects.filter(user_id__in=team_user_ids)
            .values_list("user_id", flat=True)
            .distinct()
        )
        safe_to_delete = [uid for uid in team_user_ids if uid not in users_still_active]
        if safe_to_delete:
            User.objects.filter(
                id__in=safe_to_delete,
                is_staff=False,
                is_superuser=False,
            ).delete()

    log_action(request, "tournament_deleted", f"Tournament '{tournament_name}' deleted")
    messages.success(request, f"Tournament '{tournament_name}' deleted.")
    return redirect("dashboard")


@login_required
def settings_view(request):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = _get_tournament(request)
    if not tournament:
        return render(request, "core/settings.html", _tournament_context(request, tournament))
    if request.method == "POST":
        form = TournamentForm(request.POST, instance=tournament)
        if form.is_valid():
            form.save()
            log_action(request, "settings_updated", "Tournament settings updated", tournament=tournament)
            messages.success(request, "Settings updated.")
            return redirect("settings")
    else:
        form = TournamentForm(instance=tournament)
    return render(request, "core/settings.html", {
        "tournament": tournament,
        "form": form,
        "users": User.objects.filter(is_superuser=False).order_by("username"),
        **_tournament_context(request, tournament),
    })


# -- User Management --

@login_required
@require_POST
def set_user_organizer(request, user_pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    target = get_object_or_404(User, pk=user_pk)
    if target.is_superuser:
        messages.error(request, "Superuser accounts cannot be modified here.")
        return redirect("settings")

    role_value = request.POST.get("is_organizer")
    if role_value not in {"0", "1"}:
        messages.error(request, "Invalid organizer role update request.")
        return redirect("settings")
    make_organizer = role_value == "1"
    if not make_organizer and target.is_staff:
        organizer_count = _organizer_count(exclude_user_id=target.pk)
        if organizer_count < 1:
            messages.error(request, "At least one organizer account is required.")
            return redirect("settings")
    target.is_staff = make_organizer
    target.save(update_fields=["is_staff"])

    action = "user_promoted_to_organizer" if make_organizer else "user_demoted_from_organizer"
    detail = f"User '{target.username}' role updated to {'organizer' if make_organizer else 'user'}."
    log_action(request, action, detail)
    messages.success(request, detail)
    return redirect("settings")


@login_required
@require_POST
def delete_user_account(request, user_pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    target = get_object_or_404(User, pk=user_pk)
    if target == request.user:
        messages.error(request, "You cannot delete your own account.")
        return redirect("settings")
    if target.is_superuser:
        messages.error(request, "Superuser accounts cannot be deleted here.")
        return redirect("settings")
    if target.is_staff:
        organizer_count = _organizer_count(exclude_user_id=target.pk)
        if organizer_count < 1:
            messages.error(request, "At least one organizer account is required.")
            return redirect("settings")

    username = target.username
    target.delete()
    log_action(request, "user_deleted", f"User '{username}' account deleted.")
    messages.success(request, f"User '{username}' deleted.")
    return redirect("settings")


# -- Public Views --

def public_standings(request):
    tournament = _get_tournament(request)
    if not tournament:
        return render(request, "core/public_standings.html", {})
    _expire_pending_score_disputes(tournament)
    context = {"tournament": tournament}
    if tournament.format in ("round_robin", "double_round_robin", "hybrid"):
        if tournament.format == "hybrid":
            groups = sorted(set(tournament.teams.exclude(group="").values_list("group", flat=True)))
            context["group_standings"] = {g: calculate_standings(tournament, group=g) for g in groups}
            ko_matches = tournament.matches.filter(group="", bracket_type="winners")
            if ko_matches.exists():
                context["bracket"] = get_bracket_data(tournament)
        else:
            context["standings"] = calculate_standings(tournament)
    if tournament.format in ("knockout", "double_elimination", "consolation"):
        context["bracket"] = get_bracket_data(tournament)
    return render(request, "core/public_standings.html", context)


def public_fixtures(request):
    tournament = _get_tournament(request)
    if not tournament:
        return render(request, "core/public_fixtures.html", {"matches": []})
    _expire_pending_score_disputes(tournament)
    matches = tournament.matches.select_related("team1", "team2", "court", "winner").order_by("scheduled_time", "match_number")
    return render(request, "core/public_fixtures.html", {"tournament": tournament, "matches": matches})
