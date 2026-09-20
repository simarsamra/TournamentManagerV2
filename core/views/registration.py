"""Joining tournaments, registration review and participant seeding."""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..models import (
    TeamMembership,
    TeamTournamentParticipation,
    Tournament,
    TournamentIndividualRegistration,
    TournamentSubstitute,
)
from ..standings import advance_loser_to_third_place, advance_winner
from ..audit import log_action
from ..services.enrollment import active_participant_count, is_registration_capacity_reached

from .helpers import (
    _can_manage_tournament,
    _check_and_finalize_tournament,
    _get_individual_registration,
    _get_tournament,
    _get_user_tournament_ids,
    _htmx_or_redirect,
    _is_organizer,
    _is_user_enrolled_in_tournament,
    _notify,
    _render_refreshable_page,
    _sync_participation_status,
    _sync_registration_status,
    _tournament_context,
)
from .tournaments import tournament_config



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

    context = {
        "tournament": tournament,
        "team_list": team_list,
        "participant_list": participant_list,
        "user_team": user_team,
        "user_registration": user_registration,
        "players_per_team": players_per_team,
        "registration_mode": registration_mode,
        "registration_full": is_registration_capacity_reached(tournament),
        **_tournament_context(request, tournament),
    }
    return _render_refreshable_page(
        request,
        "core/join_tournament.html",
        "core/partials/join_tournament_content.html",
        context,
    )


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
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")

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

    context = {
        "tournament": tournament,
        "registrations": registrations,
        "team_regs": team_regs,
        **_tournament_context(request, tournament),
    }
    return _render_refreshable_page(
        request,
        "core/registration_review.html",
        "core/partials/registration_review_content.html",
        context,
    )


@login_required
@require_POST
def approve_registration(request, tournament_pk, reg_pk):
    """Organizer approves a pending registration (3.8)."""
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can approve registrations.")
        return redirect("dashboard")

    tournament = get_object_or_404(Tournament, pk=tournament_pk)
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")

    # Try individual registration first, then team participation
    reg = (
        TournamentIndividualRegistration.objects.filter(pk=reg_pk, tournament=tournament).first()
        or TeamTournamentParticipation.objects.filter(pk=reg_pk, tournament=tournament, team__is_internal=False).first()
    )
    if not reg:
        messages.error(request, "Registration not found.")
        return redirect("registration_review", pk=tournament_pk)

    reg.status = "active"
    reg.withdrawn_at = None
    reg.save(update_fields=["status", "withdrawn_at", "updated_at"])
    if isinstance(reg, TournamentIndividualRegistration):
        _sync_registration_status(reg)
    else:
        _sync_participation_status(reg)

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
    return _htmx_or_redirect(request, registration_review_view, "registration_review", pk=tournament_pk)


@login_required
@require_POST
def reject_registration(request, tournament_pk, reg_pk):
    """Organizer rejects a pending registration (3.9)."""
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can reject registrations.")
        return redirect("dashboard")

    tournament = get_object_or_404(Tournament, pk=tournament_pk)
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")
    reason = request.POST.get("reason", "").strip()

    reg = (
        TournamentIndividualRegistration.objects.filter(pk=reg_pk, tournament=tournament).first()
        or TeamTournamentParticipation.objects.filter(pk=reg_pk, tournament=tournament, team__is_internal=False).first()
    )
    if not reg:
        messages.error(request, "Registration not found.")
        return redirect("registration_review", pk=tournament_pk)

    reg.status = "withdrawn"
    reg.withdrawn_at = timezone.now()
    reg.save(update_fields=["status", "withdrawn_at", "updated_at"])
    if isinstance(reg, TournamentIndividualRegistration):
        _sync_registration_status(reg)
    else:
        _sync_participation_status(reg)

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
    return _htmx_or_redirect(request, registration_review_view, "registration_review", pk=tournament_pk)


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
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")
    participation = get_object_or_404(
        TeamTournamentParticipation, pk=participation_pk, tournament=tournament
    )
    reason = request.POST.get("reason", "").strip()

    participation.status = "withdrawn"
    participation.withdrawn_at = timezone.now()
    participation.save(update_fields=["status", "withdrawn_at", "updated_at"])
    _sync_participation_status(participation)

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
                from ..standings import advance_winner, advance_loser_to_third_place
                advance_winner(match)
                advance_loser_to_third_place(match)
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
    return _htmx_or_redirect(request, registration_review_view, "registration_review", pk=tournament_pk)


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
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")

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
            return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)

        content_type = request.META.get("CONTENT_TYPE", "")
        if "application/json" in content_type:
            try:
                data = json.loads(request.body)
                raw_seeds = data.get("seeds", {})
            except (json.JSONDecodeError, AttributeError):
                return JsonResponse({"error": "Invalid JSON"}, status=400)
            if not isinstance(raw_seeds, dict):
                return JsonResponse({"error": "'seeds' must be an object"}, status=400)
            # JSON object keys are strings; the apply loop looks them up by the
            # participant's integer pk, so normalise both sides here.
            seeds = {}
            for key, val in raw_seeds.items():
                try:
                    seeds[int(key)] = int(val)
                except (TypeError, ValueError):
                    return JsonResponse(
                        {"error": f"Invalid seed entry: {key!r} -> {val!r}"}, status=400
                    )
        else:
            seeds = {}
            for key, val in request.POST.items():
                if key.startswith("seed_"):
                    try:
                        p_pk = int(key[5:])
                        seeds[p_pk] = int(val)
                    except ValueError:
                        pass

        applied = 0
        for p in participants:
            new_seed = seeds.get(p.pk)
            if new_seed is not None and new_seed != p.seed:
                p.seed = new_seed
                p.save(update_fields=["seed"])
                applied += 1

        log_action(
            request, "seeds_updated",
            f"Seeds updated for '{tournament.name}' ({applied} changed)",
            tournament=tournament,
        )
        messages.success(request, f"Seeds saved ({applied} changed).")
        return _htmx_or_redirect(request, tournament_config, "tournament_config", pk=pk)

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
    if not _can_manage_tournament(request.user, tournament):
        messages.error(request, "You do not manage that tournament.")
        return redirect("dashboard")
    participation = get_object_or_404(TeamTournamentParticipation, pk=participation_pk, tournament=tournament)
    team = participation.team

    current_subs = TournamentSubstitute.objects.filter(
        participation=participation
    ).select_related("user")

    if request.method == "POST":
        action = request.POST.get("action", "add")

        if action == "remove":
            sub_pk = request.POST.get("sub_pk")
            if sub_pk:
                sub_membership = TournamentSubstitute.objects.filter(
                    pk=sub_pk, participation=participation
                ).select_related("user").first()
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
                elif TournamentSubstitute.objects.filter(
                    participation=participation, user=target_user
                ).exists():
                    messages.error(
                        request, f"'{username}' is already a substitute for this team."
                    )
                elif _is_user_enrolled_in_tournament(target_user, tournament):
                    messages.error(
                        request,
                        f"'{username}' already competes in '{tournament.name}' with "
                        f"another team.",
                    )
                else:
                    TournamentSubstitute.objects.create(
                        participation=participation,
                        user=target_user,
                        added_by=request.user,
                    )
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
