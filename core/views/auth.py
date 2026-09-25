"""Sign-in, registration, profile and the dashboard."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db.models import Q
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .. import analytics
from ..models import (
    Match,
    NoShowReport,
    RescheduleRequest,
    TeamTournamentParticipation,
    Tournament,
)
from ..forms import AccountRegistrationForm, ProfileUpdateForm, SelfPasswordChangeForm
from ..standings import calculate_standings, get_third_place_match
from ..audit import log_action, _client_ip
from ..services.enrollment import active_participant_count

from .helpers import (
    LOGIN_ATTEMPTS_PER_ACCOUNT,
    LOGIN_ATTEMPTS_PER_IP,
    _expire_no_show_reports,
    _expire_pending_score_disputes,
    _get_individual_registration,
    _get_team,
    _get_tournament,
    _has_dual_roles,
    _is_captain,
    _is_organizer,
    _is_user_enrolled_in_tournament,
    _manageable_tournaments,
    _render_refreshable_page,
    _safe_next_url,
    _team_display_label,
    _throttle_bump,
    _throttle_clear,
    _throttle_get,
    _tournament_context,
    throttled,
)



# -- Auth Views --

def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if request.method == "POST":
        ip = _client_ip(request) or "unknown"
        username = request.POST.get("username", "").strip()
        # Two counters: one per IP (blunt) and one per account, so spraying one
        # password across many usernames from a single IP still trips a limit,
        # and one account cannot be brute-forced from many IPs.
        ip_key = f"login_attempts_ip_{ip}"
        user_key = f"login_attempts_user_{username.lower()}"

        if _throttle_get(ip_key) >= LOGIN_ATTEMPTS_PER_IP or (
            username and _throttle_get(user_key) >= LOGIN_ATTEMPTS_PER_ACCOUNT
        ):
            messages.error(request, "Too many failed login attempts. Please wait 5 minutes before trying again.")
            return render(request, "core/login.html")
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user:
            _throttle_clear(ip_key, user_key)
            login(request, user)
            # Clear tournament selection on login to ensure dashboard defaults to active tournament
            if "selected_tournament_id" in request.session:
                del request.session["selected_tournament_id"]
            log_action(request, "login", f"User '{username}' logged in")
            return redirect("dashboard")
        # Fixed window: only set the TTL on the first failure, so the window
        # expires instead of sliding forward on every attempt. incr() preserves
        # the existing TTL.
        for key in (ip_key, user_key) if username else (ip_key,):
            _throttle_bump(key)
        messages.error(request, "Invalid credentials.")
    return render(request, "core/login.html")


@require_POST
def logout_view(request):
    """POST only: a GET logout is CSRF-exempt, so any third-party page could
    log a user out with an <img src="/logout/">."""
    if request.user.is_authenticated:
        log_action(request, "logout", f"User '{request.user.username}' logged out")
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


@throttled("account_register", limit=5, window=3600)
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




# -- Dashboard --

def _news_board_context(user, tournament):
    """The tournament's news board: the latest AI news update, written once
    by the worker for everyone (ai/recap.py), and the next fixtures. Shown
    to whoever may view the tournament's analytics, like the recap card."""
    from ..ai.recap import COMING_UP, fixture_when, latest_recap, upcoming_fixtures

    allowed, _ = analytics.can_view_analytics(user, tournament)
    if not allowed:
        return {}
    coming_up = []
    for match in upcoming_fixtures(tournament)[:COMING_UP]:
        coming_up.append({
            "match": match,
            "team1": _team_display_label(tournament, match.team1),
            "team2": _team_display_label(tournament, match.team2),
            "when": fixture_when(match),
        })
    return {"show_news_board": True, "news": latest_recap(tournament), "news_coming_up": coming_up}


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
                # For hybrid format, the actual champion is the knockout-phase winner
                # (tournament.champion), not standings[0] which reflects group-stage RR.
                if tournament.format == "hybrid" and tournament.champion:
                    context["tournament_champion"] = tournament.champion
                    context["tournament_champion_label"] = _team_display_label(tournament, tournament.champion)
                    # Runner-up is the other finalist (lost the final), not standings position
                    _final = (
                        tournament.matches
                        .filter(bracket_type="winners", next_match__isnull=True,
                                group="", team1__isnull=False, team2__isnull=False,
                                status="confirmed")
                        .order_by("-round_number")
                        .first()
                    )
                    if _final and _final.winner:
                        _runner_up = _final.team2 if _final.winner == _final.team1 else _final.team1
                    else:
                        _runner_up = None
                    context["tournament_runner_up_1"] = _runner_up
                    context["tournament_runner_up_1_label"] = (
                        _team_display_label(tournament, _runner_up) if _runner_up else None
                    )
                    # 3rd place: winner of the third-place match (if one exists)
                    _third_match = get_third_place_match(tournament)
                    if _third_match and _third_match.status == "confirmed" and _third_match.winner:
                        _third = _third_match.winner
                    else:
                        _third = None
                    context["tournament_runner_up_2"] = _third
                    context["tournament_runner_up_2_label"] = (
                        _team_display_label(tournament, _third) if _third else None
                    )
                else:
                    context["tournament_champion"] = standings[0]["team"] if standings else tournament.champion
                    context["tournament_champion_label"] = standings[0].get("display_label") or _team_display_label(
                        tournament, standings[0]["team"]
                    )
                    context["tournament_runner_up_1"] = standings[1]["team"] if len(standings) > 1 else None
                    context["tournament_runner_up_2"] = standings[2]["team"] if len(standings) > 2 else None
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
        from ..models import TeamTournamentCourtPreference
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
        all_tournaments = _manageable_tournaments(request.user)
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
    if tournament and settings.AI_ANALYTICS_ENABLED and tournament.status in ("active", "completed"):
        context.update(_news_board_context(request.user, tournament))
    context.update(_tournament_context(request, tournament))
    return _render_refreshable_page(
        request,
        "core/dashboard.html",
        "core/partials/dashboard_content.html",
        context,
    )


@login_required
@require_POST
def select_tournament(request):
    tournament_id = request.POST.get("tournament_id")
    next_url = _safe_next_url(request)
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
