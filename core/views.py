"""Core views for tournament management."""
import json
import math
import os
import random
from collections import defaultdict
from datetime import datetime, timedelta

from django.core.cache import cache as django_cache
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
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
    TeamMembership, TeamTournamentParticipation, TeamTournamentCourtPreference,
    TournamentIndividualRegistration, Notification, TeamInvite, OrganizerApplication,
    OrganizerProfile,
)
from .forms import (
    TournamentForm, CourtForm, TimeSlotForm, TeamRegistrationForm,
    AccountRegistrationForm, ProfileUpdateForm, SelfPasswordChangeForm,
    CreateTeamForm, StandaloneTeamForm,
    ScoreSubmitForm, RescheduleForm, TeamPreferencesForm, BulkTeamForm,
    BulkTeamFileForm, CourtAvailabilityForm, TeamMemberInviteForm, ExistingTeamMemberForm,
)
from .scheduling import (
    generate_fixtures,
    generate_consolation_if_ready,
    estimate_required_matches,
    estimate_completion_date,
    count_available_slots,
    _assign_schedule_to_existing,
)
from .standings import calculate_standings, advance_winner, get_bracket_data, check_group_stage_complete, _determine_champion
from .withdrawals import handle_withdrawal
from .backup import create_backup, validate_backup, restore_backup, list_backups, delete_backup
from .audit import log_action
from .services.enrollment import active_participant_count, is_registration_capacity_reached

SEARCH_RESULT_LIMIT = 30
CRITICAL_STAGE_DISPUTE_WINDOW_MINUTES = 10
DEFAULT_DISPUTE_WINDOW_MINUTES = 10
CRITICAL_STAGE_MATCHES_THRESHOLD = 2


def _get_available_tournaments():
    return Tournament.objects.annotate(
        team_count=Count("team_participations", distinct=True),
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
            # Non-organiser: explicit selection should win when user is enrolled.
            selected_id = request.GET.get("tournament")
            if selected_id:
                t_obj = tournaments.filter(pk=selected_id).first()
                if t_obj and _is_user_enrolled_in_tournament(request.user, t_obj):
                    request.session["selected_tournament_id"] = t_obj.pk
                    return t_obj

            # Honour session selection if they have a membership there.
            selected_id = request.session.get("selected_tournament_id")
            if selected_id:
                t_obj = tournaments.filter(pk=selected_id).first()
                if t_obj and _is_user_enrolled_in_tournament(request.user, t_obj):
                    return t_obj

            # Fallback: first active tournament where the user is enrolled.
            active_tournaments = tournaments.filter(status="active")
            user_active = None
            for t in active_tournaments:
                if _is_user_enrolled_in_tournament(request.user, t):
                    user_active = t
                    break
            if user_active:
                request.session["selected_tournament_id"] = user_active.pk
                return user_active
            
            # Fall back to the user's first team's most recent tournament
            team = _get_team(request.user)
            if team:
                participation = team.participations.select_related("tournament").order_by("-created_at").first()
                if participation:
                    return participation.tournament
    elif request:
        # Public pages can switch tournament via query param/session.
        selected_id = request.GET.get("tournament")
        if selected_id and tournaments.filter(pk=selected_id).exists():
            selected = tournaments.get(pk=selected_id)
            request.session["selected_tournament_id"] = selected.pk
            return selected

        selected_id = request.session.get("selected_tournament_id")
        if selected_id and tournaments.filter(pk=selected_id).exists():
            return tournaments.get(pk=selected_id)

        active_tournament = tournaments.filter(status="active").first()
        if active_tournament:
            request.session["selected_tournament_id"] = active_tournament.pk
            return active_tournament
    return _get_available_tournaments().first()


def _tournament_context(request, tournament=None):
    if not request.user.is_authenticated:
        return {}
    
    # Check for dual-role users
    has_dual_roles = _has_dual_roles(request.user)
    view_mode = request.session.get("view_mode", "team") if has_dual_roles else None

    nav_team = _get_team(request.user, tournament) if tournament else None
    ribbon_team_label = None
    if tournament and nav_team:
        ribbon_team_label = _team_display_label(tournament, nav_team)
    elif nav_team:
        ribbon_team_label = nav_team.name

    ctx = {
        "has_dual_roles": has_dual_roles,
        "view_mode": view_mode,
        "my_teams_sidebar": list(
            Team.objects.filter(memberships__user=request.user, is_internal=False)
            .distinct()
            .order_by("name")
        ),
        "ribbon_team_pk": nav_team.pk if nav_team else None,
        "ribbon_team_label": ribbon_team_label,
    }
    
    if _is_organizer(request.user):
        ctx.update({
            "available_tournaments": _get_available_tournaments(),
            "selected_tournament": tournament,
        })
        return ctx
    
    # Non-organiser: supply switcher data when enrolled in multiple tournaments
    user_tournament_ids = _get_user_tournament_ids(request.user)
    if len(user_tournament_ids) > 1:
        user_tournaments = list(
            Tournament.objects.filter(pk__in=user_tournament_ids).order_by("-created_at")
        )
        ctx["user_tournaments"] = user_tournaments
        ctx["selected_tournament"] = tournament
    # All open-registration tournaments (for notification count)
    open_registration_tournaments = list(
        Tournament.objects.filter(status="registration_open").order_by("-created_at")
    )
    if open_registration_tournaments:
        ctx["open_registration_tournaments"] = open_registration_tournaments

    # Open tournaments the user has NOT yet joined — for quick join actions
    joinable = [t for t in open_registration_tournaments if t.pk not in set(user_tournament_ids)]
    if joinable:
        ctx["joinable_tournaments"] = joinable
    return ctx


def _public_tournament_context(tournament=None):
    return {
        "public_tournaments": _get_available_tournaments(),
        "selected_tournament": tournament,
    }


def _get_user_tournament_ids(user):
    """Tournament IDs the user is enrolled in (team memberships or individual registrations)."""
    ids = set(
        user.memberships.filter(team__is_internal=False).values_list(
            "team__participations__tournament_id", flat=True
        ).distinct()
    )
    ids.update(
        TournamentIndividualRegistration.objects.filter(user=user, status="active").values_list(
            "tournament_id", flat=True
        )
    )
    return list(ids)


def _get_individual_registration(user, tournament):
    if not tournament or tournament.registration_mode != "individual":
        return None
    return (
        TournamentIndividualRegistration.objects.filter(
            user=user, tournament=tournament, status="active"
        )
        .select_related("shadow_team", "tournament")
        .first()
    )


def _is_user_enrolled_in_tournament(user, tournament):
    if tournament.registration_mode == "individual":
        if TournamentIndividualRegistration.objects.filter(
            user=user, tournament=tournament, status="active"
        ).exists():
            return True
        return user.memberships.filter(team__participations__tournament=tournament).exists()
    return user.memberships.filter(
        team__participations__tournament=tournament,
        team__is_internal=False,
    ).exists()


def _ensure_shadow_team_for_registration(registration, sport_type=None):
    """Create or sync internal shadow Team + participation for an individual registration."""
    tournament = registration.tournament
    sport = sport_type or tournament.sport_type or "other"
    if registration.shadow_team_id:
        team = registration.shadow_team
        participation, _ = TeamTournamentParticipation.objects.get_or_create(
            team=team,
            tournament=tournament,
            defaults={
                "status": registration.status,
                "group": registration.group or "",
                "seed": registration.seed,
            },
        )
        TeamTournamentParticipation.objects.filter(pk=participation.pk).update(
            status=registration.status,
            group=registration.group or "",
            seed=registration.seed,
            withdrawn_at=registration.withdrawn_at,
        )
        return team

    base = f"__tm_shadow_{tournament.pk}_{registration.user_id}_{registration.pk}"
    name = base[:100]
    idx = 0
    while Team.objects.filter(name=name).exists():
        idx += 1
        suffix = f"_{idx}"
        name = (base[: max(1, 100 - len(suffix))] + suffix)[:100]
    team = Team.objects.create(name=name, sport_type=sport, is_internal=True)
    TeamTournamentParticipation.objects.create(
        team=team,
        tournament=tournament,
        status=registration.status,
        group=registration.group or "",
        seed=registration.seed,
        withdrawn_at=registration.withdrawn_at,
    )
    registration.shadow_team = team
    registration.save(update_fields=["shadow_team", "updated_at"])
    Player.objects.get_or_create(team=team, name=registration.display_name)
    return team


def _team_display_label(tournament, team):
    if not team:
        return "TBD"
    if tournament and tournament.registration_mode == "individual":
        reg = TournamentIndividualRegistration.objects.filter(
            tournament=tournament, shadow_team=team, status="active"
        ).first()
        if reg:
            return reg.display_name
    return team.name


def _team_display_map(tournament, team_ids):
    """Return {team_id: display_label} for efficient template rendering."""
    valid_ids = [tid for tid in team_ids if tid]
    if not valid_ids:
        return {}
    if tournament and tournament.registration_mode == "individual":
        labels = dict(
            TournamentIndividualRegistration.objects.filter(
                tournament=tournament,
                status="active",
                shadow_team_id__in=valid_ids,
            ).values_list("shadow_team_id", "display_name")
        )
        return labels
    return dict(Team.objects.filter(pk__in=valid_ids).values_list("pk", "name"))


def _is_organizer(user):
    """Check if user is an approved organizer."""
    try:
        return hasattr(user, 'organizer_profile') and user.organizer_profile.verified
    except:
        return False


def _is_captain(user, team=None):
    """Check if user is captain of their active team, or of a specific team if provided."""
    if not user.is_authenticated:
        return False
    try:
        if team is None:
            # Check if captain of active team
            if not hasattr(user, 'team_assignment') or not user.team_assignment.active_team:
                return False
            return TeamMembership.objects.filter(
                user=user, 
                team=user.team_assignment.active_team, 
                role="captain"
            ).exists()
        else:
            # Check if captain of specific team
            return TeamMembership.objects.filter(
                user=user, 
                team=team, 
                role="captain"
            ).exists()
    except:
        return False


def _get_active_team(user):
    """Get user's active team, or None."""
    if not user.is_authenticated:
        return None
    try:
        if hasattr(user, 'team_assignment') and user.team_assignment.active_team:
            return user.team_assignment.active_team
    except:
        pass
    return None


def _get_team(user, tournament=None):
    """Return the user's competitor Team for match flows (membership team or individual shadow team)."""
    if tournament is not None:
        if tournament.registration_mode == "individual":
            reg = _get_individual_registration(user, tournament)
            if reg and reg.shadow_team_id:
                return reg.shadow_team
            membership = user.memberships.filter(
                team__participations__tournament=tournament
            ).select_related("team").first()
            return membership.team if membership else None
        membership = user.memberships.filter(
            team__participations__tournament=tournament,
            team__is_internal=False,
        ).select_related("team").first()
        return membership.team if membership else None
    membership = (
        user.memberships.filter(team__is_internal=False)
        .select_related("team")
        .order_by("role", "joined_at")
        .first()
    )
    if membership:
        return membership.team
    reg = (
        TournamentIndividualRegistration.objects.filter(user=user, status="active", shadow_team__isnull=False)
        .select_related("shadow_team")
        .order_by("-updated_at")
        .first()
    )
    return reg.shadow_team if reg else None


def _has_dual_roles(user):
    """Check if user is both organizer and team member."""
    if not _is_organizer(user):
        return False
    # Check if user has any team memberships
    return user.memberships.exists()


def _organizer_count(exclude_user_id=None):
    from .models import OrganizerProfile
    qs = OrganizerProfile.objects.filter(verified=True)
    if exclude_user_id is not None:
        qs = qs.exclude(user_id=exclude_user_id)
    return qs.count()


def _safe_page_param(request, default=1):
    """Return a safe positive page number from query params."""
    raw = request.GET.get("page", default)
    try:
        page = int(raw)
    except (TypeError, ValueError):
        return default
    return page if page > 0 else default


def _auto_end_date(tournament):
    """Return an auto-computed end_date using format-aware schedule simulation."""
    start = tournament.start_date
    if not start:
        return None

    if not tournament.pk:
        # Cannot access related courts/participants before the instance is saved.
        return start

    team_count = active_participant_count(tournament) or tournament.expected_teams_count or 0
    if team_count < 2:
        return start

    estimated_end = estimate_completion_date(tournament, team_count=team_count, start_date=start)
    return estimated_end or start


def _is_partial_refresh(request):
    return (
        request.GET.get("partial") == "1"
        and request.headers.get("x-requested-with", "").lower() == "xmlhttprequest"
    )


def _resolve_individual_team_name(user, requested_name=""):
    raw = (requested_name or "").strip()
    if raw:
        return raw
    display_name = (user.first_name or "").strip()
    if display_name:
        return display_name
    return user.username


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

        if match.reschedule_requests.filter(
            status="pending",
            requested_by__memberships__team=report.absent_team
        ).exists():
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


def _dispute_window_minutes_for_match(match):
    return (
        CRITICAL_STAGE_DISPUTE_WINDOW_MINUTES
        if _is_critical_stage_match(match)
        else DEFAULT_DISPUTE_WINDOW_MINUTES
    )


def _is_within_dispute_window(match):
    return bool(match.dispute_deadline_at and timezone.now() <= match.dispute_deadline_at)


def _lock_match_score(match, confirmed_by_user=None, lock_note=""):
    """Lock score permanently, mark confirmed, and execute completion side-effects.

    Args:
        match: Match whose submitted score should be finalized.
        confirmed_by_user: User that explicitly locked the score, or None for
            organizer/automatic locks.
        lock_note: Optional note appended to match notes (e.g., auto-lock reason).
    """
    tournament = match.tournament
    is_elimination = tournament.format in ("knockout", "double_elimination", "consolation") or (
        tournament.format == "hybrid" and not match.group
    )
    if is_elimination and match.score_team1 == match.score_team2:
        return False

    match.confirmed_by = confirmed_by_user
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
    # Fill dates/courts for newly unlocked knockout matches without reshuffling existing assignments.
    if tournament.format in ("knockout", "double_elimination", "consolation", "hybrid"):
        _assign_schedule_to_existing(tournament, knockout_only=True)
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
            if _lock_match_score(match, confirmed_by_user=None, lock_note="Auto-locked after dispute deadline."):
                log_action(
                    None,
                    "score_auto_locked",
                    f"Score auto-locked for {match} after deadline: {match.score_team1}-{match.score_team2}",
                    tournament=match.tournament,
                )


def _validate_tournament_ready(tournament):
    """Return a list of human-friendly reasons a tournament cannot start yet."""
    errors = []
    if tournament.registration_mode == "individual":
        active_regs = list(
            tournament.individual_registrations.filter(status="active")
            .select_related("shadow_team")
            .order_by("id")
        )
        for reg in active_regs:
            if not reg.shadow_team_id or not reg.shadow_team.is_internal:
                _ensure_shadow_team_for_registration(reg, tournament.sport_type)
        active_teams = list(
            Team.objects.filter(
                participations__tournament=tournament,
                participations__status="active",
                is_internal=True,
            ).distinct()
        )
    else:
        active_teams = list(
            Team.objects.filter(
                participations__tournament=tournament,
                participations__status="active",
                is_internal=False,
            ).prefetch_related("memberships").distinct()
        )
    active_count = len(active_teams)

    if active_count < 2:
        if tournament.registration_mode == "individual":
            errors.append("Need at least 2 active participants.")
        else:
            errors.append("Need at least 2 active teams.")

    if tournament.expected_teams_count and active_count != tournament.expected_teams_count:
        if tournament.registration_mode == "individual":
            errors.append(
                f"Registered participants ({active_count}) must match the expected participant count ({tournament.expected_teams_count})."
            )
        else:
            errors.append(
                f"Registered teams ({active_count}) must match the expected team count ({tournament.expected_teams_count})."
            )

    if tournament.registration_mode != "individual":
        required_players = max(1, tournament.players_per_team or 1)
        roster_mismatch = []
        for team in active_teams:
            count = team.memberships.count()
            if count != required_players:
                roster_mismatch.append(f"{team.name} ({count})")
        if roster_mismatch:
            errors.append(
                f"Each team must have enough members before starting (exactly {required_players} required). "
                "Mismatched teams: " + ", ".join(roster_mismatch[:5]) + "."
            )

    if not tournament.courts.filter(is_available=True).exists():
        errors.append("Add at least one available court before starting.")
    elif tournament.registration_mode != "individual":
        from .models import TeamTournamentCourtPreference
        missing_preferences = [
            team.name for team in active_teams
            if not TeamTournamentCourtPreference.objects.filter(
                participation__team=team, participation__tournament=tournament
            ).exists()
        ]
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
                f"Not enough court availability to schedule this tournament "
                f"({available_slots} available slot{'' if available_slots == 1 else 's'} for about {required_matches} matches). "
                f"Check that your court availability entries have a wide enough date range — "
                f"if an availability record has an 'End Date' set, it limits recurring slots to only those weekdays that fall before that date. "
                f"Remove the end date (leave it blank) to make availability open-ended."
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


# -- Notification helper --

def _notify(users, notification_type, message, link="", tournament=None):
    """Create Notification records for one or multiple users.

    Args:
        users: A single User instance or an iterable of User instances.
        notification_type: One of the Notification.NOTIFICATION_TYPES keys.
        message: Human-readable message text.
        link: Optional URL the notification links to.
        tournament: Optional Tournament FK value.
    """
    from django.contrib.auth.models import User as _User
    if isinstance(users, _User):
        users = [users]
    notifications = [
        Notification(
            user=u,
            notification_type=notification_type,
            message=message,
            link=link,
            tournament=tournament,
        )
        for u in users
    ]
    if notifications:
        Notification.objects.bulk_create(notifications)


def _check_roster_minimum(team):
    """Warn captain + organizers when a team's roster drops below the minimum (12.3).

    Called after a member leaves or is removed from a team.
    Returns a list of notifications created.
    """
    current_count = team.memberships.count()
    # Gather all active tournaments this team participates in
    active_participations = TeamTournamentParticipation.objects.filter(
        team=team,
        status__in=["active", "pending"],
    ).select_related("tournament")

    from django.contrib.auth.models import User as _User
    for participation in active_participations:
        tournament = participation.tournament
        if tournament.status not in ("active", "paused", "registration_open", "ready", "scheduled"):
            continue
        min_size = max(1, tournament.players_per_team or 1)
        if current_count < min_size:
            captain_user = _User.objects.filter(
                memberships__team=team, memberships__role="captain"
            ).first()
            if captain_user:
                _notify(
                    captain_user,
                    "general",
                    f"⚠️ Your team '{team.name}' now has {current_count} player(s) but '{tournament.name}' requires {min_size}. "
                    f"You may be disqualified if the roster is not restored.",
                    link=f"/team/{team.pk}/",
                    tournament=tournament,
                )


def _promote_team_participation_when_full(team, tournament=None, request=None):
    """Promote pending participations to active when the roster has enough members."""
    if team.is_internal:
        return []

    current_count = team.memberships.count()
    pending_qs = TeamTournamentParticipation.objects.filter(team=team, status="pending").select_related("tournament")
    if tournament is not None:
        pending_qs = pending_qs.filter(tournament=tournament)

    promoted_tournaments = []
    for participation in pending_qs:
        required = max(1, participation.tournament.players_per_team or 1)
        if current_count < required:
            continue

        participation.status = "active"
        participation.save(update_fields=["status"])
        promoted_tournaments.append(participation.tournament)
        log_action(
            request,
            "team_registration_completed",
            f"Team '{team.name}' roster complete — registration active in '{participation.tournament.name}'",
            tournament=participation.tournament,
        )

        if (
            is_registration_capacity_reached(participation.tournament)
            and participation.tournament.status == "registration_open"
        ):
            participation.tournament.status = "ready"
            participation.tournament.save(update_fields=["status"])
            log_action(
                request,
                "registration_auto_closed",
                (
                    f"Registration auto-closed: expected "
                    f"{participation.tournament.expected_teams_count} "
                    f"{participation.tournament.participant_label_plural.lower()} reached"
                ),
                tournament=participation.tournament,
            )

    return promoted_tournaments


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


@login_required
def profile_view(request):
    tournament = _get_tournament(request)
    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action == "change_password":
            profile_form = ProfileUpdateForm(initial={
                "first_name": request.user.first_name,
                "last_name": request.user.last_name,
                "email": request.user.email,
            })
            password_form = SelfPasswordChangeForm(request.POST)
            if password_form.is_valid():
                current_password = password_form.cleaned_data["current_password"]
                if not request.user.check_password(current_password):
                    messages.error(request, "Current password is incorrect.")
                else:
                    request.user.set_password(password_form.cleaned_data["new_password"])
                    request.user.save(update_fields=["password"])
                    update_session_auth_hash(request, request.user)
                    log_action(request, "profile_password_changed", f"User '{request.user.username}' changed password")
                    messages.success(request, "Password updated successfully.")
                    return redirect("profile")
        else:
            profile_form = ProfileUpdateForm(request.POST)
            password_form = SelfPasswordChangeForm()
            if profile_form.is_valid():
                request.user.first_name = profile_form.cleaned_data.get("first_name", "").strip()
                request.user.last_name = profile_form.cleaned_data.get("last_name", "").strip()
                request.user.email = profile_form.cleaned_data.get("email", "").strip()
                request.user.save(update_fields=["first_name", "last_name", "email"])
                log_action(request, "profile_updated", f"User '{request.user.username}' updated profile")
                messages.success(request, "Profile updated.")
                return redirect("profile")
    else:
        profile_form = ProfileUpdateForm(initial={
            "first_name": request.user.first_name,
            "last_name": request.user.last_name,
            "email": request.user.email,
        })
        password_form = SelfPasswordChangeForm()

    return render(request, "core/profile.html", {
        "profile_form": profile_form,
        "password_form": password_form,
        **_tournament_context(request, tournament),
    })


# Keep old name as alias so any hard-coded URL still works
def register_view(request, pk=None):
    if pk is not None:
        return redirect("join_tournament", pk=pk)
    return redirect("account_register")


@login_required
def join_tournament_list_view(request):
    """Show all open tournaments the user can join."""
    open_tournaments = Tournament.objects.filter(
        status="registration_open"
    ).order_by("start_date", "created_at")

    user_tournament_ids = set(_get_user_tournament_ids(request.user))

    tournament_list = []
    for t in open_tournaments:
        entry_count = active_participant_count(t)
        tournament_list.append({
            "tournament": t,
            "already_joined": t.pk in user_tournament_ids,
            "team_count": entry_count,
        })

    return render(request, "core/join_tournament_list.html", {
        "tournament_list": tournament_list,
    })


@login_required
def join_tournament_view(request, pk):
    """Browse teams in a tournament — join an existing one or create a new one."""
    tournament = get_object_or_404(Tournament, pk=pk)

    if tournament.status != "registration_open":
        messages.error(request, "Registration is currently closed for this tournament.")
        return redirect("join_tournament_list")

    existing_membership = request.user.memberships.filter(
        team__participations__tournament=tournament
    ).select_related("team").first()
    user_registration = _get_individual_registration(request.user, tournament)
    user_team = (
        existing_membership.team
        if tournament.registration_mode == "team" and existing_membership
        else None
    )

    players_per_team = tournament.players_per_team
    registration_mode = tournament.registration_mode

    team_list = []
    participant_list = []

    if registration_mode == "individual":
        regs = (
            tournament.individual_registrations.filter(status="active")
            .select_related("user", "shadow_team")
            .order_by("display_name", "id")
        )
        for reg in regs:
            participant_list.append({
                "registration": reg,
                "display_name": reg.display_name,
                "is_self": reg.user_id == request.user.pk,
            })
    else:
        participations = (
            TeamTournamentParticipation.objects.filter(
                tournament=tournament,
                status__in=["active", "pending"],
                team__is_internal=False,
            )
            .select_related("team")
            .prefetch_related("team__memberships")
            .order_by("team__name")
        )
        for participation in participations:
            team = participation.team
            count = team.memberships.count()
            is_full = count >= players_per_team
            is_user_member = existing_membership and existing_membership.team_id == team.pk
            team_list.append({
                "team": team,
                "member_count": count,
                "players_per_team": players_per_team,
                "is_full": is_full,
                "is_user_member": is_user_member,
                "participation_status": participation.status,
            })

    return render(request, "core/join_tournament.html", {
        "tournament": tournament,
        "team_list": team_list,
        "participant_list": participant_list,
        "user_team": user_team,
        "user_registration": user_registration,
        "players_per_team": players_per_team,
        "registration_mode": registration_mode,
        "registration_full": is_registration_capacity_reached(tournament),
    })


@login_required
@require_POST
def join_team_view(request, tournament_pk, team_pk):
    """Join an existing team in a tournament."""
    tournament = get_object_or_404(Tournament, pk=tournament_pk)
    if tournament.registration_mode == "individual":
        messages.error(request, "This tournament only accepts individual registrations.")
        return redirect("join_tournament", pk=tournament_pk)

    team = get_object_or_404(Team, pk=team_pk)
    # Verify the team participates in this tournament (pending or active)
    participation = TeamTournamentParticipation.objects.filter(
        team=team, tournament=tournament, status__in=["active", "pending"]
    ).first()
    if not participation:
        messages.error(request, "That team is not registered for this tournament.")
        return redirect("join_tournament", pk=tournament_pk)

    if tournament.status != "registration_open":
        messages.error(request, "Registration is currently closed for this tournament.")
        return redirect("join_tournament_list")

    if _is_user_enrolled_in_tournament(request.user, tournament):
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

    # Promote participation to active once roster is full
    new_count = team.memberships.count()
    required = max(1, tournament.players_per_team or 1)
    promoted_tournament_ids = {
        t.pk for t in _promote_team_participation_when_full(team, tournament=tournament, request=request)
    }
    if tournament.pk in promoted_tournament_ids:
        messages.success(request, f"You joined {team.name}! The roster is now complete — your team is fully registered.")
    else:
        still_needed = required - new_count
        messages.success(request, f"You joined {team.name}! {still_needed} more player{'s' if still_needed != 1 else ''} needed to complete registration.")

    return redirect("dashboard")


@login_required
def create_team_view(request, pk):
    """Create a brand-new team in an open tournament."""
    tournament = get_object_or_404(Tournament, pk=pk)

    if tournament.status != "registration_open":
        messages.error(request, "Registration is currently closed for this tournament.")
        return redirect("join_tournament_list")

    if _is_user_enrolled_in_tournament(request.user, tournament):
        messages.error(request, "You are already registered for this tournament.")
        return redirect("join_tournament", pk=pk)

    # Determine if registration is full and waitlisting applies
    _registration_is_full = bool(
        tournament.expected_teams_count
        and active_participant_count(tournament) >= tournament.expected_teams_count
    )

    if request.method == "POST":
        form = CreateTeamForm(request.POST, tournament=tournament)
        if form.is_valid():
            if tournament.registration_mode == "individual":
                # Individuals can't be waitlisted (no multi-player roster logic)
                if _registration_is_full:
                    messages.error(
                        request,
                        f"Registration is full. This tournament only allows {tournament.expected_teams_count} {tournament.participant_label_plural.lower()}.",
                    )
                    return redirect("join_tournament", pk=pk)
                requested_name = form.cleaned_data.get("participant_name", "")
                display_name = _resolve_individual_team_name(request.user, requested_name=requested_name)

                if TournamentIndividualRegistration.objects.filter(
                    tournament=tournament, display_name__iexact=display_name
                ).exclude(user=request.user).exists():
                    form.add_error(
                        "participant_name",
                        "That name is already in use. Please choose a different player name.",
                    )
                    return render(request, "core/create_team.html", {
                        "form": form,
                        "tournament": tournament,
                        **_tournament_context(request, tournament),
                    })

                registration, _ = TournamentIndividualRegistration.objects.update_or_create(
                    user=request.user,
                    tournament=tournament,
                    defaults={
                        "display_name": display_name,
                        "status": "active",
                        "withdrawn_at": None,
                    },
                )
                _ensure_shadow_team_for_registration(registration, tournament.sport_type)
                log_action(
                    request,
                    "individual_registered",
                    f"Player '{display_name}' registered for '{tournament.name}'",
                    tournament=tournament,
                )
                messages.success(request, f"You are registered as '{display_name}'.")
                return redirect("dashboard")

            team_name = form.cleaned_data["team_name"]
            if Team.objects.filter(name__iexact=team_name).exists():
                form.add_error("team_name", "A team with that name already exists.")
            else:
                required = max(1, tournament.players_per_team or 1)
                # If registration full, put team on waitlist (4.7)
                if _registration_is_full:
                    initial_status = "waitlisted"
                elif required == 1:
                    # Single-player: captain alone completes the team → active
                    initial_status = "active"
                else:
                    # Multi-player: start pending until full roster joins
                    initial_status = "pending"
                team = Team.objects.create(
                    name=team_name,
                    department=form.cleaned_data.get("department", "").strip(),
                    sport_type=tournament.sport_type,
                )
                TeamTournamentParticipation.objects.create(team=team, tournament=tournament, status=initial_status)
                TeamMembership.objects.create(team=team, user=request.user, role="captain")
                log_action(
                    request,
                    "team_created",
                    f"Team '{team_name}' created by '{request.user.username}' (status: {initial_status})",
                    tournament=tournament,
                )
                if initial_status == "waitlisted":
                    messages.success(
                        request,
                        f"Team '{team_name}' created! Registration is full — you have been added to the waitlist.",
                    )
                elif initial_status == "pending":
                    still_needed = required - 1
                    messages.success(
                        request,
                        f"Team '{team_name}' created! Share it with your teammates — "
                        f"you need {still_needed} more player{'s' if still_needed != 1 else ''} to complete registration."
                    )
                else:
                    messages.success(request, f"Team '{team_name}' created!")
                    # Auto-close only when team goes active (players_per_team == 1)
                    if (
                        is_registration_capacity_reached(tournament)
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
        "registration_full": _registration_is_full,
        **_tournament_context(request, tournament),
    })


