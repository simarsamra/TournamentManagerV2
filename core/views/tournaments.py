"""Tournament setup, courts, availability and lifecycle transitions."""
import math
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..models import (
    Court,
    CourtAvailability,
    TeamTournamentCourtPreference,
    TeamTournamentParticipation,
    TimeSlot,
    Tournament,
    TournamentIndividualRegistration,
)
from ..forms import (
    BulkTeamFileForm,
    BulkTeamForm,
    CourtAvailabilityForm,
    CourtForm,
    TimeSlotForm,
    TournamentForm,
)
from ..scheduling import (
    _assign_schedule_to_existing,
    count_available_slots,
    estimate_completion_date,
    estimate_required_matches,
    generate_fixtures,
)
from ..standings import _determine_champion, check_group_stage_complete
from ..audit import log_action
from ..services.enrollment import active_participant_count

from .helpers import (
    _auto_end_date,
    _build_capacity_by_date,
    _can_manage_tournament,
    _create_teams_from_data,
    _get_tournament,
    _htmx_or_redirect,
    _infer_end_time,
    _is_htmx_request,
    _is_organizer,
    _notify,
    _parse_match_slots_from_request,
    _parse_team_line,
    _render_refreshable_page,
    _team_display_label,
    _tournament_context,
    _validate_tournament_ready,
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
            t.created_by = request.user
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
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")
    request.session["selected_tournament_id"] = tournament.pk
    team_participations = list(
        TeamTournamentParticipation.objects.filter(tournament=tournament)
        .select_related("team")
        .order_by("team__name")
    )

    required_members = max(1, tournament.players_per_team or 1)
    for participation in team_participations:
        participation.display_name = _team_display_label(tournament, participation.team)
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

    court_availabilities = CourtAvailability.objects.filter(
        court__tournament=tournament
    ).select_related("court")

    availability_warnings = []
    base_date = tournament.start_date or timezone.localdate()

    # _build_slots clamps each row to max(tournament.start_date, row.start_date),
    # so a window that ends before the tournament starts yields no slots at all
    # — silently, until "Not enough court availability" blocks the start.
    stale_rows = [
        availability for availability in court_availabilities
        if availability.end_date and availability.end_date < base_date
    ]
    if stale_rows:
        availability_warnings.append(
            f"{len(stale_rows)} availability entr"
            f"{'y ends' if len(stale_rows) == 1 else 'ies end'} before the tournament "
            f"start date ({base_date}), so "
            f"{'it contributes' if len(stale_rows) == 1 else 'they contribute'} no "
            "schedulable slots. Extend those end dates, or move the tournament "
            "start date earlier."
        )

    if tournament.end_date:
        conflicting_open_rows = 0
        for availability in court_availabilities:
            if availability.end_date:
                continue
            range_start = max(base_date, availability.start_date or base_date)
            if tournament.end_date < range_start:
                conflicting_open_rows += 1
        if conflicting_open_rows:
            availability_warnings.append(
                f"Tournament end date ({tournament.end_date}) is earlier than the effective start date "
                f"for {conflicting_open_rows} open-ended availability entr"
                f"{'y' if conflicting_open_rows == 1 else 'ies'}. "
                "Update the tournament end date or set explicit end dates on those entries."
            )

    availability_date_warning = " ".join(availability_warnings)

    available_slots = count_available_slots(tournament)
    active_count = active_participant_count(tournament)
    required_matches = estimate_required_matches(tournament, team_count=active_count)
    active_teams_count = active_count

    context = {
        "tournament": tournament,
        "tournament_champion_label": (
            _team_display_label(tournament, tournament.champion) if tournament.champion else ""
        ),
        "courts": tournament.courts.all(),
        "court_availabilities": court_availabilities,
        "availability_date_warning": availability_date_warning,
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
    }
    return _render_refreshable_page(
        request,
        "core/tournament_config.html",
        "core/partials/tournament_config_content.html",
        context,
    )


@login_required
@require_POST
def proceed_to_knockout_view(request, pk):
    """Admin action: seed advancing teams into the knockout bracket."""
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")
    if tournament.format != "hybrid" or tournament.status != "active":
        messages.error(request, "Knockout phase can only be triggered for an active hybrid tournament.")
        return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)

    result = check_group_stage_complete(tournament)
    if result:
        messages.success(request, "Knockout phase started! Teams have been seeded into the bracket based on group standings.")
        log_action(request, "knockout_phase_started", "Admin triggered knockout phase progression", tournament=tournament)
    else:
        messages.error(request, "Cannot proceed: group stage is not yet complete, or the knockout bracket is already populated.")
    return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)


@login_required
def add_court(request, pk):
    if request.method != "POST":
        return redirect("tournament_config", pk=pk)
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")
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
            return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)
        log_action(request, "court_added", f"Court '{court.name}' added", tournament=tournament)
        messages.success(request, f"Court '{court.name}' added.")
    else:
        for error in form.errors.get("name", []):
            messages.error(request, error)
    return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)


