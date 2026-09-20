"""Teams, rosters, invitations and captaincy."""
"""Core views for tournament management."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ..models import (
    Match,
    Team,
    TeamInvite,
    TeamMembership,
    TeamTournamentCourtPreference,
    TeamTournamentParticipation,
    Tournament,
    TournamentIndividualRegistration,
)
from ..forms import (
    CreateTeamForm,
    ExistingTeamMemberForm,
    StandaloneTeamForm,
    TeamMemberInviteForm,
    TeamPreferencesForm,
    password_strength_errors,
)
from ..withdrawals import handle_withdrawal
from ..audit import log_action
from ..services.enrollment import active_participant_count, is_registration_capacity_reached

from .helpers import (
    _check_roster_minimum,
    _ensure_shadow_team_for_registration,
    _get_team,
    _get_tournament,
    _htmx_or_redirect,
    _is_captain,
    _is_htmx_request,
    _is_organizer,
    _is_user_enrolled_in_tournament,
    _notify,
    _promote_team_participation_when_full,
    _render_refreshable_page,
    _resolve_individual_team_name,
    _roster_conflicts_for_joining,
    _team_display_label,
    _tournament_context,
)



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

    if _is_htmx_request(request):
        return HttpResponse(status=204, headers={"HX-Redirect": reverse("dashboard")})
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
                    return _render_refreshable_page(
                        request,
                        "core/create_team.html",
                        "core/partials/create_team_content.html",
                        {
                            "form": form,
                            "tournament": tournament,
                            **_tournament_context(request, tournament),
                        },
                    )

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
                if _is_htmx_request(request):
                    return HttpResponse(status=204, headers={"HX-Redirect": reverse("dashboard")})
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
                try:
                    team = Team.objects.create(
                        name=team_name,
                        department=form.cleaned_data.get("department", "").strip(),
                        sport_type=tournament.sport_type,
                    )
                except IntegrityError:
                    # Team.name is unique; another request can take the name
                    # between the check above and this insert.
                    form.add_error("team_name", "A team with that name already exists.")
                    return _render_refreshable_page(
                        request,
                        "core/create_team.html",
                        "core/partials/create_team_content.html",
                        {
                            "form": form,
                            "tournament": tournament,
                            "registration_full": _registration_is_full,
                            **_tournament_context(request, tournament),
                        },
                    )
                participation = TeamTournamentParticipation.objects.create(
                    team=team, tournament=tournament, status=initial_status
                )
                TeamMembership.objects.create(team=team, user=request.user, role="captain")
                # The form requires a court selection whenever the tournament has
                # courts; persist it, or _validate_tournament_ready will later
                # block the start on preferences the captain already supplied.
                preferred_courts = form.cleaned_data.get("preferred_courts") or []
                if preferred_courts:
                    TeamTournamentCourtPreference.objects.bulk_create([
                        TeamTournamentCourtPreference(participation=participation, court=court)
                        for court in preferred_courts
                    ])
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

    return _render_refreshable_page(
        request,
        "core/create_team.html",
        "core/partials/create_team_content.html",
        {
            "form": form,
            "tournament": tournament,
            "registration_full": _registration_is_full,
            **_tournament_context(request, tournament),
        },
    )


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
                try:
                    team = Team.objects.create(
                        name=team_name,
                        department=form.cleaned_data.get("department", "").strip(),
                        sport_type=form.cleaned_data.get("sport_type") or "other",
                    )
                except IntegrityError:
                    form.add_error("team_name", "A team with that name already exists.")
                    return render(request, "core/create_standalone_team.html", {
                        "form": form,
                        **_tournament_context(request, _get_tournament(request)),
                    })
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
            participant_list = list(
                tournament.individual_registrations.filter(status="active")
                .select_related("user", "shadow_team")
                .order_by("display_name", "id")
            )
        else:
            teams = Team.objects.filter(
                participations__tournament=tournament,
                participations__status="active",
                is_internal=False,
            ).prefetch_related("players").distinct().order_by("name")
            captain_by_team = dict(
                TeamMembership.objects.filter(
                    team__in=teams, role="captain"
                ).values_list("team_id", "user__username")
            )
            for team in teams:
                participation = team.participations.filter(tournament=tournament).first()
                team.group = participation.group if participation else ""
                team.participation_pk = participation.pk if participation else None
                team.captain_username = captain_by_team.get(team.pk, "")

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
    team_participation = (
        TeamTournamentParticipation.objects.filter(team=team, tournament=tournament).first()
        if tournament else None
    )
    context = {
        "team": team,
        "team_participation": team_participation,
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
    }
    return _render_refreshable_page(
        request,
        "core/team_detail.html",
        "core/partials/team_detail_content.html",
        context,
    )


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

                conflicts = _roster_conflicts_for_joining(existing_user, team)
                if conflicts:
                    for reason in conflicts:
                        messages.error(request, reason)
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
    return _htmx_or_redirect(request, team_detail, "team_detail", pk=pk)


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
    member_user = membership.user
    strength_errors = password_strength_errors(new_password, user=member_user)
    if strength_errors:
        for message in strength_errors:
            messages.error(request, message)
        return redirect("team_detail", pk=pk)
    member_user.set_password(new_password)
    member_user.save()
    log_action(
        request,
        "member_password_reset",
        f"Password reset for member '{member_user.username}' in team '{team.name}'",
        tournament=_get_tournament(request),
    )
    messages.success(request, f"Password for '{member_user.username}' has been reset.")
    return _htmx_or_redirect(request, team_detail, "team_detail", pk=pk)


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
    captain_membership = TeamMembership.objects.filter(team=team, role="captain").select_related("user").first()
    if not captain_membership:
        messages.error(request, "No captain found for this team.")
        return redirect("team_detail", pk=pk)
    captain_user = captain_membership.user
    strength_errors = password_strength_errors(new_password, user=captain_user)
    if strength_errors:
        for message in strength_errors:
            messages.error(request, message)
        return redirect("team_detail", pk=pk)
    captain_user.set_password(new_password)
    captain_user.save()
    log_action(
        request,
        "captain_password_reset",
        f"Password reset for captain '{captain_user.username}' of team '{team.name}'",
        tournament=_get_tournament(request),
    )
    messages.success(request, f"Password for captain '{captain_user.username}' has been reset.")
    return _htmx_or_redirect(request, team_detail, "team_detail", pk=pk)


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
    return _htmx_or_redirect(request, team_detail, "team_detail", pk=pk)


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
    if _is_htmx_request(request):
        return HttpResponse(status=204, headers={"HX-Redirect": reverse("teams")})
    return redirect("teams")


@login_required
def team_preferences(request, pk):
    from ..models import TeamTournamentCourtPreference
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
    if _is_htmx_request(request):
        return HttpResponse(status=204, headers={"HX-Redirect": reverse("dashboard")})
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
    if _is_htmx_request(request):
        return HttpResponse(status=204, headers={"HX-Redirect": reverse("dashboard")})
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
    if _is_htmx_request(request):
        return HttpResponse(status=204, headers={"HX-Redirect": reverse("dashboard")})
    return redirect("dashboard")


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

    conflicts = _roster_conflicts_for_joining(request.user, team)
    if conflicts:
        for reason in conflicts:
            messages.error(request, reason)
        # Leave the invite pending so a freed slot lets them retry.
        return redirect("my_invites")

    TeamMembership.objects.create(team=team, user=request.user, role="member")
    _promote_team_participation_when_full(team, request=request)

    # Update active team
    from ..models import UserTeamAssignment
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
    if _is_htmx_request(request):
        return HttpResponse(status=204, headers={"HX-Redirect": reverse("team_detail", kwargs={"pk": team.pk})})
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
    if _is_htmx_request(request):
        return my_invites_view(request)
    return redirect("notifications")


@login_required
def my_invites_view(request):
    """List all pending team invites for the current user."""
    invites = TeamInvite.objects.filter(
        invited_user=request.user, status="pending"
    ).select_related("team", "invited_by")
    tournament = _get_tournament(request)
    context = {
        "invites": invites,
        **_tournament_context(request, tournament),
    }
    return _render_refreshable_page(
        request,
        "core/my_invites.html",
        "core/partials/my_invites_content.html",
        context,
    )


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