@login_required
def create_standalone_team_view(request):
    """Create a reusable team independent of any tournament."""
    if request.method == "POST":
        form = StandaloneTeamForm(request.POST)
        if form.is_valid():
            team_name = form.cleaned_data["team_name"].strip()
            if Team.objects.filter(name__iexact=team_name).exists():
                form.add_error("team_name", "A team with that name already exists.")
            else:
                team = Team.objects.create(
                    name=team_name,
                    department=form.cleaned_data.get("department", "").strip(),
                    sport_type=form.cleaned_data.get("sport_type") or "other",
                )
                TeamMembership.objects.create(team=team, user=request.user, role="captain")
                log_action(request, "standalone_team_created", f"Team '{team_name}' created by '{request.user.username}'")
                messages.success(request, f"Team '{team_name}' created.")
                return redirect("team_detail", pk=team.pk)
    else:
        form = StandaloneTeamForm()

    tournament = _get_tournament(request)
    return render(request, "core/create_standalone_team.html", {
        "form": form,
        **_tournament_context(request, tournament),
    })




# -- Dashboard --

@login_required
def dashboard_view(request):
    tournament = _get_tournament(request)
    if tournament:
        _expire_no_show_reports(tournament)
        _expire_pending_score_disputes(tournament)
    team = _get_team(request.user, tournament=tournament)
    individual_registration = (
        _get_individual_registration(request.user, tournament)
        if tournament and tournament.registration_mode == "individual"
        else None
    )
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

    dashboard_display_name = (
        individual_registration.display_name
        if individual_registration
        else (team.name if team else "")
    )
    context = {
        "tournament": tournament,
        "team": team,
        "individual_registration": individual_registration,
        "dashboard_display_name": dashboard_display_name,
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
        ).exclude(submitted_by=request.user).select_related("team1", "team2", "submitted_by")
        context["pending_matches"] = pending_matches
        context["dispute_window_matches"] = pending_matches

        # Completed matches in chronological order (for trajectory)
        completed_chrono = list(
            team_matches_qs.filter(
                status__in=["confirmed", "forfeited"]
            ).select_related("team1", "team2", "winner", "court").order_by("match_number")
        )

        # Recent results: last 5, most recent first (for display)
        recent_matches = list(
            team_matches_qs.filter(status__in=["confirmed", "forfeited"])
            .select_related("team1", "team2", "winner")
            .order_by("-updated_at")[:5]
        )
        for m in recent_matches:
            opp = m.team2 if m.team1_id == team.pk else m.team1
            m.opponent_display = _team_display_label(tournament, opp)
        context["recent_matches"] = recent_matches

        context["pending_reschedules"] = RescheduleRequest.objects.filter(
            match__in=team_matches_qs, status="pending",
        ).exclude(requested_by=request.user)
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
            for s in standings:
                s["display_label"] = _team_display_label(tournament, s["team"])
            team_standing = next((s for s in standings if s["team"].pk == team.pk), None)
        context["team_standing"] = team_standing
        
        # Add runner-ups context for completed tournaments
        if tournament.status == "completed":
            if standings:
                # For round-robin formats, get top 3 from standings
                context["tournament_champion"] = standings[0]["team"] if standings else tournament.champion
                context["tournament_runner_up_1"] = standings[1]["team"] if len(standings) > 1 else None
                context["tournament_runner_up_2"] = standings[2]["team"] if len(standings) > 2 else None
                context["tournament_champion_label"] = standings[0].get("display_label") or _team_display_label(
                    tournament, standings[0]["team"]
                )
                context["tournament_runner_up_1_label"] = (
                    standings[1].get("display_label")
                    if len(standings) > 1
                    else None
                )
                context["tournament_runner_up_2_label"] = (
                    standings[2].get("display_label")
                    if len(standings) > 2
                    else None
                )
            else:
                # For bracket formats, use tournament.champion
                context["tournament_champion"] = tournament.champion
                # For bracket formats, we might not have clear 2nd/3rd, so leave empty
                context["tournament_runner_up_1"] = None
                context["tournament_runner_up_2"] = None
                if tournament.champion:
                    context["tournament_champion_label"] = _team_display_label(tournament, tournament.champion)

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
                "opponent": _team_display_label(tournament, opponent) if opponent else "TBD",
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
        context["next_opponent_label"] = (
            _team_display_label(tournament, next_opponent) if next_opponent else None
        )
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
        from .models import TeamTournamentCourtPreference
        preferred_court_ids = set(
            TeamTournamentCourtPreference.objects.filter(
                participation__team=team, participation__tournament=tournament
            ).values_list("court_id", flat=True)
        )
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
        if tournament.registration_mode == "individual" and individual_registration:
            context["team_member_count"] = 1
            context["players_needed"] = 0
            context["is_team_full"] = True
            context["team_participation_status"] = "active"
            context["registered_teams_count"] = active_participant_count(tournament)
            context["team_members"] = []
        else:
            member_count = team.memberships.count()
            context["team_member_count"] = member_count
            context["players_needed"] = max(0, tournament.players_per_team - member_count)
            context["is_team_full"] = member_count >= tournament.players_per_team
            context["registered_teams_count"] = active_participant_count(tournament)
            context["team_members"] = list(
                team.memberships.select_related("user").order_by("role", "joined_at")
            )
            team_part = TeamTournamentParticipation.objects.filter(
                team=team, tournament=tournament
            ).only("status").first()
            context["team_participation_status"] = team_part.status if team_part else "active"

    if is_organizer:
        all_tournaments = _get_available_tournaments()
        context["all_tournaments"] = all_tournaments
        context["active_tournaments_count"] = all_tournaments.filter(status="active").count()
        context["setup_tournaments_count"] = all_tournaments.filter(
            status__in=["setup", "registration_open", "ready", "scheduled"]
        ).count()
        context["completed_tournaments_count"] = all_tournaments.filter(status="completed").count()
    if tournament and is_organizer:
        context["total_teams"] = active_participant_count(tournament)
        context["roster_label"] = (
            "Participants" if tournament.registration_mode == "individual" else "Teams"
        )
        context["total_matches"] = tournament.matches.count()
        context["confirmed_matches"] = tournament.matches.filter(status="confirmed").count()
        context["pending_matches_count"] = tournament.matches.filter(status="pending_confirmation").count()
        context["disputed_matches"] = tournament.matches.filter(status="disputed").count()
        context["critical_disputes"] = tournament.matches.filter(status="disputed", critical_dispute=True).select_related(
            "team1", "team2", "disputed_by"
        )
        for match in context["critical_disputes"]:
            match.team1_label = _team_display_label(tournament, match.team1)
            match.team2_label = _team_display_label(tournament, match.team2)
        if context.get("all_tournaments"):
            for t in context["all_tournaments"]:
                t.champion_display_label = _team_display_label(t, t.champion) if t.champion else ""
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
            t = form.save(commit=False)
            if not t.end_date and t.start_date:
                t.end_date = _auto_end_date(t)
            t.save()
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
    team_participations = list(
        TeamTournamentParticipation.objects.filter(tournament=tournament)
        .select_related("team")
        .order_by("team__name")
    )

    required_members = max(1, tournament.players_per_team or 1)
    for participation in team_participations:
        if participation.team.is_internal:
            reg = TournamentIndividualRegistration.objects.filter(
                shadow_team=participation.team, tournament=tournament
            ).first()
            participation.member_count = 1 if reg else 0
        else:
            participation.member_count = participation.team.memberships.count()
            if (
                participation.status == "pending"
                and participation.member_count >= required_members
            ):
                participation.status = "active"
                participation.save(update_fields=["status"])
        participation.preferred_court_names = list(
            TeamTournamentCourtPreference.objects.filter(participation=participation)
            .select_related("court")
            .values_list("court__name", flat=True)
        )
        # Flag teams that are still pending and under the required roster size.
        participation.players_needed = max(0, required_members - participation.member_count)
        participation.is_underfilled = (
            participation.status == "pending" and participation.member_count < required_members
        )

    underfilled_count = sum(1 for p in team_participations if p.is_underfilled)

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

    available_slots = count_available_slots(tournament)
    active_count = active_participant_count(tournament)
    required_matches = estimate_required_matches(tournament, team_count=active_count)
    active_teams_count = active_count

    return render(request, "core/tournament_config.html", {
        "tournament": tournament,
        "tournament_champion_label": (
            _team_display_label(tournament, tournament.champion) if tournament.champion else ""
        ),
        "courts": tournament.courts.all(),
        "court_availabilities": CourtAvailability.objects.filter(court__tournament=tournament).select_related("court"),
        "team_participations": team_participations,
        "underfilled_count": underfilled_count,
        "active_teams_count": active_teams_count,
        "remaining_spots": max(0, (tournament.expected_teams_count or 0) - active_teams_count),
        "time_slots": tournament.time_slots.select_related("court").all(),
        "court_form": CourtForm(),
        "timeslot_form": TimeSlotForm(tournament=tournament),
        "court_availability_form": CourtAvailabilityForm(tournament=tournament),
        "bulk_team_form": BulkTeamForm(),
        "bulk_team_file_form": BulkTeamFileForm(),
        "show_proceed_knockout": show_proceed_knockout,
        "available_slots": available_slots,
        "required_matches": required_matches,
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
def delete_court_availability(request, pk, availability_pk):
    """Organiser deletes a single court availability record."""
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    availability = get_object_or_404(CourtAvailability, pk=availability_pk, court__tournament=tournament)
    label = str(availability)
    availability.delete()
    log_action(request, "court_availability_deleted", f"Deleted availability: {label}", tournament=tournament)
    messages.success(request, f"Availability '{label}' removed.")
    return redirect("tournament_config", pk=pk)


def _calculate_daily_slots(start_time, end_time, duration_minutes):
    total_minutes = (datetime.combine(datetime.min, end_time) - datetime.combine(datetime.min, start_time)).total_seconds() / 60
    if total_minutes <= 0:
        return 0
    return int(total_minutes // duration_minutes)


def _infer_end_time(start_time, matches_per_court_per_day, duration_minutes):
    start_dt = datetime.combine(datetime.min, start_time)
    end_dt = start_dt + timedelta(minutes=matches_per_court_per_day * duration_minutes)
    return end_dt.time() if end_dt.date() == start_dt.date() else None


def _parse_match_slots_from_request(request, duration_minutes):
    starts = request.POST.getlist("match_start")
    ends = request.POST.getlist("match_end")
    if not starts and not ends:
        return None, None

    if len(starts) != len(ends):
        return None, "Number of start and end times must match."

    slots = []
    last_end = None
    for idx, (start_str, end_str) in enumerate(zip(starts, ends), start=1):
        start_str = start_str.strip()
        end_str = end_str.strip()
        if not start_str or not end_str:
            return None, f"Match {idx} requires both start and end times."
        try:
            start_time = datetime.strptime(start_str, "%H:%M").time()
            end_time = datetime.strptime(end_str, "%H:%M").time()
        except ValueError:
            return None, f"Match {idx} times must be in HH:MM format."
        if end_time <= start_time:
            return None, f"Match {idx} end time must be after its start time."
        duration = (datetime.combine(datetime.min, end_time) - datetime.combine(datetime.min, start_time)).total_seconds() / 60
        if round(duration) != duration_minutes:
            return None, f"Match {idx} must be exactly {duration_minutes} minutes long."
        if last_end and start_time <= last_end:
            return None, f"Match {idx} must start after the previous match ends."
        slots.append((start_time, end_time))
        last_end = end_time

    return slots, None


def _estimate_availability_end_date(start_date, weekdays, daily_slots, required_matches, max_days=365 * 2):
    remaining = required_matches
    current = start_date
    while remaining > 0 and (current - start_date).days <= max_days:
        if current.weekday() in weekdays:
            remaining -= daily_slots
            if remaining <= 0:
                return current
        current += timedelta(days=1)
    return None


def _build_capacity_by_date(start_date, weekdays, daily_capacity, max_days=365 * 2):
    """Build a date->capacity map for recurring weekday availability."""
    capacity = {}
    current = start_date
    for _ in range(max_days + 1):
        if current.weekday() in weekdays:
            capacity[current] = daily_capacity
        current += timedelta(days=1)
    return capacity


@login_required
@require_POST
def estimate_court_availability_end_date(request, pk):
    if not _is_organizer(request.user):
        return JsonResponse({"status": "error", "message": "Not authorized."}, status=403)
    tournament = get_object_or_404(Tournament, pk=pk)
    form = CourtAvailabilityForm(request.POST, tournament=tournament)
    if not form.is_valid():
        error_message = "Please correct the availability details and try again."
        return JsonResponse({"status": "error", "message": error_message, "errors": form.errors}, status=400)

    courts = list(form.cleaned_data["courts"])
    weekdays = [int(day) for day in form.cleaned_data["weekdays"]]
    start_time = form.cleaned_data["start_time"]
    matches_per_court_per_day = form.cleaned_data.get("matches_per_court_per_day")
    start_date = form.cleaned_data.get("start_date") or tournament.start_date or timezone.localdate()
    if not courts:
        return JsonResponse({"status": "error", "message": "Select at least one court."}, status=400)
    if not weekdays:
        return JsonResponse({"status": "error", "message": "Select at least one weekday."}, status=400)

    duration = max(1, tournament.default_match_duration or 35)
    match_slots, parse_error = _parse_match_slots_from_request(request, duration)
    if parse_error:
        return JsonResponse({"status": "error", "message": parse_error}, status=400)
    if match_slots is not None:
        daily_slots_per_court = len(match_slots)
    else:
        inferred_end_time = _infer_end_time(start_time, matches_per_court_per_day, duration)
        if inferred_end_time is None:
            return JsonResponse({"status": "error", "message": "The selected number of matches does not fit in a single day from the chosen start time."}, status=400)
        daily_slots_per_court = matches_per_court_per_day

    active_count = active_participant_count(tournament)
    team_count = active_count or tournament.expected_teams_count or 0
    if team_count < 2:
        return JsonResponse({"status": "error", "message": "Need at least 2 participants or teams in the tournament to estimate an end date. Add entries or set the expected count."}, status=400)

    required_matches = estimate_required_matches(tournament, team_count=team_count)
    weekly_slots = daily_slots_per_court * len(courts) * len(weekdays)
    if weekly_slots <= 0:
        return JsonResponse({"status": "error", "message": "The selected schedule does not produce any available slots."}, status=400)

    estimated_end_date = estimate_completion_date(
        tournament,
        team_count=team_count,
        capacity_by_date=_build_capacity_by_date(
            start_date,
            weekdays,
            daily_slots_per_court * len(courts),
        ),
        start_date=start_date,
    )
    if not estimated_end_date:
        return JsonResponse({"status": "error", "message": "Could not estimate an end date from the selected availability. Try a longer daily window or more weekdays."}, status=400)

    weeks_needed = math.ceil(required_matches / max(1, weekly_slots))
    message = (
        f"Estimated end date: {estimated_end_date} — {required_matches} match{'' if required_matches == 1 else 'es'} requires about {weeks_needed} week{'' if weeks_needed == 1 else 's'} of selected availability. "
        f"Using {daily_slots_per_court} slot{'' if daily_slots_per_court == 1 else 's'} per court per day across {len(courts)} court{'' if len(courts) == 1 else 's'} and {len(weekdays)} weekday{'' if len(weekdays) == 1 else 's'}."
    )
    if active_count == 0 and tournament.expected_teams_count:
        message += f" Using expected count of {tournament.expected_teams_count}."

    return JsonResponse({
        "status": "ok",
        "message": message,
        "estimated_end_date": str(estimated_end_date),
        "required_matches": required_matches,
        "weekly_slots": weekly_slots,
        "daily_slots_per_court": daily_slots_per_court,
    })


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
        end_time = form.cleaned_data.get("end_time")
        start_date = form.cleaned_data.get("start_date")
        end_date = form.cleaned_data.get("end_date")
        additional_start_times = form.cleaned_data.get("additional_start_times")
        is_active = form.cleaned_data.get("is_active", False)
        duration = max(1, tournament.default_match_duration or 35)
        match_slots, parse_error = _parse_match_slots_from_request(request, duration)
        if parse_error:
            messages.error(request, parse_error)
            return redirect("tournament_config", pk=pk)

        if match_slots is not None:
            start_time = match_slots[0][0]
            additional_start_times = ", ".join(slot[0].strftime("%H:%M") for slot in match_slots[1:])
            end_time = match_slots[-1][1]
            matches_per_court_per_day = len(match_slots)
        else:
            matches_per_court_per_day = form.cleaned_data.get("matches_per_court_per_day") or 1
            if end_time is None:
                inferred = _infer_end_time(start_time, matches_per_court_per_day, duration)
                if inferred is None:
                    messages.error(
                        request,
                        "The selected number of matches does not fit in a single day from the chosen start time."
                    )
                    return redirect("tournament_config", pk=pk)
                end_time = inferred

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
                    additional_start_times=additional_start_times or "",
                    matches_per_court_per_day=matches_per_court_per_day,
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

        if created_count and tournament.matches.exists():
            _assign_schedule_to_existing(tournament, knockout_only=True)
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
        if tournament.matches.exists():
            _assign_schedule_to_existing(tournament, knockout_only=True)
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
        if Team.objects.filter(name__iexact=team_name).exists():
            messages.warning(request, f"Team '{team_name}' already exists, skipped.")
            continue
        # Enforce registration limit
        if tournament.expected_teams_count:
            current_count = active_participant_count(tournament)
            if current_count >= tournament.expected_teams_count:
                messages.warning(
                    request,
                    f"Registration limit of {tournament.expected_teams_count} {tournament.participant_label_plural.lower()} reached. '{team_name}' and subsequent entries were skipped.",
                )
                break
        user = User.objects.create_user(username=username, password=password)
        team = Team.objects.create(name=team_name)
        TeamTournamentParticipation.objects.create(team=team, tournament=tournament, status="active")
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
    team_count = active_participant_count(tournament)
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

    start = tournament.start_date or timezone.localdate()
    if court_count > 0:
        estimated_end = estimate_completion_date(tournament, team_count=team_count, start_date=start)
    else:
        estimated_end = None

    if not estimated_end:
        matches_per_day = max(1, matches_per_court_per_day * max(1, court_count))
        days_needed = math.ceil(required_matches / matches_per_day)
        estimated_end = start + timedelta(days=days_needed - 1)
    else:
        days_needed = max(1, (estimated_end - start).days + 1)
        matches_per_day = max(1, matches_per_court_per_day * max(1, court_count))

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
def remove_team_from_tournament(request, pk, participation_pk):
    """Organiser removes a team/individual registration from a tournament."""
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    participation = get_object_or_404(TeamTournamentParticipation, pk=participation_pk, tournament=tournament)
    team_name = participation.team.name

    # For individual-mode tournaments, also remove the corresponding individual registration
    if tournament.registration_mode == "individual" and participation.team.is_internal:
        TournamentIndividualRegistration.objects.filter(
            shadow_team=participation.team, tournament=tournament
        ).delete()

    participation.delete()
    log_action(
        request,
        "team_removed_from_tournament",
        f"Removed '{team_name}' from '{tournament.name}'",
        tournament=tournament,
    )
    messages.success(request, f"'{team_name}' has been removed from the tournament.")
    return redirect("tournament_config", pk=pk)


@login_required
@require_POST
def open_registration(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    reopening = tournament.status == "scheduled"
    if reopening:
        # Clear the draft schedule so it isn't stale after new registrations
        deleted_count, _ = tournament.matches.all().delete()
        log_action(request, "schedule_cleared", f"Draft schedule cleared ({deleted_count} matches) to re-open registration for '{tournament.name}'", tournament=tournament)
    tournament.status = "registration_open"
    tournament.save(update_fields=["status"])
    log_action(request, "registration_opened", f"Registration {'re-opened' if reopening else 'opened'} for '{tournament.name}'", tournament=tournament)
    if reopening:
        messages.success(request, "Registration re-opened. The previous draft schedule has been cleared — regenerate the schedule once you close registration again.")
    else:
        messages.success(request, "Registration is now open.")
    return redirect("tournament_config", pk=pk)


@login_required
@require_POST
def close_registration(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)

    errors = []

    # Block on pending (incomplete) teams in team-mode
    if tournament.registration_mode != "individual":
        pending_qs = TeamTournamentParticipation.objects.filter(
            tournament=tournament, status="pending"
        ).select_related("team")
        if pending_qs.exists():
            names = ", ".join(f"'{p.team.name}'" for p in pending_qs[:5])
            extra = f" (+{pending_qs.count() - 5} more)" if pending_qs.count() > 5 else ""
            errors.append(
                f"{pending_qs.count()} team(s) are still forming (incomplete roster): {names}{extra}. "
                f"Each team needs {tournament.players_per_team} players. Remove incomplete teams or wait for their rosters to fill before closing registration."
            )

    active_count = active_participant_count(tournament)
    if active_count < 2:
        if tournament.registration_mode == "individual":
            errors.append("Need at least 2 active participants before closing registration.")
        else:
            errors.append("Need at least 2 active teams before closing registration.")
    if tournament.expected_teams_count and active_count != tournament.expected_teams_count:
        if tournament.registration_mode == "individual":
            errors.append(
                f"Registered participants ({active_count}) must match the expected participant count ({tournament.expected_teams_count}) before closing registration."
            )
        else:
            errors.append(
                f"Registered teams ({active_count}) must match the expected team count ({tournament.expected_teams_count}) before closing registration."
            )

    if tournament.registration_mode != "individual":
        required_players = max(1, tournament.players_per_team or 1)
        roster_mismatch = []
        for participation in TeamTournamentParticipation.objects.filter(
            tournament=tournament,
            status="active",
            team__is_internal=False,
        ).select_related("team"):
            count = participation.team.memberships.count()
            if count != required_players:
                roster_mismatch.append((participation.team.name, count))
        if roster_mismatch:
            team_names = ", ".join(f"{name} ({count})" for name, count in roster_mismatch[:5])
            errors.append(
                f"Each team must have exactly {required_players} members before closing registration. "
                f"Mismatched teams: {team_names}."
            )

    if errors:
        for error in errors:
            messages.error(request, error)
        return redirect("tournament_config", pk=pk)

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
               f"{tournament.team_participations.filter(status='active').count()} teams",
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
        if not _is_user_enrolled_in_tournament(request.user, tournament):
            messages.error(request, "You are not enrolled in that tournament.")
            return redirect(next_url)

    request.session["selected_tournament_id"] = tournament.pk
    messages.success(request, f"Now viewing '{tournament.name}'.")
    return redirect(next_url)


@login_required
def test_maker_view(request):
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can access Test Maker.")
        return redirect("dashboard")

    tournament = _get_tournament(request)
    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        actions_without_tournament = {"create_user_team_pool"}
        if not tournament and action not in actions_without_tournament:
            messages.error(request, "No tournament selected. Create/select a tournament first.")
            return redirect("test_maker")

        def _next_unique_username(base_username):
            if not User.objects.filter(username=base_username).exists():
                return base_username
            suffix = 1
            while User.objects.filter(username=f"{base_username}_{suffix}").exists():
                suffix += 1
            return f"{base_username}_{suffix}"

        def _next_unique_display_name(base_name, tournament_obj):
            if not tournament_obj.individual_registrations.filter(display_name__iexact=base_name).exists():
                return base_name
            suffix = 1
            while tournament_obj.individual_registrations.filter(
                display_name__iexact=f"{base_name}_{suffix}"
            ).exists():
                suffix += 1
            return f"{base_name}_{suffix}"

        if action == "create_user_team_pool":
            user_count_raw = request.POST.get("pool_user_count", "50")
            team_count_raw = request.POST.get("pool_team_count", "25")
            members_raw = request.POST.get("pool_members_per_team", "2")
            user_prefix = (request.POST.get("pool_user_prefix") or "tm_user_").strip() or "tm_user_"
            team_prefix = (request.POST.get("pool_team_prefix") or "tm_team_").strip() or "tm_team_"
            pool_password = request.POST.get("pool_password") or "pass123"

            try:
                user_count = max(1, int(user_count_raw))
                team_count = max(0, int(team_count_raw))
                members_per_team = max(1, int(members_raw))
            except ValueError:
                messages.error(request, "Pool counts and members per team must be valid numbers.")
                return redirect("test_maker")

            created_users = 0
            reused_users = 0
            created_teams = 0
            created_memberships = 0

            pool_users = []
            user_width = max(3, len(str(max(user_count, team_count * members_per_team))))
            for idx in range(1, user_count + 1):
                username = f"{user_prefix}{idx:0{user_width}d}"
                user = User.objects.filter(username=username).first()
                if user is None:
                    user = User.objects.create_user(
                        username=username,
                        password=pool_password,
                        first_name=f"U{idx:0{user_width}d}",
                    )
                    created_users += 1
                else:
                    reused_users += 1
                pool_users.append(user)

            required_members = team_count * members_per_team
            next_user_idx = len(pool_users) + 1
            while len(pool_users) < required_members:
                username = _next_unique_username(f"{user_prefix}{next_user_idx:0{user_width}d}")
                user = User.objects.create_user(
                    username=username,
                    password=pool_password,
                    first_name=f"U{next_user_idx:0{user_width}d}",
                )
                created_users += 1
                pool_users.append(user)
                next_user_idx += 1

            if tournament:
                sport_type = tournament.sport_type
            else:
                sport_type = "football"

            team_width = max(3, len(str(max(1, team_count))))
            for idx in range(1, team_count + 1):
                team_name = f"{team_prefix}{idx:0{team_width}d}"
                team, team_created = Team.objects.get_or_create(
                    name=team_name,
                    defaults={"sport_type": sport_type, "is_internal": False},
                )
                if team_created:
                    created_teams += 1

                start = (idx - 1) * members_per_team
                members = pool_users[start:start + members_per_team]
                for member_idx, user in enumerate(members, start=1):
                    role = "captain" if member_idx == 1 else "member"
                    membership, membership_created = TeamMembership.objects.get_or_create(
                        team=team,
                        user=user,
                        defaults={"role": role},
                    )
                    if not membership_created and member_idx == 1 and membership.role != "captain":
                        membership.role = "captain"
                        membership.save(update_fields=["role"])
                    if membership_created:
                        created_memberships += 1
                    Player.objects.get_or_create(team=team, name=user.username)

            summary = (
                f"Pool ready: {created_users} user(s) created, {reused_users} reused, "
                f"{created_teams} team(s) created, {created_memberships} membership(s) created."
            )
            log_action(
                request,
                "test_maker_create_user_team_pool",
                summary,
                tournament=tournament,
            )
            messages.success(request, summary)

        elif action == "create_test_teams":
            team_count_raw = request.POST.get("team_count", "10")
            members_raw = request.POST.get("members_per_team") or str(tournament.players_per_team or 2)
            team_prefix = (request.POST.get("team_prefix") or "team").strip() or "team"
            username_prefix = (request.POST.get("username_prefix") or "t").strip() or "t"
            default_password = request.POST.get("default_password") or "pass123"

            try:
                team_count = max(1, int(team_count_raw))
                members_per_team = max(1, int(members_raw))
            except ValueError:
                messages.error(request, "Team count and members per team must be valid numbers.")
                return redirect("test_maker")

            if tournament.registration_mode == "individual":
                created_regs = 0
                created_users = 0
                created_shadow_teams = 0
                for idx in range(1, team_count + 1):
                    display_name = f"{team_prefix}{idx}"[:100]
                    if tournament.individual_registrations.filter(display_name__iexact=display_name).exists():
                        continue
                    captain_username = _next_unique_username(f"{username_prefix}{idx}p1")
                    captain = User.objects.create_user(
                        username=captain_username,
                        password=default_password,
                        first_name=display_name,
                    )
                    created_users += 1
                    reg = TournamentIndividualRegistration.objects.create(
                        tournament=tournament,
                        user=captain,
                        display_name=display_name,
                        status="active",
                    )
                    created_regs += 1
                    shadow = _ensure_shadow_team_for_registration(reg, tournament.sport_type)
                    if shadow:
                        created_shadow_teams += 1
                log_action(
                    request,
                    "test_maker_create_teams",
                    (
                        f"Individual test data: {created_regs} registration(s), {created_users} user(s), "
                        f"{created_shadow_teams} shadow competitor(s)"
                    ),
                    tournament=tournament,
                )
                messages.success(
                    request,
                    (
                        f"Test participants created: {created_regs} registration(s), {created_users} user(s), "
                        f"{created_shadow_teams} internal competitor(s)."
                    ),
                )
            else:
                created_teams = 0
                created_users = 0
                created_memberships = 0
                created_participations = 0

                for idx in range(1, team_count + 1):
                    team_name = f"{team_prefix}{idx}"
                    if TeamTournamentParticipation.objects.filter(
                        team__name=team_name,
                        tournament=tournament,
                    ).exists():
                        continue

                    captain_username = _next_unique_username(f"{username_prefix}{idx}p1")
                    captain = User.objects.create_user(
                        username=captain_username,
                        password=default_password,
                        first_name=team_name,
                    )
                    created_users += 1

                    team, team_created = Team.objects.get_or_create(
                        name=team_name, defaults={"sport_type": tournament.sport_type}
                    )
                    if team_created:
                        created_teams += 1

                    _, participation_created = TeamTournamentParticipation.objects.get_or_create(
                        team=team,
                        tournament=tournament,
                        defaults={"status": "active"},
                    )
                    if participation_created:
                        created_participations += 1

                    TeamMembership.objects.create(team=team, user=captain, role="captain")
                    created_memberships += 1
                    Player.objects.get_or_create(team=team, name=captain_username)

                    for member_idx in range(2, members_per_team + 1):
                        member_username = _next_unique_username(f"{username_prefix}{idx}p{member_idx}")
                        member = User.objects.create_user(
                            username=member_username,
                            password=default_password,
                            first_name=team_name,
                        )
                        created_users += 1
                        TeamMembership.objects.create(team=team, user=member, role="member")
                        created_memberships += 1
                        Player.objects.get_or_create(team=team, name=member_username)

                log_action(
                    request,
                    "test_maker_create_teams",
                    (
                        f"Created {created_teams} team(s), {created_users} user(s), "
                        f"{created_memberships} membership(s), {created_participations} participation(s)"
                    ),
                    tournament=tournament,
                )
                messages.success(
                    request,
                    (
                        f"Test data created: {created_teams} team(s), {created_users} user(s), "
                        f"{created_memberships} membership(s), {created_participations} participation(s)."
                    ),
                )
        elif action == "register_to_open_tournament":
            if tournament.status != "registration_open":
                messages.error(request, "Tournament must have status 'Registration Open' to use this action.")
                return redirect("test_maker")

            reg_count_raw = request.POST.get("reg_count", "5")
            reg_prefix = (request.POST.get("reg_prefix") or ("p" if tournament.registration_mode == "individual" else "rteam")).strip()
            reg_username_prefix = (request.POST.get("reg_username_prefix") or "r").strip() or "r"
            reg_password = request.POST.get("reg_password") or "pass123"

            try:
                reg_count = max(1, int(reg_count_raw))
            except ValueError:
                messages.error(request, "Count must be a valid number.")
                return redirect("test_maker")

            created_users = 0
            created_regs = 0
            created_teams = 0
            created_participations = 0

            if tournament.registration_mode == "individual":
                for idx in range(1, reg_count + 1):
                    display_name = f"{reg_prefix}{idx}"[:100]
                    if tournament.individual_registrations.filter(display_name__iexact=display_name).exists():
                        continue
                    username = _next_unique_username(f"{reg_username_prefix}{idx}")
                    user = User.objects.create_user(
                        username=username,
                        password=reg_password,
                        first_name=display_name,
                    )
                    created_users += 1
                    ind_reg = TournamentIndividualRegistration.objects.create(
                        tournament=tournament,
                        user=user,
                        display_name=display_name,
                        status="active",
                    )
                    created_regs += 1
                    shadow = _ensure_shadow_team_for_registration(ind_reg, tournament.sport_type)
                    if shadow:
                        created_participations += 1
            else:
                members_per_team = max(1, int(request.POST.get("reg_members_per_team") or tournament.players_per_team or 1))
                for idx in range(1, reg_count + 1):
                    team_name = f"{reg_prefix}{idx}"
                    if TeamTournamentParticipation.objects.filter(tournament=tournament, team__name=team_name).exists():
                        continue
                    captain_username = _next_unique_username(f"{reg_username_prefix}{idx}p1")
                    captain = User.objects.create_user(
                        username=captain_username,
                        password=reg_password,
                        first_name=team_name,
                    )
                    created_users += 1
                    team, team_created = Team.objects.get_or_create(
                        name=team_name,
                        defaults={"sport_type": tournament.sport_type},
                    )
                    if team_created:
                        created_teams += 1
                    TeamMembership.objects.get_or_create(team=team, user=captain, defaults={"role": "captain"})
                    _, part_created = TeamTournamentParticipation.objects.get_or_create(
                        team=team,
                        tournament=tournament,
                        defaults={"status": "active"},
                    )
                    if part_created:
                        created_regs += 1
                        created_participations += 1
                    for member_idx in range(2, members_per_team + 1):
                        member_username = _next_unique_username(f"{reg_username_prefix}{idx}p{member_idx}")
                        member = User.objects.create_user(
                            username=member_username,
                            password=reg_password,
                            first_name=team_name,
                        )
                        created_users += 1
                        TeamMembership.objects.create(team=team, user=member, role="member")

            if tournament.registration_mode == "individual":
                summary = (
                    f"Registered {created_regs} individual(s), {created_users} user(s), "
                    f"{created_participations} shadow competitor(s)."
                )
            else:
                summary = (
                    f"Registered {created_regs} team(s), {created_teams} new team(s) created, "
                    f"{created_users} user(s), {created_participations} participation(s)."
                )
            log_action(request, "test_maker_register_to_open", summary, tournament=tournament)
            messages.success(request, summary)

        elif action == "register_existing_to_open_tournament":
            if tournament.status != "registration_open":
                messages.error(request, "Tournament must have status 'Registration Open' to use this action.")
                return redirect("test_maker")

            existing_count_raw = request.POST.get("existing_count", "5")
            try:
                existing_count = max(1, int(existing_count_raw))
            except ValueError:
                messages.error(request, "Count must be a valid number.")
                return redirect("test_maker")

            registered = 0
            skipped = 0
            created_shadows = 0

            if tournament.registration_mode == "individual":
                candidates = list(
                    User.objects.exclude(
                        individual_registrations__tournament=tournament
                    ).order_by("username", "id")[:existing_count]
                )
                for user in candidates:
                    base_name = (user.first_name or user.username or "participant").strip()[:100] or "participant"
                    display_name = _next_unique_display_name(base_name, tournament)[:100]
                    ind_reg = TournamentIndividualRegistration.objects.create(
                        tournament=tournament,
                        user=user,
                        display_name=display_name,
                        status="active",
                    )
                    registered += 1
                    shadow = _ensure_shadow_team_for_registration(ind_reg, tournament.sport_type)
                    if shadow:
                        created_shadows += 1
                skipped = max(0, existing_count - len(candidates))
                summary = (
                    f"Registered {registered} existing user(s) as individuals; "
                    f"created {created_shadows} shadow competitor(s)."
                )
            else:
                candidates = list(
                    Team.objects.filter(is_internal=False)
                    .exclude(participations__tournament=tournament)
                    .order_by("name", "id")[:existing_count]
                )
                for team in candidates:
                    _, created = TeamTournamentParticipation.objects.get_or_create(
                        team=team,
                        tournament=tournament,
                        defaults={"status": "active"},
                    )
                    if created:
                        registered += 1
                    else:
                        skipped += 1
                skipped += max(0, existing_count - len(candidates))
                summary = f"Registered {registered} existing team(s) to tournament."

            if skipped > 0:
                summary = f"{summary} Skipped {skipped} slot(s) due to unavailable candidates."

            log_action(request, "test_maker_register_existing", summary, tournament=tournament)
            messages.success(request, summary)

        elif action == "randomize_court_preferences":
            if tournament.registration_mode == "individual":
                messages.warning(
                    request,
                    "Court preference randomization is team-level and is skipped for individual-mode tournaments.",
                )
                return redirect("test_maker")
            teams = list(
                Team.objects.filter(
                    participations__tournament=tournament,
                    participations__status="active",
                    is_internal=False,
                ).distinct().order_by("id")
            )
            courts = list(tournament.courts.filter(is_available=True).order_by("id"))
            if not courts:
                courts = list(tournament.courts.order_by("id"))

            if not teams:
                messages.warning(request, "No teams found in selected tournament.")
                return redirect("test_maker")
            if not courts:
                messages.warning(request, "No courts available to assign preferences.")
                return redirect("test_maker")

            for team in teams:
                pick_count = random.randint(1, min(3, len(courts)))
                picked = random.sample(courts, pick_count)
                participation, _ = TeamTournamentParticipation.objects.get_or_create(
                    team=team,
                    tournament=tournament,
                    defaults={"status": "active"},
                )
                TeamTournamentCourtPreference.objects.filter(participation=participation).delete()
                TeamTournamentCourtPreference.objects.bulk_create([
                    TeamTournamentCourtPreference(participation=participation, court=court)
                    for court in picked
                ])

            log_action(
                request,
                "test_maker_randomize_courts",
                f"Randomized court preferences for {len(teams)} team(s)",
                tournament=tournament,
            )
            messages.success(request, f"Randomized court preferences for {len(teams)} team(s).")

        elif action == "randomize_scores":
            limit_raw = request.POST.get("match_count", "10")
            try:
                limit = max(1, int(limit_raw))
            except ValueError:
                messages.error(request, "Match count must be a valid number.")
                return redirect("test_maker")

            terminal_statuses = ["confirmed", "forfeited", "cancelled", "bye"]
            matches = list(
                tournament.matches.filter(team1__isnull=False, team2__isnull=False)
                .exclude(status__in=terminal_statuses)
                .order_by("match_number", "id")[:limit]
            )

            if not matches:
                messages.warning(request, "No eligible matches found for score randomization.")
                return redirect("test_maker")

            updated = 0
            for match in matches:
                s1 = random.randint(0, 5)
                s2 = random.randint(0, 5)
                if s1 == s2:
                    if random.random() < 0.5:
                        s1 += 1
                    else:
                        s2 += 1

                match.score_team1 = s1
                match.score_team2 = s2
                match.winner = match.team1 if s1 > s2 else match.team2
                match.status = "confirmed"
                match.submitted_by = None
                match.confirmed_by = None
                match.save(update_fields=[
                    "score_team1", "score_team2", "winner", "status", "submitted_by", "confirmed_by"
                ])
                advance_winner(match)
                updated += 1

            log_action(
                request,
                "test_maker_randomize_scores",
                f"Randomized and confirmed scores for {updated} match(es)",
                tournament=tournament,
            )
            messages.success(request, f"Randomized and confirmed scores for {updated} match(es).")

        elif action == "randomize_schedule":
            limit_raw = request.POST.get("schedule_count", "20")
            try:
                limit = max(1, int(limit_raw))
            except ValueError:
                messages.error(request, "Schedule count must be a valid number.")
                return redirect("test_maker")

            courts = list(tournament.courts.filter(is_available=True).order_by("id"))
            if not courts:
                courts = list(tournament.courts.order_by("id"))
            if not courts:
                messages.warning(request, "No courts found. Add courts before random scheduling.")
                return redirect("test_maker")

            matches = list(
                tournament.matches.filter(team1__isnull=False, team2__isnull=False, scheduled_time__isnull=True)
                .exclude(status__in=["confirmed", "forfeited", "cancelled", "bye"])
                .order_by("match_number", "id")[:limit]
            )
            if not matches:
                messages.warning(request, "No unscheduled eligible matches found.")
                return redirect("test_maker")

            duration_minutes = max(5, int(tournament.default_match_duration or 30))
            start_at = timezone.now().replace(second=0, microsecond=0) + timedelta(hours=1)

            updated = 0
            for idx, match in enumerate(matches):
                slot_start = start_at + timedelta(minutes=idx * duration_minutes)
                slot_end = slot_start + timedelta(minutes=duration_minutes)
                match.scheduled_time = slot_start
                match.scheduled_end_time = slot_end
                match.court = courts[idx % len(courts)]
                if match.status not in ["in_progress", "pending_confirmation"]:
                    match.status = "upcoming"
                match.save(update_fields=["scheduled_time", "scheduled_end_time", "court", "status"])
                updated += 1

            log_action(
                request,
                "test_maker_randomize_schedule",
                f"Randomly scheduled {updated} match(es)",
                tournament=tournament,
            )
            messages.success(request, f"Randomly scheduled {updated} match(es).")

        elif action == "set_dispute_window":
            minutes_raw = request.POST.get("dispute_window_minutes", "")
            try:
                minutes = max(1, int(minutes_raw))
            except (ValueError, TypeError):
                messages.error(request, "Dispute window must be a valid number of minutes.")
                return redirect("test_maker")
            import core.views as _self
            _self.DEFAULT_DISPUTE_WINDOW_MINUTES = minutes
            _self.CRITICAL_STAGE_DISPUTE_WINDOW_MINUTES = minutes
            log_action(
                request,
                "test_maker_set_dispute_window",
                f"Dispute window set to {minutes} minute(s) by {request.user.username}",
                tournament=tournament,
            )
            messages.success(request, f"Dispute window updated to {minutes} minute(s) (takes effect on next score submission).")

        else:
            messages.error(request, "Unknown Test Maker action.")

        return redirect("test_maker")

    roster_count = 0
    roster_label = "Teams"
    if tournament:
        if tournament.registration_mode == "individual":
            roster_count = active_participant_count(tournament)
            roster_label = "Participants"
        else:
            roster_count = active_participant_count(tournament)
            roster_label = "Teams"
    available_existing_users = 0
    available_existing_teams = Team.objects.filter(is_internal=False).count()
    if tournament:
        if tournament.registration_mode == "individual":
            available_existing_users = User.objects.exclude(
                individual_registrations__tournament=tournament
            ).count()
        available_existing_teams = Team.objects.filter(is_internal=False).exclude(
            participations__tournament=tournament
        ).count()

    context = {
        "tournament": tournament,
        "registration_mode": tournament.registration_mode if tournament else "team",
        "total_teams": roster_count,
        "roster_label": roster_label,
        "total_courts": tournament.courts.count() if tournament else 0,
        "total_matches": tournament.matches.count() if tournament else 0,
        "pending_matches": (
            tournament.matches.exclude(status__in=["confirmed", "forfeited", "cancelled", "bye"]).count()
            if tournament else 0
        ),
        "available_existing_users": available_existing_users,
        "available_existing_teams": available_existing_teams,
        "dispute_window_minutes": DEFAULT_DISPUTE_WINDOW_MINUTES,
        **_tournament_context(request, tournament),
    }
    return render(request, "core/test_maker.html", context)


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
    teams_qs = Team.objects.filter(participations__tournament=tournament)
    if not _is_organizer(request.user):
        teams_qs = teams_qs.filter(is_internal=False)
    teams = teams_qs.distinct()
    courts = tournament.courts.all()
    groups = sorted(set(tournament.team_participations.exclude(group="").values_list("group", flat=True)))
    team_ids = {
        m.team1_id for m in matches if m.team1_id
    } | {
        m.team2_id for m in matches if m.team2_id
    } | {
        m.winner_id for m in matches if m.winner_id
    }
    team_name_map = _team_display_map(tournament, team_ids)
    context = {
        "tournament": tournament,
        "matches": matches,
        "team_name_map": team_name_map,
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
        "team": _get_team(request.user, tournament),
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
        and match.submitted_by != request.user
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
    team_name_map = _team_display_map(
        match.tournament,
        [match.team1_id, match.team2_id, match.winner_id],
    )

    team1_label = team_name_map.get(match.team1_id, match.team1.name if match.team1 else "TBD")
    team2_label = team_name_map.get(match.team2_id, match.team2.name if match.team2 else "TBD")
    winner_label = team_name_map.get(match.winner_id, match.winner.name if match.winner else "")

    context = {
        "match": match,
        "team1_label": team1_label,
        "team2_label": team2_label,
        "winner_label": winner_label,
        "team": team,
        "tournament": match.tournament,
        "is_participant": is_participant,
        "can_submit": can_submit,
        "can_confirm": can_confirm,
        "can_dispute": can_dispute,
        "dispute_window_open": dispute_window_open,
        "is_critical_stage": is_critical_stage,
        "dispute_window_minutes": _dispute_window_minutes_for_match(match),
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
    if match.tournament.status == "paused" and not is_organizer:
        messages.error(request, "The tournament is currently paused. Score submission is not allowed.")
        return redirect("match_detail", pk=pk)
    # Organizers can submit scores in both active and paused; participants only when active
    allowed_tournament_statuses = ("active", "paused") if is_organizer else ("active",)
    if match.tournament.status not in allowed_tournament_statuses:
        messages.error(request, "Scores can only be submitted once the tournament has started.")
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
            match.submitted_by = request.user
            match.confirmed_by = None
            submitted_at = timezone.now()
            window_minutes = _dispute_window_minutes_for_match(match)
            match.score_submitted_at = submitted_at
            match.dispute_deadline_at = submitted_at + timedelta(minutes=window_minutes)
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
                    f"Score submitted. Opponent has {CRITICAL_STAGE_DISPUTE_WINDOW_MINUTES} minute(s) to dispute before auto-lock."
                )
            else:
                messages.success(
                    request,
                    f"Score submitted. Opponent has {DEFAULT_DISPUTE_WINDOW_MINUTES} minute(s) to dispute before auto-lock."
                )
    return redirect("match_detail", pk=pk)


@login_required
@require_POST
def confirm_score(request, pk):
    match = get_object_or_404(Match, pk=pk)
    _expire_pending_score_disputes(match.tournament)
    match.refresh_from_db()
    team = _get_team(request.user, match.tournament)
    if not team or match.submitted_by == request.user:
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
    if not _lock_match_score(match, confirmed_by_user=request.user):
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
    if not team or match.submitted_by == request.user:
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
    match.disputed_by = request.user
    match.critical_dispute = _is_critical_stage_match(match)
    prefix = "CRITICAL-STAGE DISPUTE" if match.critical_dispute else "DISPUTED"
    match.notes = f"{prefix} by {request.user.username}: {dispute_note}" if dispute_note else f"{prefix} by {request.user.username}"
    match.save()
    log_action(request, "score_disputed",
               f"Score disputed for {match} by {request.user.username}: {dispute_note}",
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
        if not _lock_match_score(match, confirmed_by_user=None):
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
    if match.tournament.status != "active":
        messages.error(request, "Rescheduling is not available until the tournament has started.")
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
            match=match, requested_by=request.user, new_time=new_dt,
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
    if not team or rr.requested_by == request.user:
        messages.error(request, "Cannot respond to your own request.")
        return redirect("match_detail", pk=match.pk)
    if match.team1 != team and match.team2 != team:
        messages.error(request, "Not a participant.")
        return redirect("match_detail", pk=match.pk)
    if not _is_captain(request.user, team) and not _is_organizer(request.user):
        messages.error(request, "Only the team captain can approve or reject reschedule requests.")
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

        team_ids = set()
        for round_matches in (context.get("bracket") or {}).values():
            for match in round_matches:
                if match.team1_id:
                    team_ids.add(match.team1_id)
                if match.team2_id:
                    team_ids.add(match.team2_id)
                if match.winner_id:
                    team_ids.add(match.winner_id)
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


# -- Teams --

@login_required
def teams_view(request):
    tournament = _get_tournament(request)
    teams = []
    participant_list = []
    is_organizer = _is_organizer(request.user)
    registration_mode = tournament.registration_mode if tournament else "team"
    if tournament:
        if tournament.registration_mode == "individual":
            regs_qs = tournament.individual_registrations.filter(status="active")
            if not is_organizer:
                regs_qs = regs_qs.filter(user=request.user)
            participant_list = list(
                regs_qs
                .select_related("user", "shadow_team")
                .order_by("display_name", "id")
            )
        else:
            teams_qs = Team.objects.filter(
                participations__tournament=tournament,
                participations__status="active",
                is_internal=False,
            )
            if not is_organizer:
                teams_qs = teams_qs.filter(memberships__user=request.user)

            teams = teams_qs.prefetch_related("players").distinct().order_by("name")
            for team in teams:
                participation = team.participations.filter(tournament=tournament).first()
                team.group = participation.group if participation else ""

    teams_colspan = 3  # Name, Status, Account/Actions
    if tournament and tournament.players_per_team > 1:
        teams_colspan += 2  # Department, Players
    if tournament and tournament.format == "hybrid":
        teams_colspan += 1  # Group

    return render(request, "core/teams.html", {
        "tournament": tournament, "teams": teams,
        "participant_list": participant_list,
        "registration_mode": registration_mode,
        "is_organizer": is_organizer,
        "teams_colspan": teams_colspan,
        **_tournament_context(request, tournament),
    })


@login_required
def team_detail(request, pk):
    team = get_object_or_404(Team.objects.prefetch_related("players"), pk=pk)
    if team.is_internal and not _is_organizer(request.user):
        allowed = TournamentIndividualRegistration.objects.filter(
            shadow_team=team, user=request.user, status="active"
        ).exists()
        if not allowed:
            messages.error(request, "That page is not available.")
            return redirect("dashboard")
    selected_tournament = _get_tournament(request)
    tournament = selected_tournament
    if tournament and not TeamTournamentParticipation.objects.filter(team=team, tournament=tournament).exists():
        tournament = None
    if not tournament:
        participation = team.participations.select_related("tournament").order_by("-created_at").first()
        tournament = participation.tournament if participation else None
    matches = Match.objects.filter(tournament=tournament).filter(
        Q(team1=team) | Q(team2=team)
    ).select_related("team1", "team2", "court", "winner").order_by("match_number")
    for match in matches:
        opponent = match.team2 if match.team1_id == team.pk else match.team1
        match.opponent_display = _team_display_label(tournament, opponent) if opponent else "TBD"
    stats = {
        "played": matches.filter(status__in=["confirmed", "forfeited"]).count(),
        "wins": matches.filter(winner=team).count(),
        "upcoming": matches.filter(status__in=["upcoming", "in_progress"]).count(),
    }
    stats["losses"] = stats["played"] - stats["wins"]
    is_organizer = _is_organizer(request.user)
    is_own_team = (_get_team(request.user, tournament) == team) if tournament else (_get_team(request.user) == team)
    is_captain = _is_captain(request.user, team)
    memberships = team.memberships.select_related("user").order_by("role", "joined_at")
    max_members = tournament.players_per_team if tournament else None
    members_full = max_members is not None and memberships.count() >= max_members
    team_heading_label = _team_display_label(tournament, team) if tournament else team.name
    return render(request, "core/team_detail.html", {
        "team": team,
        "team_heading_label": team_heading_label,
        "tournament": tournament, "matches": matches, "stats": stats,
        "players": team.players.all(),
        "is_organizer": is_organizer,
        "is_own_team": is_own_team,
        "is_captain": is_captain,
        "memberships": memberships,
        "members_full": members_full,
        "max_members": max_members,
        "invite_form": TeamMemberInviteForm() if (is_captain or is_organizer) else None,
        "existing_member_form": ExistingTeamMemberForm() if (is_captain or is_organizer) else None,
        **_tournament_context(request, selected_tournament),
    })


@login_required
def manage_team_members(request, pk):
    team = get_object_or_404(Team, pk=pk)
    user_team = _get_team(request.user)
    is_organizer = _is_organizer(request.user)
    if not is_organizer and (user_team != team or not _is_captain(request.user, user_team)):
        messages.error(request, "Only the team captain can manage members.")
        return redirect("team_detail", pk=pk)
    tournament = _get_tournament(request)
    if tournament and not TeamTournamentParticipation.objects.filter(team=team, tournament=tournament).exists():
        tournament = None
    max_members = tournament.players_per_team if tournament else None
    if max_members is not None and team.memberships.count() >= max_members:
        messages.error(request, f"Team is already at the maximum of {max_members} member(s).")
        return redirect("team_detail", pk=pk)
    if request.method == "POST":
        member_action = (request.POST.get("member_action") or "create_account").strip()
        if member_action == "add_existing":
            form = ExistingTeamMemberForm(request.POST)
            if form.is_valid():
                username = form.cleaned_data["username"].strip()
                existing_user = User.objects.filter(username=username).first()
                if not existing_user:
                    messages.error(request, "User not found.")
                    return redirect("team_detail", pk=pk)

                if TeamMembership.objects.filter(team=team, user=existing_user).exists():
                    messages.error(request, f"'{username}' is already in this team.")
                    return redirect("team_detail", pk=pk)

                if tournament and existing_user.memberships.filter(
                    team__participations__tournament=tournament
                ).exclude(team=team).exists():
                    messages.error(
                        request,
                        f"'{username}' is already in another team for this tournament.",
                    )
                    return redirect("team_detail", pk=pk)

                TeamMembership.objects.create(team=team, user=existing_user, role="member")
                _promote_team_participation_when_full(team, tournament=tournament, request=request)
                log_action(
                    request,
                    "existing_team_member_added",
                    f"Existing user '{existing_user.username}' added to team '{team.name}'",
                    tournament=tournament,
                )
                messages.success(request, f"User '{existing_user.username}' added to {team.name}.")
            else:
                for field in form:
                    for error in field.errors:
                        messages.error(request, f"{field.label}: {error}")
        else:
            form = TeamMemberInviteForm(request.POST)
            if form.is_valid():
                new_user = User.objects.create_user(
                    username=form.cleaned_data["username"],
                    password=form.cleaned_data["password"],
                )
                TeamMembership.objects.create(team=team, user=new_user, role="member")
                _promote_team_participation_when_full(team, tournament=tournament, request=request)
                log_action(
                    request,
                    "team_member_added",
                    f"Member '{new_user.username}' added to team '{team.name}'",
                    tournament=tournament,
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
        tournament=_get_tournament(request),
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
    captain_membership = TeamMembership.objects.filter(team=team, role="captain").select_related("user").first()
    if not captain_membership:
        messages.error(request, "No captain found for this team.")
        return redirect("team_detail", pk=pk)
    captain_user = captain_membership.user
    captain_user.set_password(new_password)
    captain_user.save()
    log_action(
        request,
        "captain_password_reset",
        f"Password reset for captain '{captain_user.username}' of team '{team.name}'",
        tournament=_get_tournament(request),
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
    membership.delete()
    log_action(
        request,
        "team_member_removed",
        f"Member '{removed_username}' removed from team '{team.name}' (account preserved)",
        tournament=_get_tournament(request),
    )
    # 12.3: warn if roster drops below tournament minimum
    _check_roster_minimum(team)
    messages.success(request, f"Member '{removed_username}' has been removed from the team.")
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
    tournament = _get_tournament(request)
    if tournament and not TeamTournamentParticipation.objects.filter(team=team, tournament=tournament).exists():
        tournament = None

    if not tournament:
        active_participations = list(
            TeamTournamentParticipation.objects.filter(team=team, status="active")
            .select_related("tournament")
            .order_by("-created_at")
        )
        if len(active_participations) == 1:
            tournament = active_participations[0].tournament
        elif len(active_participations) > 1:
            messages.error(request, "Select the tournament first, then withdraw the team from that tournament.")
            return redirect("team_detail", pk=pk)

    if not tournament:
        messages.error(request, "This team is not actively enrolled in any tournament.")
        return redirect("team_detail", pk=pk)

    participation = TeamTournamentParticipation.objects.filter(team=team, tournament=tournament).first()
    if participation and participation.status == "withdrawn":
        messages.info(request, f"Team '{team.name}' is already withdrawn.")
        return redirect("team_detail", pk=pk)

    if tournament.status == "completed":
        messages.error(request, "Completed tournaments do not allow team withdrawals.")
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

    handle_withdrawal(request, team, tournament)
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
        reported_by=request.user,
        absent_team=opponent,
        present_team=team,
        note=request.POST.get("note", "").strip(),
        deadline_at=timezone.now() + timedelta(days=1),
    )
    log_action(
        request,
        "match_no_show_reported",
        f"No-show reported for {match}. Absent: {opponent.name}, Reporter: {request.user.username}",
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
    from .models import TeamTournamentCourtPreference
    team = get_object_or_404(Team, pk=pk)
    tournament = _get_tournament(request)
    if not tournament:
        participation = team.participations.select_related("tournament").order_by("-created_at").first()
        tournament = participation.tournament if participation else None
    user_team = _get_team(request.user)
    if (team != user_team or not _is_captain(request.user, user_team)) and not _is_organizer(request.user):
        messages.error(request, "Only the team captain or an organizer can update preferences.")
        return redirect("team_detail", pk=pk)
    participation = TeamTournamentParticipation.objects.filter(
        team=team, tournament=tournament
    ).first() if tournament else None
    if request.method == "POST":
        form = TeamPreferencesForm(request.POST, tournament=tournament)
        if form.is_valid() and participation:
            TeamTournamentCourtPreference.objects.filter(participation=participation).delete()
            courts = form.cleaned_data.get("preferred_courts") or []
            TeamTournamentCourtPreference.objects.bulk_create([
                TeamTournamentCourtPreference(participation=participation, court=c) for c in courts
            ])
            participation.availability_notes = form.cleaned_data["availability_notes"]
            participation.save(update_fields=["availability_notes"])
            messages.success(request, "Preferences saved.")
            return redirect("team_detail", pk=pk)
    else:
        current_courts = []
        availability_notes = ""
        if participation:
            current_courts = list(
                TeamTournamentCourtPreference.objects.filter(participation=participation)
                .values_list("court", flat=True)
            )
            availability_notes = participation.availability_notes
        form = TeamPreferencesForm(
            tournament=tournament,
            initial={"preferred_courts": current_courts, "availability_notes": availability_notes},
        )
    team_heading_label = _team_display_label(tournament, team) if tournament else team.name
    return render(request, "core/team_preferences.html", {
        "team": team,
        "team_heading_label": team_heading_label,
        "form": form,
        "tournament": tournament,
        **_tournament_context(request, tournament),
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
            "utilization": round(confirmed / total * 100, 1) if total > 0 else 0,
        })
    team_stats = []
    for team in teams.filter(participations__tournament=tournament, participations__status="active"):
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
            "display_label": _team_display_label(tournament, team),
            "win_rate": round(wins / played * 100, 1) if played > 0 else 0,
        })
    team_stats.sort(key=lambda x: x["win_rate"], reverse=True)
    schedule_density = defaultdict(int)
    for m in matches.filter(scheduled_time__isnull=False):
        day = m.scheduled_time.strftime("%Y-%m-%d")
        schedule_density[day] += 1
    schedule_density = dict(sorted(schedule_density.items()))
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
    recent_logs = AuditLog.objects.filter(tournament=tournament).order_by("-timestamp")[:20]
    context = {
        "tournament": tournament, "match_stats": match_stats, "court_stats": court_stats,
        "team_stats": team_stats, "schedule_density": json.dumps(schedule_density),
        "withdrawal_info": withdrawal_info, "recent_logs": recent_logs,
    }
    if tournament.format in ("round_robin", "double_round_robin", "hybrid"):
        context["standings"] = calculate_standings(tournament)

    active_teams = list(
        teams.filter(
            participations__tournament=tournament,
            participations__status="active",
        ).distinct().order_by("name")
    )
    for team in active_teams:
        team.display_label = _team_display_label(tournament, team)

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
                row_copy["display_label"] = _team_display_label(tournament, row["team"])
                row_copy["point_change"] = 0
                by_team_id[row["team"].pk] = row_copy

            for m in simulator_matches:
                m.team1_label = _team_display_label(tournament, m.team1)
                m.team2_label = _team_display_label(tournament, m.team2)
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
            Q(requested_by=request.user) | Q(match__team1=team) | Q(match__team2=team)
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

    # Delete only tournament-bound data; user accounts remain intact.
    tournament.delete()

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
    is_settings_locked = bool(tournament.started_at or tournament.status in ("active", "completed"))
    if request.method == "POST":
        if is_settings_locked:
            messages.error(request, "Tournament settings are locked after the tournament has started.")
            return redirect("settings")
        form = TournamentForm(request.POST, instance=tournament)
        if form.is_valid():
            t = form.save(commit=False)
            if not t.end_date and t.start_date:
                t.end_date = _auto_end_date(t)
            t.save()
            log_action(request, "settings_updated", "Tournament settings updated", tournament=tournament)
            messages.success(request, "Settings updated.")
            return redirect("settings")
    else:
        form = TournamentForm(instance=tournament)
    return render(request, "core/settings.html", {
        "tournament": tournament,
        "form": form,
        "is_settings_locked": is_settings_locked,
        "users": User.objects.filter(is_superuser=False).order_by("username"),
        "organizer_applications": OrganizerApplication.objects.order_by("-created_at"),
        **_tournament_context(request, tournament),
    })


@login_required
@require_POST
def compute_end_date_view(request, pk):
    """Compute and save an auto end date for the tournament, then redirect back to settings."""
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    computed = _auto_end_date(tournament)
    if computed:
        tournament.end_date = computed
        tournament.save(update_fields=["end_date"])
        messages.success(request, f"End date computed and set to {computed.strftime('%B %d, %Y')}.")
    else:
        messages.error(request, "Could not compute end date — make sure a start date is set.")
    return redirect("settings")


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
    if not make_organizer:
        organizer_count = _organizer_count(exclude_user_id=target.pk)
        if organizer_count < 1:
            messages.error(request, "At least one organizer account is required.")
            return redirect("settings")
    
    from .models import OrganizerProfile
    org_profile, _ = OrganizerProfile.objects.get_or_create(user=target)
    org_profile.verified = make_organizer
    org_profile.save(update_fields=["verified"])

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
    
    from .models import OrganizerProfile
    if hasattr(target, 'organizer_profile') and target.organizer_profile.verified:
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

    team_ids = set()
    for round_matches in (context.get("bracket") or {}).values():
        for match in round_matches:
            if match.team1_id:
                team_ids.add(match.team1_id)
            if match.team2_id:
                team_ids.add(match.team2_id)
            if match.winner_id:
                team_ids.add(match.winner_id)
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


# -- Captain lifecycle --

@login_required
@require_POST
def enter_existing_team_view(request, pk):
    """Captain enters an existing (global) team into a new open tournament."""
    tournament = get_object_or_404(Tournament, pk=pk)
    if tournament.status != "registration_open":
        messages.error(request, "Registration is currently closed for this tournament.")
        return redirect("join_tournament_list")
    if tournament.registration_mode == "individual":
        messages.error(request, "This tournament only accepts individual registrations.")
        return redirect("join_tournament", pk=pk)

    # User must be captain of some team
    captain_membership = request.user.memberships.filter(role="captain").select_related("team").first()
    if not captain_membership:
        messages.error(request, "You are not a captain of any team. Create a new team instead.")
        return redirect("join_tournament", pk=pk)

    team = captain_membership.team

    # Check the team is not already in this tournament
    if TeamTournamentParticipation.objects.filter(team=team, tournament=tournament).exists():
        messages.warning(request, f"'{team.name}' is already registered for this tournament.")
        return redirect("dashboard")

    if _is_user_enrolled_in_tournament(request.user, tournament):
        messages.error(request, "You are already in a team for this tournament.")
        return redirect("join_tournament", pk=pk)

    required_players = max(1, tournament.players_per_team or 1)
    member_count = team.memberships.count()
    if member_count != required_players:
        messages.error(
            request,
            f"'{team.name}' must have exactly {required_players} members to enter this tournament "
            f"(currently {member_count}).",
        )
        return redirect("join_tournament", pk=pk)

    if tournament.expected_teams_count:
        current_count = TeamTournamentParticipation.objects.filter(
            tournament=tournament, status="active", team__is_internal=False
        ).count()
        if current_count >= tournament.expected_teams_count:
            messages.error(
                request,
                f"Registration is full ({current_count}/{tournament.expected_teams_count}).",
            )
            return redirect("join_tournament", pk=pk)

    TeamTournamentParticipation.objects.create(team=team, tournament=tournament, status="active")
    log_action(
        request,
        "team_entered_tournament",
        f"Team '{team.name}' entered tournament '{tournament.name}'",
        tournament=tournament,
    )
    messages.success(request, f"'{team.name}' has been entered into '{tournament.name}'!")
    return redirect("dashboard")


@login_required
@require_POST
def leave_team_view(request, pk):
    """A non-captain member leaves their team."""
    team = get_object_or_404(Team, pk=pk)
    membership = TeamMembership.objects.filter(team=team, user=request.user).first()
    if not membership:
        messages.error(request, "You are not a member of this team.")
        return redirect("dashboard")
    if membership.role == "captain":
        messages.error(request, "Captains cannot leave — transfer captaincy or delete the team first.")
        return redirect("team_detail", pk=pk)
    membership.delete()
    log_action(
        request,
        "team_left",
        f"User '{request.user.username}' left team '{team.name}'",
        tournament=_get_tournament(request),
    )
    # 12.3: warn if roster drops below tournament minimum
    _check_roster_minimum(team)
    messages.success(request, f"You have left '{team.name}'.")
    return redirect("dashboard")


@login_required
def transfer_captaincy_view(request, pk):
    """Captain transfers their role to another team member."""
    team = get_object_or_404(Team, pk=pk)
    if not _is_captain(request.user, team):
        messages.error(request, "Only the current captain can transfer captaincy.")
        return redirect("team_detail", pk=pk)

    members = team.memberships.exclude(user=request.user).select_related("user")
    if not members.exists():
        messages.error(request, "There are no other members to transfer captaincy to.")
        return redirect("team_detail", pk=pk)

    if request.method == "POST":
        new_captain_id = request.POST.get("new_captain")
        new_membership = TeamMembership.objects.filter(
            team=team, user_id=new_captain_id
        ).exclude(user=request.user).first()
        if not new_membership:
            messages.error(request, "Invalid member selected.")
            return redirect("transfer_captaincy", pk=pk)
        # Demote current captain, promote new one
        TeamMembership.objects.filter(team=team, user=request.user).update(role="member")
        new_membership.role = "captain"
        new_membership.save(update_fields=["role"])
        log_action(
            request,
            "captaincy_transferred",
            f"Captaincy of '{team.name}' transferred from '{request.user.username}' to '{new_membership.user.username}'",
            tournament=_get_tournament(request),
        )
        messages.success(request, f"Captaincy transferred to '{new_membership.user.username}'.")
        return redirect("team_detail", pk=pk)

    return render(request, "core/transfer_captaincy.html", {
        "team": team,
        "members": members,
        **_tournament_context(request, _get_tournament(request)),
    })


@login_required
@require_POST
def delete_team_view(request, pk):
    """Captain deletes the team entirely (and all participations / memberships)."""
    team = get_object_or_404(Team, pk=pk)
    if not _is_captain(request.user, team) and not _is_organizer(request.user):
        messages.error(request, "Only the captain or an organizer can delete the team.")
        return redirect("team_detail", pk=pk)

    # Safety: require confirmation and password from captain
    if not _is_organizer(request.user):
        if request.POST.get("confirm_delete") != "yes":
            messages.error(request, "Please confirm deletion before continuing.")
            return redirect("team_detail", pk=pk)
        password = request.POST.get("password", "")
        if not password or not request.user.check_password(password):
            messages.error(request, "Incorrect password. Deletion cancelled.")
            return redirect("team_detail", pk=pk)

    team_name = team.name
    tournament = _get_tournament(request)
    team.delete()
    log_action(
        request,
        "team_deleted",
        f"Team '{team_name}' deleted",
        tournament=tournament,
    )
    messages.success(request, f"Team '{team_name}' has been deleted.")
    return redirect("dashboard")


# =============================================================================
# SECTION 8 — NOTIFICATION VIEWS
# =============================================================================

@login_required
def notifications_view(request):
    """List all notifications for the current user (8.1, 8.3)."""
    notifications = Notification.objects.filter(user=request.user).order_by("-created_at")[:100]
    unread_count = Notification.objects.filter(user=request.user, is_read=False).count()
    tournament = _get_tournament(request)
    return render(request, "core/notifications.html", {
        "notifications": notifications,
        "unread_count": unread_count,
        **_tournament_context(request, tournament),
    })


@login_required
@require_POST
def mark_notifications_read(request):
    """Mark all notifications as read (8.3)."""
    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    return redirect("notifications")


@login_required
@require_POST
def mark_notification_read(request, pk):
    """Mark a single notification as read and redirect to its link (8.3)."""
    notif = get_object_or_404(Notification, pk=pk, user=request.user)
    notif.is_read = True
    notif.save(update_fields=["is_read"])
    if notif.link:
        return redirect(notif.link)
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
    teams = [m.team for m in memberships]
    wins = 0
    losses = 0
    if teams:
        team_ids = [t.pk for t in teams]
        wins = Match.objects.filter(winner_id__in=team_ids, status="confirmed").count()
        losses = (
            Match.objects.filter(
                status="confirmed",
                team1_id__in=team_ids,
                winner__isnull=False,
            ).exclude(winner_id__in=team_ids).count()
            + Match.objects.filter(
                status="confirmed",
                team2_id__in=team_ids,
                winner__isnull=False,
            ).exclude(winner_id__in=team_ids).count()
        )
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
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can review applications.")
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

    return redirect("settings")


# =============================================================================
# SECTION 2.2–2.4 — TEAM INVITE FLOWS
# =============================================================================

@login_required
def team_invite_view(request, pk):
    """Captain invites a user to the team (2.2)."""
    team = get_object_or_404(Team, pk=pk)
    if not _is_captain(request.user, team):
        messages.error(request, "Only the team captain can send invites.")
        return redirect("team_detail", pk=pk)

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        if not username:
            messages.error(request, "Please enter a username.")
        else:
            try:
                target_user = User.objects.get(username=username)
            except User.DoesNotExist:
                messages.error(request, f"No user found with username '{username}'.")
                return redirect("team_invite", pk=pk)

            if target_user == request.user:
                messages.error(request, "You cannot invite yourself.")
                return redirect("team_invite", pk=pk)

            # Check they are not already a member
            if TeamMembership.objects.filter(team=team, user=target_user).exists():
                messages.error(request, f"{username} is already a member of this team.")
                return redirect("team_invite", pk=pk)

            # Create or update invite
            invite, created = TeamInvite.objects.get_or_create(
                team=team,
                invited_user=target_user,
                defaults={"invited_by": request.user},
            )
            if not created and invite.status == "pending":
                messages.warning(request, f"An invite for {username} is already pending.")
                return redirect("team_invite", pk=pk)
            elif not created:
                invite.status = "pending"
                invite.invited_by = request.user
                invite.save()

            _notify(
                target_user,
                "team_invite_received",
                f"You have been invited to join {team.name} by {request.user.username}.",
                link="/teams/my-invites/",
            )
            log_action(request, "team_invite_sent", f"Invite sent to '{username}' for team '{team.name}'")
            messages.success(request, f"Invite sent to {username}.")
            return redirect("team_detail", pk=pk)

    tournament = _get_tournament(request)
    pending_invites = TeamInvite.objects.filter(team=team, status="pending").select_related("invited_user")
    return render(request, "core/team_invite.html", {
        "team": team,
        "pending_invites": pending_invites,
        **_tournament_context(request, tournament),
    })


@login_required
@require_POST
def accept_team_invite(request, pk):
    """Accept a team invite (2.3)."""
    invite = get_object_or_404(TeamInvite, pk=pk, invited_user=request.user)
    if invite.status != "pending":
        messages.error(request, "This invite is no longer active.")
        return redirect("notifications")

    team = invite.team
    # Check not already a member
    if TeamMembership.objects.filter(team=team, user=request.user).exists():
        invite.status = "accepted"
        invite.save()
        messages.info(request, f"You are already a member of {team.name}.")
        return redirect("team_detail", pk=team.pk)

    TeamMembership.objects.create(team=team, user=request.user, role="member")
    _promote_team_participation_when_full(team, request=request)

    # Update active team
    from .models import UserTeamAssignment
    assignment, _ = UserTeamAssignment.objects.get_or_create(user=request.user)
    assignment.active_team = team
    assignment.save()

    invite.status = "accepted"
    invite.save()

    # Notify captain
    captain_membership = TeamMembership.objects.filter(team=team, role="captain").select_related("user").first()
    if captain_membership:
        _notify(
            captain_membership.user,
            "team_invite_accepted",
            f"{request.user.username} accepted your invite to join {team.name}.",
            link=f"/team/{team.pk}/",
        )

    log_action(request, "team_invite_accepted", f"User '{request.user.username}' joined team '{team.name}'")
    messages.success(request, f"You have joined {team.name}!")
    return redirect("team_detail", pk=team.pk)


@login_required
@require_POST
def decline_team_invite(request, pk):
    """Decline a team invite (2.4)."""
    invite = get_object_or_404(TeamInvite, pk=pk, invited_user=request.user)
    if invite.status != "pending":
        messages.error(request, "This invite is no longer active.")
        return redirect("notifications")

    invite.status = "declined"
    invite.save()

    # Notify captain
    captain_membership = TeamMembership.objects.filter(team=invite.team, role="captain").select_related("user").first()
    if captain_membership:
        _notify(
            captain_membership.user,
            "team_invite_declined",
            f"{request.user.username} declined your invite to join {invite.team.name}.",
            link=f"/team/{invite.team.pk}/",
        )

    log_action(request, "team_invite_declined", f"User '{request.user.username}' declined invite to '{invite.team.name}'")
    messages.info(request, f"You declined the invite to join {invite.team.name}.")
    return redirect("notifications")


@login_required
def my_invites_view(request):
    """List all pending team invites for the current user."""
    invites = TeamInvite.objects.filter(
        invited_user=request.user, status="pending"
    ).select_related("team", "invited_by")
    tournament = _get_tournament(request)
    return render(request, "core/my_invites.html", {
        "invites": invites,
        **_tournament_context(request, tournament),
    })


# =============================================================================
# SECTION 2.11 — TEAM TOURNAMENT HISTORY
# =============================================================================

def team_history_view(request, pk):
    """Team tournament history (2.11)."""
    team = get_object_or_404(Team, pk=pk, is_internal=False)
    participations = (
        TeamTournamentParticipation.objects.filter(team=team)
        .select_related("tournament")
        .order_by("-tournament__created_at")
    )
    tournament = _get_tournament(request) if request.user.is_authenticated else None
    ctx = {
        "team": team,
        "participations": participations,
    }
    if request.user.is_authenticated:
        ctx.update(_tournament_context(request, tournament))
    return render(request, "core/team_history.html", ctx)


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
    matches = (
        tournament.matches.filter(team1__isnull=False, team2__isnull=False)
        .select_related("team1", "team2", "court")
        .order_by("round_number", "match_number")
    )

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
# SECTION 4.8 — MY REGISTRATIONS
# =============================================================================

@login_required
def my_registrations_view(request):
    """List all current and past registrations for the current user (4.8)."""
    # Team registrations via memberships
    team_participations = (
        TeamTournamentParticipation.objects.filter(
            team__memberships__user=request.user,
            team__is_internal=False,
        )
        .select_related("team", "tournament")
        .order_by("-tournament__created_at")
        .distinct()
    )
    # Individual registrations
    individual_regs = (
        TournamentIndividualRegistration.objects.filter(user=request.user)
        .select_related("tournament")
        .order_by("-tournament__created_at")
    )
    tournament = _get_tournament(request)
    return render(request, "core/my_registrations.html", {
        "team_participations": team_participations,
        "individual_regs": individual_regs,
        **_tournament_context(request, tournament),
    })


# =============================================================================
# SECTION 3.7–3.9 — REGISTRATION REVIEW (ORGANIZER)
# =============================================================================

@login_required
def registration_review_view(request, pk):
    """Organizer reviews all registrations for a tournament (3.7)."""
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can review registrations.")
        return redirect("dashboard")

    tournament = get_object_or_404(Tournament, pk=pk)

    if tournament.registration_mode == "individual":
        registrations = list(
            TournamentIndividualRegistration.objects.filter(tournament=tournament)
            .select_related("user")
            .order_by("status", "display_name")
        )
        team_regs = []
    else:
        registrations = []
        team_regs = list(
            TeamTournamentParticipation.objects.filter(tournament=tournament, team__is_internal=False)
            .select_related("team")
            .prefetch_related("team__memberships__user")
            .order_by("status", "team__name")
        )

    return render(request, "core/registration_review.html", {
        "tournament": tournament,
        "registrations": registrations,
        "team_regs": team_regs,
        **_tournament_context(request, tournament),
    })


@login_required
@require_POST
def approve_registration(request, tournament_pk, reg_pk):
    """Organizer approves a pending registration (3.8)."""
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can approve registrations.")
        return redirect("dashboard")

    tournament = get_object_or_404(Tournament, pk=tournament_pk)

    # Try individual registration first, then team participation
    reg = (
        TournamentIndividualRegistration.objects.filter(pk=reg_pk, tournament=tournament).first()
        or TeamTournamentParticipation.objects.filter(pk=reg_pk, tournament=tournament, team__is_internal=False).first()
    )
    if not reg:
        messages.error(request, "Registration not found.")
        return redirect("registration_review", pk=tournament_pk)

    reg.status = "active"
    reg.save(update_fields=["status", "updated_at"])

    # Notify relevant users
    if hasattr(reg, "user"):
        notify_users = [reg.user]
        name = reg.display_name
    else:
        notify_users = list(User.objects.filter(memberships__team=reg.team))
        name = reg.team.name

    _notify(
        notify_users,
        "registration_approved",
        f"Your registration for {tournament.name} has been approved!",
        link=f"/tournaments/{tournament.pk}/",
        tournament=tournament,
    )

    log_action(
        request, "registration_approved",
        f"Registration for '{name}' approved in '{tournament.name}'",
        tournament=tournament,
    )
    messages.success(request, f"Registration for '{name}' approved.")
    return redirect("registration_review", pk=tournament_pk)


@login_required
@require_POST
def reject_registration(request, tournament_pk, reg_pk):
    """Organizer rejects a pending registration (3.9)."""
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can reject registrations.")
        return redirect("dashboard")

    tournament = get_object_or_404(Tournament, pk=tournament_pk)
    reason = request.POST.get("reason", "").strip()

    reg = (
        TournamentIndividualRegistration.objects.filter(pk=reg_pk, tournament=tournament).first()
        or TeamTournamentParticipation.objects.filter(pk=reg_pk, tournament=tournament, team__is_internal=False).first()
    )
    if not reg:
        messages.error(request, "Registration not found.")
        return redirect("registration_review", pk=tournament_pk)

    reg.status = "withdrawn"
    reg.save(update_fields=["status", "updated_at"])

    # Notify relevant users
    if hasattr(reg, "user"):
        notify_users = [reg.user]
        name = reg.display_name
    else:
        notify_users = list(User.objects.filter(memberships__team=reg.team))
        name = reg.team.name

    reason_text = f" Reason: {reason}" if reason else ""
    _notify(
        notify_users,
        "registration_rejected",
        f"Your registration for {tournament.name} has been rejected.{reason_text}",
        link=f"/tournaments/{tournament.pk}/",
        tournament=tournament,
    )

    log_action(
        request, "registration_rejected",
        f"Registration for '{name}' rejected in '{tournament.name}'{reason_text}",
        tournament=tournament,
    )
    messages.success(request, f"Registration for '{name}' rejected.")
    return redirect("registration_review", pk=tournament_pk)


# =============================================================================
# SECTION 3.14 — CANCEL TOURNAMENT
# =============================================================================

@login_required
@require_POST
def cancel_tournament(request, pk):
    """Cancel a tournament and notify all registered participants (3.14)."""
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can cancel tournaments.")
        return redirect("dashboard")

    tournament = get_object_or_404(Tournament, pk=pk)

    if tournament.status in ("completed", "cancelled"):
        messages.error(request, f"Cannot cancel a tournament that is already {tournament.status}.")
        return redirect("settings")

    if request.POST.get("confirm_cancel", "").strip().upper() != "CANCEL":
        messages.error(request, "Please type CANCEL to confirm.")
        return redirect("settings")

    tournament.status = "cancelled"
    tournament.save(update_fields=["status"])

    # Collect all enrolled users for notification
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
            "tournament_cancelled",
            f"The tournament '{tournament.name}' has been cancelled.",
            link=f"/tournaments/{tournament.pk}/",
            tournament=tournament,
        )

    log_action(
        request, "tournament_cancelled",
        f"Tournament '{tournament.name}' cancelled",
        tournament=tournament,
    )
    messages.success(request, f"Tournament '{tournament.name}' has been cancelled.")
    return redirect("dashboard")


# =============================================================================
# SECTION 3.15 — DUPLICATE TOURNAMENT
# =============================================================================

@login_required
@require_POST
def duplicate_tournament(request, pk):
    """Create a new tournament with same config but no participants or matches (3.15)."""
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can duplicate tournaments.")
        return redirect("dashboard")

    source = get_object_or_404(Tournament, pk=pk)
    new_name = f"Copy of {source.name}"

    new_tournament = Tournament.objects.create(
        name=new_name,
        sport_type=source.sport_type,
        registration_mode=source.registration_mode,
        format=source.format,
        players_per_team=source.players_per_team,
        status="setup",
        points_per_win=source.points_per_win,
        points_per_loss=source.points_per_loss,
        points_per_draw=source.points_per_draw,
        tiebreaker_order=source.tiebreaker_order,
        num_groups=source.num_groups,
        teams_per_group_advance=source.teams_per_group_advance,
        withdrawal_policy=source.withdrawal_policy,
        default_match_duration=source.default_match_duration,
        expected_teams_count=source.expected_teams_count,
    )
    # Set this as the organizer's selected tournament
    request.session["selected_tournament_id"] = new_tournament.pk

    log_action(
        request, "tournament_duplicated",
        f"Tournament '{source.name}' duplicated as '{new_name}'",
        tournament=new_tournament,
    )
    messages.success(request, f"Tournament duplicated as '{new_name}'. Please update the dates and settings.")
    return redirect("tournament_config", pk=new_tournament.pk)


# =============================================================================
# SECTION 7.2 — DISQUALIFY TEAM
# =============================================================================

@login_required
@require_POST
def disqualify_team(request, tournament_pk, participation_pk):
    """Organizer disqualifies a team mid-tournament (7.2)."""
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can disqualify teams.")
        return redirect("dashboard")

    tournament = get_object_or_404(Tournament, pk=tournament_pk)
    participation = get_object_or_404(
        TeamTournamentParticipation, pk=participation_pk, tournament=tournament
    )
    reason = request.POST.get("reason", "").strip()

    participation.status = "withdrawn"
    participation.withdrawn_at = timezone.now()
    participation.save(update_fields=["status", "withdrawn_at", "updated_at"])

    # Forfeit any active/upcoming matches for this team
    team = participation.team
    active_matches = tournament.matches.filter(
        status__in=("upcoming", "in_progress", "pending_confirmation"),
    ).filter(Q(team1=team) | Q(team2=team))

    for match in active_matches:
        opponent = match.get_opponent(team)
        if opponent:
            match.status = "forfeited"
            match.winner = opponent
            match.notes = (match.notes + "\n" if match.notes else "") + f"Disqualification: {team.name}. {reason}"
            match.save()
            if tournament.format in ("knockout", "double_elimination", "consolation", "hybrid"):
                from .standings import advance_winner
                advance_winner(match)
            _check_and_finalize_tournament(tournament)

    # Notify team members
    team_users = list(User.objects.filter(memberships__team=team))
    reason_text = f" Reason: {reason}" if reason else ""
    _notify(
        team_users,
        "disqualified",
        f"Your team {team.name} has been disqualified from {tournament.name}.{reason_text}",
        link=f"/tournaments/{tournament.pk}/",
        tournament=tournament,
    )

    log_action(
        request, "team_disqualified",
        f"Team '{team.name}' disqualified from '{tournament.name}'.{reason_text}",
        tournament=tournament,
    )
    messages.success(request, f"Team '{team.name}' has been disqualified.")
    return redirect("registration_review", pk=tournament_pk)


# =============================================================================
# SECTION 7.5–7.6 — PAUSE / RESUME TOURNAMENT
# =============================================================================

@login_required
@require_POST
def pause_tournament(request, pk):
    """Pause an active tournament (7.5)."""
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can pause tournaments.")
        return redirect("dashboard")

    tournament = get_object_or_404(Tournament, pk=pk)
    if tournament.status != "active":
        messages.error(request, "Only active tournaments can be paused.")
        return redirect("dashboard")

    tournament.status = "paused"
    tournament.save(update_fields=["status"])

    # Notify all enrolled participants
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
            "tournament_paused",
            f"The tournament '{tournament.name}' has been paused. No new results can be submitted.",
            link=f"/tournaments/{tournament.pk}/",
            tournament=tournament,
        )

    log_action(request, "tournament_paused", f"Tournament '{tournament.name}' paused", tournament=tournament)
    messages.success(request, f"Tournament '{tournament.name}' has been paused.")
    return redirect("dashboard")


@login_required
@require_POST
def resume_tournament(request, pk):
    """Resume a paused tournament (7.6)."""
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can resume tournaments.")
        return redirect("dashboard")

    tournament = get_object_or_404(Tournament, pk=pk)
    if tournament.status != "paused":
        messages.error(request, "Only paused tournaments can be resumed.")
        return redirect("dashboard")

    tournament.status = "active"
    tournament.save(update_fields=["status"])

    # Notify all enrolled participants
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
            "tournament_resumed",
            f"The tournament '{tournament.name}' has resumed. Match results can be submitted again.",
            link=f"/tournaments/{tournament.pk}/",
            tournament=tournament,
        )

    log_action(request, "tournament_resumed", f"Tournament '{tournament.name}' resumed", tournament=tournament)
    messages.success(request, f"Tournament '{tournament.name}' has been resumed.")
    return redirect("dashboard")


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
    from .models import AuditLog as _AuditLog
    organizer = get_object_or_404(User, pk=pk)
    try:
        profile = organizer.organizer_profile
        if not profile.verified and not organizer.is_staff:
            from django.http import Http404
            raise Http404
    except OrganizerProfile.DoesNotExist:
        if not organizer.is_staff:
            from django.http import Http404
            raise Http404
        profile = None

    # Find tournaments created by this organizer via AuditLog entries for 'tournament_created'
    created_tournament_pks = _AuditLog.objects.filter(
        user=organizer, action="tournament_created"
    ).values_list("tournament_id", flat=True).distinct()

    if created_tournament_pks.exists():
        tournaments = Tournament.objects.filter(pk__in=created_tournament_pks).order_by("-created_at")
    else:
        # Fallback: show all tournaments if none are specifically attributed to this organizer
        # (e.g. created via admin or before audit logs were in place)
        if organizer.is_staff:
            tournaments = Tournament.objects.all().order_by("-created_at")
        else:
            tournaments = Tournament.objects.none()

    return render(request, "core/organizer_public_page.html", {
        "organizer": organizer,
        "profile": profile,
        "tournaments": tournaments,
    })


# =============================================================================
# FLOW 4 — Suspend/unsuspend user (11.2)
# =============================================================================

@require_POST
@login_required
def toggle_user_suspension(request, user_pk):
    """Suspend or unsuspend a user account (11.2)."""
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can suspend users.")
        return redirect("settings")

    target = get_object_or_404(User, pk=user_pk)

    if target == request.user:
        messages.error(request, "You cannot suspend yourself.")
        return redirect("settings")

    if target.is_superuser:
        messages.error(request, "Cannot suspend a superuser.")
        return redirect("settings")

    if target.is_active:
        target.is_active = False
        target.save(update_fields=["is_active"])
        log_action(request, "user_suspended", f"User '{target.username}' suspended")
        messages.success(request, f"User '{target.username}' has been suspended.")
    else:
        target.is_active = True
        target.save(update_fields=["is_active"])
        log_action(request, "user_unsuspended", f"User '{target.username}' unsuspended")
        messages.success(request, f"User '{target.username}' has been unsuspended.")

    return redirect("settings")


# =============================================================================
# FLOW 5 — Impersonate user (11.7)
# =============================================================================

@login_required
@require_POST
def impersonate_user(request, user_pk):
    """Admin can impersonate any user for debugging (11.7)."""
    if not request.user.is_superuser:
        messages.error(request, "Only admins can impersonate users.")
        return redirect("settings")
    target = get_object_or_404(User, pk=user_pk)
    if target.is_superuser:
        messages.error(request, "Cannot impersonate a superuser.")
        return redirect("settings")
    # Store original user pk before switching
    original_pk = request.user.pk
    request.session["impersonating_original_user_pk"] = original_pk
    # Store the original admin's session auth hash so we can restore it exactly.
    # Without this, if the admin's password changes during the impersonation session,
    # Django would invalidate the session when we try to restore it (HASH_SESSION_KEY
    # must match get_session_auth_hash() for the session to remain valid).
    request.session["impersonating_original_hash"] = request.user.get_session_auth_hash()
    # Switch session to target user (manually update Django's internal session keys)
    request.session["_auth_user_id"] = str(target.pk)
    request.session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
    request.session["_auth_user_hash"] = target.get_session_auth_hash()
    log_action(request, "impersonation_started", f"Admin '{request.user.username}' impersonating '{target.username}'")
    messages.warning(request, f"You are now impersonating {target.username}. Click 'Stop Impersonating' to return.")
    return redirect("dashboard")


def stop_impersonating(request):
    """Stop impersonation and restore original admin session (no login_required — impersonated user may be inactive)."""
    original_pk = request.session.get("impersonating_original_user_pk")
    if not original_pk:
        messages.info(request, "You are not impersonating anyone.")
        return redirect("dashboard")
    original_user = get_object_or_404(User, pk=original_pk)
    impersonated_user_id = request.session.get("_auth_user_id", "unknown")
    original_hash = request.session.pop("impersonating_original_hash", original_user.get_session_auth_hash())
    request.session["_auth_user_id"] = str(original_pk)
    request.session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
    request.session["_auth_user_hash"] = original_hash
    del request.session["impersonating_original_user_pk"]
    log_action(request, "impersonation_ended", f"Admin '{original_user.username}' stopped impersonating user pk={impersonated_user_id}")
    messages.success(request, "Impersonation ended.")
    return redirect("settings")


# =============================================================================
# FLOW 6 — Team stats page (10.2)
# =============================================================================

@login_required
def team_stats_view(request, pk):
    """Show win/loss/draw stats for a team (10.2)."""
    team = get_object_or_404(Team, pk=pk)

    played_matches = Match.objects.filter(
        Q(team1=team) | Q(team2=team),
        status__in=["confirmed", "forfeited"],
    )
    total = played_matches.count()
    wins = played_matches.filter(winner=team).count()
    losses = played_matches.exclude(winner=team).exclude(winner=None).count()
    draws = total - wins - losses

    participations = TeamTournamentParticipation.objects.filter(team=team).select_related("tournament")
    participation_data = []
    for p in participations:
        champion = getattr(p.tournament, "champion", None)
        placement = "1st" if champion and champion == team else "—"
        participation_data.append({"participation": p, "placement": placement})

    roster = TeamMembership.objects.filter(team=team).select_related("user").order_by("joined_at")

    tournament = _get_tournament(request)
    return render(request, "core/team_stats.html", {
        "team": team,
        "total": total,
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "participation_data": participation_data,
        "roster": roster,
        **_tournament_context(request, tournament),
    })


# =============================================================================
# FLOW 7 — Seed participants (3.10)
# =============================================================================

@login_required
def seed_participants_view(request, pk):
    """View and edit participant seeds for a tournament (3.10)."""
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can manage seeds.")
        return redirect("dashboard")

    tournament = get_object_or_404(Tournament, pk=pk)

    if tournament.registration_mode == "individual":
        participants = list(
            TournamentIndividualRegistration.objects.filter(
                tournament=tournament, status="active"
            ).order_by("seed", "id")
        )
    else:
        participants = list(
            TeamTournamentParticipation.objects.filter(
                tournament=tournament, status="active"
            ).select_related("team").order_by("seed", "id")
        )

    if request.method == "POST":
        action = request.POST.get("action", "save")

        if action == "auto_seed":
            for idx, p in enumerate(participants, start=1):
                p.seed = idx
                p.save(update_fields=["seed"])
            log_action(request, "seeds_auto_assigned", f"Auto-seeded {len(participants)} participants for '{tournament.name}'", tournament=tournament)
            messages.success(request, "Participants auto-seeded.")
            return redirect("tournament_config", pk=pk)

        content_type = request.META.get("CONTENT_TYPE", "")
        if "application/json" in content_type:
            try:
                data = json.loads(request.body)
                seeds = data.get("seeds", {})
            except (json.JSONDecodeError, AttributeError):
                return JsonResponse({"error": "Invalid JSON"}, status=400)
        else:
            seeds = {}
            for key, val in request.POST.items():
                if key.startswith("seed_"):
                    try:
                        p_pk = int(key[5:])
                        seeds[p_pk] = int(val)
                    except ValueError:
                        pass

        for p in participants:
            new_seed = seeds.get(p.pk) if isinstance(seeds, dict) else None
            if new_seed is not None:
                p.seed = new_seed
                p.save(update_fields=["seed"])

        log_action(request, "seeds_updated", f"Seeds updated for '{tournament.name}'", tournament=tournament)
        messages.success(request, "Seeds saved.")
        return redirect("tournament_config", pk=pk)

    return render(request, "core/seed_participants.html", {
        "tournament": tournament,
        "participants": participants,
        "is_individual": tournament.registration_mode == "individual",
        **_tournament_context(request, tournament),
    })


# =============================================================================
# FLOW 7.3 — Add substitute player to team tournament roster
# =============================================================================

@login_required
def tournament_team_sub_view(request, pk, participation_pk):
    """Organizer can add a substitute player to a team's tournament roster (7.3).

    A substitute is added as a 'sub' TeamMembership, which allows them to
    participate in this tournament's matches without being a permanent member.
    """
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can manage substitutes.")
        return redirect("dashboard")

    tournament = get_object_or_404(Tournament, pk=pk)
    participation = get_object_or_404(TeamTournamentParticipation, pk=participation_pk, tournament=tournament)
    team = participation.team

    current_subs = TeamMembership.objects.filter(team=team, role="sub").select_related("user")

    if request.method == "POST":
        action = request.POST.get("action", "add")

        if action == "remove":
            sub_pk = request.POST.get("sub_pk")
            if sub_pk:
                sub_membership = TeamMembership.objects.filter(
                    pk=sub_pk, team=team, role="sub"
                ).first()
                if sub_membership:
                    username = sub_membership.user.username
                    sub_membership.delete()
                    log_action(
                        request,
                        "sub_removed",
                        f"Sub '{username}' removed from team '{team.name}' for '{tournament.name}'",
                        tournament=tournament,
                    )
                    messages.success(request, f"Substitute '{username}' removed.")
            return redirect("tournament_team_sub", pk=pk, participation_pk=participation_pk)

        # action == "add"
        username = request.POST.get("username", "").strip()
        if not username:
            messages.error(request, "Please enter a username.")
        else:
            try:
                target_user = User.objects.get(username__iexact=username, is_active=True)
            except User.DoesNotExist:
                messages.error(request, f"User '{username}' not found.")
                target_user = None

            if target_user:
                if TeamMembership.objects.filter(team=team, user=target_user).exists():
                    messages.error(request, f"'{username}' is already on this team.")
                else:
                    TeamMembership.objects.create(team=team, user=target_user, role="sub")
                    log_action(
                        request,
                        "sub_added",
                        f"Sub '{username}' added to team '{team.name}' for '{tournament.name}'",
                        tournament=tournament,
                    )
                    _notify(
                        target_user,
                        "general",
                        f"You have been added as a substitute for '{team.name}' in the tournament '{tournament.name}'.",
                        link=f"/team/{team.pk}/",
                        tournament=tournament,
                    )
                    messages.success(request, f"'{username}' added as a substitute.")
        return redirect("tournament_team_sub", pk=pk, participation_pk=participation_pk)

    return render(request, "core/tournament_team_sub.html", {
        "tournament": tournament,
        "team": team,
        "participation": participation,
        "current_subs": current_subs,
        **_tournament_context(request, tournament),
    })