@login_required
@require_POST
def delete_court_availability(request, pk, availability_pk):
    """Organiser deletes a single court availability record."""
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")
    availability = get_object_or_404(CourtAvailability, pk=availability_pk, court__tournament=tournament)
    label = str(availability)
    availability.delete()
    log_action(request, "court_availability_deleted", f"Deleted availability: {label}", tournament=tournament)
    messages.success(request, f"Availability '{label}' removed.")
    
    # For HTMX requests, return empty response so hx-swap="delete" removes the element
    if _is_htmx_request(request):
        return HttpResponse("")
    
    return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)


@login_required
@require_POST
def estimate_court_availability_end_date(request, pk):
    def _availability_response(payload, status=200):
        if _is_htmx_request(request):
            return render(
                request,
                "core/partials/availability_estimate_result.html",
                payload,
                status=status,
            )
        return JsonResponse(payload, status=status)

    if not _is_organizer(request.user):
        return _availability_response({"status": "error", "message": "Not authorized."}, status=403)
    tournament = get_object_or_404(Tournament, pk=pk)
    if not _can_manage_tournament(request.user, tournament):
        return _availability_response(
            {"status": "error", "message": "Not authorized."}, status=403
        )
    form = CourtAvailabilityForm(request.POST, tournament=tournament)
    if not form.is_valid():
        if "courts" in form.errors:
            error_message = form.errors["courts"][0]
        elif "weekdays" in form.errors:
            error_message = "Select at least one weekday."
        else:
            first_error = ""
            for errs in form.errors.values():
                if errs:
                    first_error = errs[0]
                    break
            error_message = first_error or "Please correct the availability details and try again."
        return _availability_response({"status": "error", "message": error_message, "errors": form.errors}, status=200)

    courts = list(form.cleaned_data["courts"])
    weekdays = [int(day) for day in form.cleaned_data["weekdays"]]
    start_time = form.cleaned_data["start_time"]
    matches_per_court_per_day = form.cleaned_data.get("matches_per_court_per_day")
    start_date = form.cleaned_data.get("start_date") or tournament.start_date or timezone.localdate()
    if not courts:
        return _availability_response({"status": "error", "message": "Select at least one court."}, status=200)
    if not weekdays:
        return _availability_response({"status": "error", "message": "Select at least one weekday."}, status=200)

    duration = max(1, tournament.default_match_duration or 35)
    match_slots, parse_error = _parse_match_slots_from_request(request, duration)
    if parse_error:
        return _availability_response({"status": "error", "message": parse_error}, status=200)
    if match_slots is not None:
        daily_slots_per_court = len(match_slots)
    else:
        inferred_end_time = _infer_end_time(start_time, matches_per_court_per_day, duration)
        if inferred_end_time is None:
            return _availability_response({"status": "error", "message": "The selected number of matches does not fit in a single day from the chosen start time."}, status=200)
        daily_slots_per_court = matches_per_court_per_day

    active_count = active_participant_count(tournament)
    team_count = active_count or tournament.expected_teams_count or 0
    if team_count < 2:
        return _availability_response({"status": "error", "message": "Need at least 2 participants or teams in the tournament to estimate an end date. Add entries or set the expected count."}, status=200)

    required_matches = estimate_required_matches(tournament, team_count=team_count)
    weekly_slots = daily_slots_per_court * len(courts) * len(weekdays)
    if weekly_slots <= 0:
        return _availability_response({"status": "error", "message": "The selected schedule does not produce any available slots."}, status=200)

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
        return _availability_response({"status": "error", "message": "Could not estimate an end date from the selected availability. Try a longer daily window or more weekdays."}, status=200)

    weeks_needed = math.ceil(required_matches / max(1, weekly_slots))
    message = (
        f"Estimated end date: {estimated_end_date} — {required_matches} match{'' if required_matches == 1 else 'es'} requires about {weeks_needed} week{'' if weeks_needed == 1 else 's'} of selected availability. "
        f"Using {daily_slots_per_court} slot{'' if daily_slots_per_court == 1 else 's'} per court per day across {len(courts)} court{'' if len(courts) == 1 else 's'} and {len(weekdays)} weekday{'' if len(weekdays) == 1 else 's'}."
    )
    if active_count == 0 and tournament.expected_teams_count:
        message += f" Using expected count of {tournament.expected_teams_count}."

    return _availability_response({
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
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")
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
            return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)

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
                    return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)
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
    return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)


@login_required
@require_POST
def add_timeslot(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")
    form = TimeSlotForm(request.POST, tournament=tournament)
    if form.is_valid():
        date = form.cleaned_data["date"]
        start = form.cleaned_data["start_time"]
        end = form.cleaned_data["end_time"]
        court = form.cleaned_data.get("court")
        if end <= start:
            messages.error(request, "End time must be after start time.")
            return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)
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
    else:
        for errs in form.errors.values():
            for err in errs:
                messages.error(request, err)
    return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)


@login_required
@require_POST
def add_teams_bulk(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")

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
            return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)
        content = uploaded.read(MAX_UPLOAD_BYTES + 1).decode("utf-8", errors="ignore")
        lines = content.split("\n")
        if len(lines) > MAX_LINES:
            messages.error(request, f"File has too many lines. Maximum is {MAX_LINES} teams.")
            return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)
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

    return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)


