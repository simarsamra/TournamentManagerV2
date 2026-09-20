"""Fixtures, scores, disputes, reschedules and no-shows."""
"""Core views for tournament management."""
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..models import (
    Match,
    NoShowReport,
    OpenSlot,
    RescheduleRequest,
    Team,
    TournamentIndividualRegistration,
)
from ..forms import RescheduleForm, ScoreSubmitForm
from ..scheduling import generate_consolation_if_ready
from ..standings import (
    advance_loser_to_third_place,
    advance_winner,
    check_group_stage_complete,
)
from ..audit import log_action

from .helpers import (
    CRITICAL_STAGE_DISPUTE_WINDOW_MINUTES,
    DEFAULT_DISPUTE_WINDOW_MINUTES,
    _build_open_slot_choices,
    _can_manage_reschedule,
    _can_manage_tournament,
    _can_override_match,
    _check_and_finalize_tournament,
    _create_open_slot_for_completed_match,
    _dispute_window_minutes_for_match,
    _expire_no_show_reports,
    _expire_pending_score_disputes,
    _finalize_no_show_match,
    _get_team,
    _get_tournament,
    _is_captain,
    _is_critical_stage_match,
    _is_htmx_request,
    _is_organizer,
    _is_within_dispute_window,
    _lock_match_score,
    _match_display_str,
    _render_refreshable_page,
    _safe_page_param,
    _sync_open_slots_for_tournament,
    _team_display_label,
    _team_display_map,
    _tournament_context,
)



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
    if tournament.registration_mode == "individual":
        team_options = [
            {
                "pk": reg.shadow_team_id,
                "name": reg.display_name,
            }
            for reg in TournamentIndividualRegistration.objects.filter(
                tournament=tournament,
                status="active",
                shadow_team__isnull=False,
            )
            .order_by("display_name")
            .only("shadow_team_id", "display_name")
        ]
    else:
        team_options = [
            {
                "pk": team.pk,
                "name": team.name,
            }
            for team in Team.objects.filter(
                participations__tournament=tournament,
                is_internal=False,
            )
            .distinct()
            .order_by("name")
            .only("pk", "name")
        ]
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
        "team_options": team_options,
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

def _redirect_to_match_detail(request, match_pk):
    if _is_htmx_request(request):
        return match_detail(request, pk=match_pk)
    return redirect("match_detail", pk=match_pk)


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
        match.status == "pending_confirmation"
        and (
            is_organizer
            or (
                is_participant
                and match.submitted_by != request.user
                and dispute_window_open
            )
        )
    )
    can_dispute = (
        is_participant
        and match.status == "pending_confirmation"
        and match.submitted_by != request.user
        and dispute_window_open
    )
    pending_no_show_report = match.no_show_reports.filter(status="pending").select_related(
        "absent_team", "present_team"
    ).first()
    no_show_window_open = bool(match.scheduled_time and match.scheduled_time <= timezone.now())
    can_mark_no_show = is_organizer and bool(match.team1_id and match.team2_id) and match.status in ("upcoming", "in_progress") and no_show_window_open
    can_report_no_show = is_participant and _is_captain(request.user, team) and bool(match.team1_id and match.team2_id) and match.status in ("upcoming", "in_progress") and no_show_window_open and not pending_no_show_report
    can_reschedule = is_participant and _can_manage_reschedule(request.user, match.tournament, team)
    can_override_result = is_organizer and _can_override_match(match)
    reschedule_form = RescheduleForm(tournament=match.tournament)
    open_slot_choices = _build_open_slot_choices(match, reschedule_form.fields["open_slot"].queryset)
    reschedule_requests = list(
        match.reschedule_requests.select_related("requested_by", "new_court").order_by("-created_at")
    )
    reschedule_request_rows = []
    for rr in reschedule_requests:
        requested_by_label = rr.requested_by.get_full_name().strip() or rr.requested_by.username
        requester_team = _get_team(rr.requested_by, match.tournament)
        requested_team_label = _team_display_label(match.tournament, requester_team) if requester_team else "-"
        reschedule_request_rows.append(
            {
                "request": rr,
                "requested_by_label": requested_by_label,
                "requested_team_label": requested_team_label,
            }
        )
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
        "reschedule_request_rows": reschedule_request_rows,
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
    # Organizer powers here (instant confirm, overriding status) apply only to
    # tournaments this user actually manages; otherwise treat them as a plain
    # participant.
    is_organizer = _can_manage_tournament(request.user, match.tournament)
    is_participant = team and (match.team1 == team or match.team2 == team)
    if not is_organizer and not is_participant:
        messages.error(request, "You are not a participant in this match.")
        return _redirect_to_match_detail(request, pk)
    if match.tournament.status == "paused" and not is_organizer:
        messages.error(request, "The tournament is currently paused. Score submission is not allowed.")
        return _redirect_to_match_detail(request, pk)
    # Organizers can submit scores in both active and paused; participants only when active
    allowed_tournament_statuses = ("active", "paused") if is_organizer else ("active",)
    if match.tournament.status not in allowed_tournament_statuses:
        messages.error(request, "Scores can only be submitted once the tournament has started.")
        return _redirect_to_match_detail(request, pk)
    allowed_statuses = ("upcoming", "in_progress", "pending_confirmation", "disputed") if is_organizer else ("upcoming", "in_progress")
    if match.status not in allowed_statuses:
        messages.error(request, "Score cannot be submitted for this match.")
        return _redirect_to_match_detail(request, pk)
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
            return _redirect_to_match_detail(request, pk)
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
                advance_loser_to_third_place(match)
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
                       f"Score submitted for {_match_display_str(match)}: {match.score_team1}-{match.score_team2}",
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
    if _is_htmx_request(request):
        return match_detail(request, pk=pk)
    return _redirect_to_match_detail(request, pk)