@login_required
def estimate_tournament_end_date(request, pk):
    """Return a JSON estimate of when the tournament will finish."""
    def _end_date_response(payload, status=200):
        if _is_htmx_request(request):
            return render(
                request,
                "core/partials/tournament_end_date_estimate.html",
                payload,
                status=status,
            )
        return JsonResponse(payload, status=status)

    if not _is_organizer(request.user):
        return _end_date_response({"error": "Unauthorized"}, status=403)
    tournament = get_object_or_404(Tournament, pk=pk)
    if not _can_manage_tournament(request.user, tournament):
        return _end_date_response({"error": "Unauthorized"}, status=403)

    # Determine team count: prefer actual active teams, fall back to expected count
    team_count = active_participant_count(tournament)
    if team_count < 2 and (tournament.expected_teams_count or 0) >= 2:
        team_count = tournament.expected_teams_count

    if team_count < 2:
        return _end_date_response({"error": "Need at least 2 teams to estimate."})

    required_matches = estimate_required_matches(tournament, team_count=team_count)
    if required_matches == 0:
        return _end_date_response({"error": "Unable to estimate matches for this format."})

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

    return _end_date_response({
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
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")
    participation = get_object_or_404(TeamTournamentParticipation, pk=participation_pk, tournament=tournament)
    if tournament.status in ("active", "completed"):
        messages.error(
            request,
            "Cannot remove a competitor from an active or completed tournament. "
            "Use 'Withdraw' instead so remaining matches are forfeited or voided.",
        )
        return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)
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
    return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)


@login_required
@require_POST
def open_registration(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")
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
    return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)


@login_required
@require_POST
def close_registration(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")

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
        return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)

    tournament.status = "ready"
    tournament.save(update_fields=["status"])
    log_action(request, "registration_closed", f"Registration closed for '{tournament.name}'", tournament=tournament)
    messages.success(request, "Registration closed. The tournament is ready for scheduling checks.")
    return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)


@login_required
@require_POST
def generate_schedule(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")
    readiness_errors = _validate_tournament_ready(tournament)
    if readiness_errors:
        for error in readiness_errors:
            messages.error(request, error)
        return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)
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
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")
    if tournament.status != "scheduled":
        readiness_errors = _validate_tournament_ready(tournament)
        if readiness_errors:
            for error in readiness_errors:
                messages.error(request, error)
            return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)
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
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")
    if tournament.status != "active":
        messages.error(request, "Only active tournaments can be marked as completed.")
        return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)
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
    return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)


# -- Settings --

@login_required
@require_POST
def delete_tournament(request, pk):
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can delete tournaments.")
        return redirect("dashboard")

    tournament = get_object_or_404(Tournament, pk=pk)
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")
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
    if _is_htmx_request(request):
        return HttpResponse(status=204, headers={"HX-Redirect": reverse("dashboard")})
    return redirect("dashboard")


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
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")

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
    if _is_htmx_request(request):
        return HttpResponse(status=204, headers={"HX-Redirect": reverse("dashboard")})
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
    if not _can_manage_tournament(request.user, source):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")
    new_name = f"Copy of {source.name}"

    new_tournament = Tournament.objects.create(
        name=new_name,
        created_by=request.user,
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
        matches_per_court_per_day=source.matches_per_court_per_day,
        enable_third_place_match=source.enable_third_place_match,
    )
    # Set this as the organizer's selected tournament
    request.session["selected_tournament_id"] = new_tournament.pk

    log_action(
        request, "tournament_duplicated",
        f"Tournament '{source.name}' duplicated as '{new_name}'",
        tournament=new_tournament,
    )
    messages.success(request, f"Tournament duplicated as '{new_name}'. Please update the dates and settings.")
    if _is_htmx_request(request):
        return HttpResponse(status=204, headers={"HX-Redirect": reverse("tournament_config", kwargs={"pk": new_tournament.pk})})
    return redirect("tournament_config", pk=new_tournament.pk)


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
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")
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
    if _is_htmx_request(request):
        return HttpResponse(status=204, headers={"HX-Redirect": reverse("dashboard")})
    return redirect("dashboard")


@login_required
@require_POST
def resume_tournament(request, pk):
    """Resume a paused tournament (7.6)."""
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can resume tournaments.")
        return redirect("dashboard")

    tournament = get_object_or_404(Tournament, pk=pk)
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")
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
    if _is_htmx_request(request):
        return HttpResponse(status=204, headers={"HX-Redirect": reverse("dashboard")})
    return redirect("dashboard")


@login_required
@require_POST
def compute_end_date_view(request, pk):
    """Compute and save an auto end date for the tournament, then redirect back to settings."""
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")
    computed = _auto_end_date(tournament)
    if computed:
        tournament.end_date = computed
        tournament.save(update_fields=["end_date"])
        messages.success(request, f"End date computed and set to {computed.strftime('%B %d, %Y')}.")
    else:
        messages.error(request, "Could not compute end date — make sure a start date is set.")
    if _is_htmx_request(request):
        return HttpResponse(status=204, headers={"HX-Redirect": reverse("settings")})
    return redirect("settings")