@login_required
@require_POST
def confirm_score(request, pk):
    match = get_object_or_404(Match, pk=pk)
    _expire_pending_score_disputes(match.tournament)
    match.refresh_from_db()
    is_organizer = _is_organizer(request.user)
    team = _get_team(request.user, match.tournament)
    if match.status != "pending_confirmation":
        messages.error(request, "Match is not pending confirmation.")
        return _redirect_to_match_detail(request, pk)
    if not is_organizer:
        if not team or match.submitted_by == request.user:
            messages.error(request, "Cannot confirm your own submission.")
            return _redirect_to_match_detail(request, pk)
        if not _is_within_dispute_window(match):
            messages.error(request, "The dispute window has expired and the score is now locked.")
            return _redirect_to_match_detail(request, pk)
        if match.team1 != team and match.team2 != team:
            messages.error(request, "You are not a participant in this match.")
            return _redirect_to_match_detail(request, pk)
    tournament = match.tournament
    if not _lock_match_score(match, confirmed_by_user=request.user):
        messages.error(request, "Draws are not allowed in elimination matches.")
        return _redirect_to_match_detail(request, pk)
    log_action(request, "score_confirmed",
               f"Score confirmed for {_match_display_str(match)}: {match.score_team1}-{match.score_team2}",
               tournament=tournament)
    messages.success(request, "Score locked. Match marked done.")
    if _is_htmx_request(request):
        return match_detail(request, pk=pk)
    return _redirect_to_match_detail(request, pk)


@login_required
@require_POST
def dispute_score(request, pk):
    match = get_object_or_404(Match, pk=pk)
    _expire_pending_score_disputes(match.tournament)
    match.refresh_from_db()
    team = _get_team(request.user, match.tournament)
    if not team or match.submitted_by == request.user:
        messages.error(request, "Cannot dispute your own submission.")
        return _redirect_to_match_detail(request, pk)
    if match.team1 != team and match.team2 != team:
        messages.error(request, "You are not a participant in this match.")
        return _redirect_to_match_detail(request, pk)
    if match.status != "pending_confirmation":
        messages.error(request, "Match is not pending confirmation.")
        return _redirect_to_match_detail(request, pk)
    if not _is_within_dispute_window(match):
        messages.error(request, "Dispute window has expired; score is locked.")
        return _redirect_to_match_detail(request, pk)
    dispute_note = request.POST.get("dispute_notes", "").strip()
    match.status = "disputed"
    match.disputed_by = request.user
    match.critical_dispute = _is_critical_stage_match(match)
    prefix = "CRITICAL-STAGE DISPUTE" if match.critical_dispute else "DISPUTED"
    match.notes = f"{prefix} by {request.user.username}: {dispute_note}" if dispute_note else f"{prefix} by {request.user.username}"
    match.save()
    log_action(request, "score_disputed",
               f"Score disputed for {_match_display_str(match)} by {request.user.username}: {dispute_note}",
               tournament=match.tournament)
    if match.critical_dispute:
        messages.warning(request, "Critical-stage dispute filed. Organizers will review with priority.")
    else:
        messages.warning(request, "Score has been disputed. An organizer will review.")
    if _is_htmx_request(request):
        return match_detail(request, pk=pk)
    return _redirect_to_match_detail(request, pk)


@login_required
@require_POST
def resolve_dispute(request, pk):
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can resolve disputes.")
        return _redirect_to_match_detail(request, pk)
    match = get_object_or_404(Match, pk=pk)
    if not _can_manage_tournament(request.user, match.tournament):
        messages.error(request, "You do not manage that tournament.")
        return _redirect_to_match_detail(request, pk)
    score1 = request.POST.get("final_score_team1")
    score2 = request.POST.get("final_score_team2")
    resolution_notes = request.POST.get("resolution_notes", "").strip()
    if match.critical_dispute and not resolution_notes:
        messages.error(request, "Critical-stage disputes require resolution notes.")
        return _redirect_to_match_detail(request, pk)
    if score1 is None or score2 is None:
        messages.error(
            request, "Enter the final score for both sides to resolve this dispute."
        )
        return _redirect_to_match_detail(request, pk)
    if score1 is not None and score2 is not None:
        try:
            final_score1 = int(score1)
            final_score2 = int(score2)
        except (TypeError, ValueError):
            messages.error(request, "Scores must be valid whole numbers.")
            return _redirect_to_match_detail(request, pk)
        if final_score1 < 0 or final_score2 < 0:
            messages.error(request, "Scores cannot be negative.")
            return _redirect_to_match_detail(request, pk)
        tournament = match.tournament
        is_elimination = tournament.format in ("knockout", "double_elimination", "consolation") or (
            tournament.format == "hybrid" and not match.group
        )
        if is_elimination and final_score1 == final_score2:
            messages.error(request, "Draws are not allowed in elimination matches.")
            return _redirect_to_match_detail(request, pk)

        match.score_team1 = final_score1
        match.score_team2 = final_score2
        if not _lock_match_score(match, confirmed_by_user=None):
            messages.error(request, "Draws are not allowed in elimination matches.")
            return _redirect_to_match_detail(request, pk)
        match.dispute_resolution_notes = resolution_notes
        match.dispute_resolved_at = timezone.now()
        match.notes += f"\nResolved by organizer."
        if resolution_notes:
            match.notes += f"\nResolution notes: {resolution_notes}"
        match.save()
        log_action(request, "dispute_resolved",
                   f"Dispute resolved for {_match_display_str(match)}: {match.score_team1}-{match.score_team2}",
                   tournament=tournament)
        messages.success(request, "Dispute resolved. Match marked done.")
    if _is_htmx_request(request):
        return match_detail(request, pk=pk)
    return _redirect_to_match_detail(request, pk)


@login_required
@require_POST
def override_match_result(request, pk):
    """Organizer override of a completed/forfeited RR or hybrid group-stage match result."""
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can override match results.")
        return _redirect_to_match_detail(request, pk)
    match = get_object_or_404(Match, pk=pk)
    if not _can_manage_tournament(request.user, match.tournament):
        messages.error(request, "You do not manage that tournament.")
        return _redirect_to_match_detail(request, pk)
    if not _can_override_match(match):
        messages.error(request, "This match cannot be overridden. It may be a knockout match or the knockout phase has already started.")
        return _redirect_to_match_detail(request, pk)

    score1 = request.POST.get("override_score_team1", "").strip()
    score2 = request.POST.get("override_score_team2", "").strip()
    reason = request.POST.get("override_reason", "").strip()

    try:
        s1, s2 = int(score1), int(score2)
    except (TypeError, ValueError):
        messages.error(request, "Scores must be valid whole numbers.")
        return _redirect_to_match_detail(request, pk)
    if s1 < 0 or s2 < 0:
        messages.error(request, "Scores cannot be negative.")
        return _redirect_to_match_detail(request, pk)

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
    # An override can be the result that completes the tournament.
    _check_and_finalize_tournament(match.tournament)

    log_action(
        request,
        "match_result_overridden",
        f"Match #{match.match_number} result overridden to {s1}-{s2}"
        + (f", winner={match.winner.name}" if match.winner else ", draw")
        + (f". Reason: {reason}" if reason else ""),
        tournament=match.tournament,
    )
    messages.success(request, f"Match result updated to {s1}–{s2}.")
    if _is_htmx_request(request):
        return match_detail(request, pk=pk)
    return _redirect_to_match_detail(request, pk)


# -- Rescheduling --

@login_required
@require_POST
def request_reschedule(request, pk):
    match = get_object_or_404(Match, pk=pk)
    _expire_no_show_reports(match.tournament)
    team = _get_team(request.user, match.tournament)
    if not team or (match.team1 != team and match.team2 != team):
        messages.error(request, "Not a participant.")
        return _redirect_to_match_detail(request, pk)
    if not _can_manage_reschedule(request.user, match.tournament, team):
        if match.tournament.registration_mode == "individual":
            messages.error(request, "Only participants in this match can request rescheduling.")
        else:
            messages.error(request, "Only the team captain can request rescheduling.")
        return _redirect_to_match_detail(request, pk)
    if match.tournament.status != "active":
        messages.error(request, "Rescheduling is not available until the tournament has started.")
        return _redirect_to_match_detail(request, pk)
    if match.status not in ("upcoming",):
        messages.error(request, "Only upcoming matches can be rescheduled.")
        return _redirect_to_match_detail(request, pk)
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
            return _redirect_to_match_detail(request, pk)

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
            return _redirect_to_match_detail(request, pk)
        RescheduleRequest.objects.create(
            match=match, requested_by=request.user, new_time=new_dt,
            new_court=new_court, reason=form.cleaned_data.get("reason", ""),
        )
        resolved = match.no_show_reports.filter(status="pending", absent_team=team)
        had_pending_no_show = resolved.exists()
        if had_pending_no_show:
            resolved.update(status="resolved", resolved_at=timezone.now())
        log_action(request, "reschedule_requested",
                   f"Reschedule requested for {_match_display_str(match)} to {new_dt}",
                   tournament=match.tournament)
        if had_pending_no_show:
            messages.success(request, "Reschedule request sent. The pending no-show notice has been cleared.")
        else:
            messages.success(request, "Reschedule request sent.")
    else:
        for errs in form.errors.values():
            for err in errs:
                messages.error(request, err)
    if _is_htmx_request(request):
        return match_detail(request, pk=pk)
    return _redirect_to_match_detail(request, pk)


@login_required
@require_POST
def respond_reschedule(request, pk):
    rr = get_object_or_404(RescheduleRequest, pk=pk)
    team = _get_team(request.user, rr.match.tournament)
    match = rr.match
    if not team or rr.requested_by == request.user:
        messages.error(request, "Cannot respond to your own request.")
        return _redirect_to_match_detail(request, match.pk)
    if match.team1 != team and match.team2 != team:
        messages.error(request, "Not a participant.")
        return _redirect_to_match_detail(request, match.pk)
    if not _can_manage_reschedule(request.user, match.tournament, team):
        if match.tournament.registration_mode == "individual":
            messages.error(request, "Only participants in this match can approve or reject reschedule requests.")
        else:
            messages.error(request, "Only the team captain can approve or reject reschedule requests.")
        return _redirect_to_match_detail(request, match.pk)
    action = request.POST.get("action")

    # A request that has already been answered must not be answered again: the
    # reschedule has been applied to the match, so flipping the request's status
    # afterwards leaves the audit trail and the schedule disagreeing.
    if rr.status != "pending":
        messages.info(
            request,
            f"That reschedule request was already {rr.get_status_display().lower()}.",
        )
        return _redirect_to_match_detail(request, match.pk)

    if action == "approve":
        duration = timedelta(minutes=match.tournament.default_match_duration)
        target_court = rr.new_court or match.court
        end_dt = rr.new_time + duration
        active_match_statuses = ["upcoming", "in_progress", "pending_confirmation", "disputed"]

        # Conflicts were only checked when the request was created. Two pending
        # requests can target the same free slot, so re-check at approval time.
        court_conflict = Match.objects.filter(
            tournament=match.tournament,
            court=target_court,
            scheduled_time__lt=end_dt,
            scheduled_end_time__gt=rr.new_time,
            status__in=active_match_statuses,
        ).exclude(pk=match.pk).exists()

        team_conflict = Match.objects.filter(
            tournament=match.tournament,
            scheduled_time__lt=end_dt,
            scheduled_end_time__gt=rr.new_time,
            status__in=active_match_statuses,
        ).filter(
            Q(team1=match.team1) | Q(team2=match.team1)
            | Q(team1=match.team2) | Q(team2=match.team2)
        ).exclude(pk=match.pk).exists()

        if court_conflict or team_conflict:
            rr.status = "cancelled"
            rr.responded_at = timezone.now()
            rr.save(update_fields=["status", "responded_at"])
            messages.error(
                request,
                "That slot is no longer free — the request has been cancelled. "
                "Please submit a new one.",
            )
            return _redirect_to_match_detail(request, match.pk)

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
        log_action(request, "reschedule_approved", f"Reschedule approved for {_match_display_str(match)}",
                   tournament=match.tournament)
        messages.success(request, "Reschedule approved!")
    elif action == "reject":
        rr.status = "rejected"
        rr.responded_at = timezone.now()
        rr.save()
        log_action(request, "reschedule_rejected", f"Reschedule rejected for {_match_display_str(match)}",
                   tournament=match.tournament)
        messages.info(request, "Reschedule rejected.")
    if _is_htmx_request(request):
        return match_detail(request, pk=match.pk)
    return _redirect_to_match_detail(request, match.pk)


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
        return _redirect_to_match_detail(request, pk)
    if not _is_captain(request.user, team) and not _is_organizer(request.user):
        messages.error(request, "Only the team captain can report a no-show.")
        return _redirect_to_match_detail(request, pk)
    if match.status not in ("upcoming", "in_progress"):
        messages.error(request, "No-shows can only be reported for active or upcoming matches.")
        return _redirect_to_match_detail(request, pk)
    if not match.scheduled_time or match.scheduled_time > timezone.now():
        messages.error(request, "No-shows can only be reported after the scheduled match time has passed.")
        return _redirect_to_match_detail(request, pk)
    if match.no_show_reports.filter(status="pending").exists():
        messages.warning(request, "A no-show notice is already pending for this match.")
        return _redirect_to_match_detail(request, pk)

    no_show_team_id = request.POST.get("no_show_team")
    opponent = match.get_opponent(team)
    if not opponent or str(opponent.pk) != str(no_show_team_id):
        messages.error(request, "You can only report your opponent as a no-show.")
        return _redirect_to_match_detail(request, pk)

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
    return _redirect_to_match_detail(request, pk)


@login_required
@require_POST
def mark_no_show(request, pk):
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can mark no-shows.")
        return _redirect_to_match_detail(request, pk)

    match = get_object_or_404(Match, pk=pk)
    if not _can_manage_tournament(request.user, match.tournament):
        messages.error(request, "You do not manage that tournament.")
        return _redirect_to_match_detail(request, pk)
    if match.status not in ("upcoming", "in_progress", "pending_confirmation"):
        messages.error(request, "No-show can only be recorded for active/upcoming matches.")
        return _redirect_to_match_detail(request, pk)
    if not match.scheduled_time or match.scheduled_time > timezone.now():
        messages.error(request, "No-show can only be recorded after the scheduled match time has passed.")
        return _redirect_to_match_detail(request, pk)

    no_show_team_id = request.POST.get("no_show_team")
    if str(match.team1_id) == str(no_show_team_id):
        loser = match.team1
        winner = match.team2
    elif str(match.team2_id) == str(no_show_team_id):
        loser = match.team2
        winner = match.team1
    else:
        messages.error(request, "Invalid team selected for no-show.")
        return _redirect_to_match_detail(request, pk)

    if not winner:
        messages.error(request, "Cannot mark no-show: opponent not assigned.")
        return _redirect_to_match_detail(request, pk)

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
    return _redirect_to_match_detail(request, pk)


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
