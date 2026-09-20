"""Shared helpers for the view modules.

Authorisation predicates, tournament/team lookup, match finalisation and
the small rendering utilities. These are imported by every module in this
package and must not import from them in return.
"""
from collections import defaultdict
from datetime import datetime, timedelta

from django.core.cache import cache as django_cache
from django.contrib import messages
from django.contrib.auth.models import User
from django.db import models as db_models
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from ..models import (
    CourtAvailability,
    Match,
    NoShowReport,
    Notification,
    OpenSlot,
    OrganizerProfile,
    Player,
    Team,
    TeamMembership,
    TeamTournamentCourtPreference,
    TeamTournamentParticipation,
    Tournament,
    TournamentIndividualRegistration,
    TournamentSubstitute,
)
from ..forms import password_strength_errors
from ..scheduling import (
    _assign_schedule_to_existing,
    count_available_slots,
    estimate_completion_date,
    estimate_required_matches,
    generate_consolation_if_ready,
)
from ..standings import (
    _determine_champion,
    advance_loser_to_third_place,
    advance_winner,
    check_group_stage_complete,
)
from ..audit import log_action
from ..services.enrollment import active_participant_count, is_registration_capacity_reached

SEARCH_RESULT_LIMIT = 30
CRITICAL_STAGE_DISPUTE_WINDOW_MINUTES = 10
DEFAULT_DISPUTE_WINDOW_MINUTES = 10
CRITICAL_STAGE_MATCHES_THRESHOLD = 2
LOGIN_ATTEMPTS_PER_IP = 10
LOGIN_ATTEMPTS_PER_ACCOUNT = 5
LOGIN_ATTEMPT_WINDOW_SECONDS = 300

__all__ = [
    "CRITICAL_STAGE_DISPUTE_WINDOW_MINUTES",
    "CRITICAL_STAGE_MATCHES_THRESHOLD",
    "DEFAULT_DISPUTE_WINDOW_MINUTES",
    "LOGIN_ATTEMPTS_PER_ACCOUNT",
    "LOGIN_ATTEMPTS_PER_IP",
    "LOGIN_ATTEMPT_WINDOW_SECONDS",
    "SEARCH_RESULT_LIMIT",
    "_auto_end_date",
    "_build_capacity_by_date",
    "_build_open_slot_choices",
    "_calculate_daily_slots",
    "_can_manage_reschedule",
    "_can_manage_tournament",
    "_can_override_match",
    "_check_and_finalize_tournament",
    "_check_roster_minimum",
    "_claim_participant_slot",
    "_create_open_slot_for_completed_match",
    "_create_teams_from_data",
    "_dispute_window_minutes_for_match",
    "_ensure_shadow_team_for_registration",
    "_estimate_availability_end_date",
    "_expire_no_show_reports",
    "_expire_pending_score_disputes",
    "_finalize_no_show_match",
    "_get_active_team",
    "_get_available_tournaments",
    "_get_individual_registration",
    "_get_team",
    "_get_tournament",
    "_get_user_tournament_ids",
    "_has_dual_roles",
    "_htmx_or_redirect",
    "_infer_end_time",
    "_is_captain",
    "_is_critical_stage_match",
    "_is_htmx_request",
    "_is_organizer",
    "_is_partial_refresh",
    "_is_site_admin",
    "_is_user_enrolled_in_tournament",
    "_is_within_dispute_window",
    "_lock_match_score",
    "_manageable_tournaments",
    "_match_display_str",
    "_notify",
    "_organizer_count",
    "_parse_match_slots_from_request",
    "_parse_team_line",
    "_promote_team_participation_when_full",
    "_public_tournament_context",
    "_render_refreshable_page",
    "_resolve_individual_team_name",
    "_roster_conflicts_for_joining",
    "_safe_next_url",
    "_safe_page_param",
    "_sync_open_slots_for_tournament",
    "_sync_participation_status",
    "_sync_registration_status",
    "_team_display_label",
    "_team_display_map",
    "_throttle_bump",
    "_throttle_clear",
    "_throttle_get",
    "_tournament_context",
    "_validate_tournament_ready",
]

def _throttle_get(key):
    """Read a throttle counter, treating cache failure as "no attempts yet".

    The production cache backend is DatabaseCache, which raises if
    `manage.py createcachetable` was never run. Losing throttling is a
    degradation; refusing every login because of it would be an outage.
    """
    try:
        return django_cache.get(key, 0) or 0
    except Exception:
        return 0


def _throttle_bump(key):
    """Increment a counter on a fixed window, ignoring cache failures."""
    try:
        if django_cache.get(key) is None:
            django_cache.set(key, 1, timeout=LOGIN_ATTEMPT_WINDOW_SECONDS)
        else:
            # incr() preserves the existing TTL, so the window stays fixed
            # rather than sliding forward on every failed attempt.
            django_cache.incr(key)
    except ValueError:
        django_cache.set(key, 1, timeout=LOGIN_ATTEMPT_WINDOW_SECONDS)
    except Exception:
        pass


def _throttle_clear(*keys):
    try:
        for key in keys:
            django_cache.delete(key)
    except Exception:
        pass



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


def _manageable_tournaments(user):
    """Tournaments `user` may administer, ordered like _get_available_tournaments.

    Site admins see everything. A verified organizer sees the ones they created
    plus legacy rows with no recorded creator (see _can_manage_tournament).
    Read-only pages are deliberately not scoped this way — an organizer can
    still look at other tournaments, they just cannot administer them.
    """
    qs = _get_available_tournaments()
    if not _is_organizer(user):
        return qs.none()
    if _is_site_admin(user):
        return qs
    return qs.filter(db_models.Q(created_by=user) | db_models.Q(created_by__isnull=True))


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
            "available_tournaments": _manageable_tournaments(request.user),
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
    # The join yields NULL for a membership in a team with no participations;
    # a None in this set makes the multi-tournament switcher appear for someone
    # enrolled in exactly one.
    ids = {
        tid
        for tid in user.memberships.filter(team__is_internal=False)
        .values_list("team__participations__tournament_id", flat=True)
        .distinct()
        if tid is not None
    }
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


def _sync_registration_status(registration):
    """Push an individual registration's status onto its shadow participation.

    The shadow TeamTournamentParticipation is what the match engine,
    _validate_tournament_ready and the standings read. Changing one side
    without the other leaves a rejected player still scheduled.
    """
    if not registration.shadow_team_id:
        _ensure_shadow_team_for_registration(registration)
        return
    TeamTournamentParticipation.objects.filter(
        team_id=registration.shadow_team_id,
        tournament_id=registration.tournament_id,
    ).update(
        status=registration.status,
        group=registration.group or "",
        seed=registration.seed,
        withdrawn_at=registration.withdrawn_at,
    )


def _sync_participation_status(participation):
    """Push a shadow participation's status back onto its registration.

    The mirror of _sync_registration_status, for organizer actions that operate
    on the participation (disqualification, withdrawal).
    """
    if not participation.team.is_internal:
        return
    TournamentIndividualRegistration.objects.filter(
        shadow_team=participation.team, tournament=participation.tournament
    ).update(
        status=participation.status,
        withdrawn_at=participation.withdrawn_at,
    )


def _team_display_label(tournament, team):
    if not team:
        return "TBD"
    if tournament and tournament.registration_mode == "individual":
        reg = TournamentIndividualRegistration.objects.filter(
            tournament=tournament, shadow_team=team
        ).first()
        if reg:
            return reg.display_name
    if getattr(team, "is_internal", False):
        player_name = team.players.order_by("id").values_list("name", flat=True).first()
        if player_name:
            return player_name
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
                shadow_team_id__in=valid_ids,
            ).values_list("shadow_team_id", "display_name")
        )
        unresolved_ids = [tid for tid in valid_ids if tid not in labels]
        if unresolved_ids:
            internal_ids = set(
                Team.objects.filter(pk__in=unresolved_ids, is_internal=True).values_list("pk", flat=True)
            )
            if internal_ids:
                for team_id, player_name in Player.objects.filter(team_id__in=internal_ids).order_by("id").values_list("team_id", "name"):
                    if team_id not in labels and player_name:
                        labels[team_id] = player_name
        fallback_ids = [tid for tid in valid_ids if tid not in labels]
        if fallback_ids:
            labels.update(dict(Team.objects.filter(pk__in=fallback_ids).values_list("pk", "name")))
        return labels
    return dict(Team.objects.filter(pk__in=valid_ids).values_list("pk", "name"))


def _match_display_str(match):
    """Return a human-readable match description using display labels (not shadow team names)."""
    t1 = _team_display_label(match.tournament, match.team1)
    t2 = _team_display_label(match.tournament, match.team2)
    return f"Match {match.match_number}: {t1} vs {t2}"


def _is_organizer(user):
    """Check if user is an approved organizer.

    This is the authorisation predicate the whole application leans on, so it
    denies rather than raises when handed something that is not a user-like
    object. It no longer swallows *everything*: a database failure used to come
    back here as a plain "not an organizer", which would have taken every
    organizer's tools away with nothing in the logs to say why.
    """
    if not user:
        return False
    try:
        if not user.is_authenticated:
            return False
        if user.is_superuser or user.is_staff:
            return True
        # Django's reverse one-to-one accessor raises RelatedObjectDoesNotExist,
        # which subclasses AttributeError, so hasattr is a valid test for
        # "this user has no organizer profile" -- and the same AttributeError
        # is what a non-user argument produces.
        return hasattr(user, "organizer_profile") and user.organizer_profile.verified
    except AttributeError:
        return False


def _is_site_admin(user):
    """Site administrators: staff or superusers.

    Distinct from "organizer". Organizers run their own tournaments; only site
    admins manage user accounts and reach across tournaments they did not
    create.
    """
    try:
        return bool(user and user.is_authenticated and (user.is_superuser or user.is_staff))
    except AttributeError:
        return False


def _can_manage_tournament(user, tournament):
    """Return True when `user` may administer `tournament`.

    Site admins manage everything. A verified organizer manages the tournaments
    they created — the app has an organizer *application* flow, so organizers
    are independent parties, not a mutually trusted pool.

    Tournaments with no recorded creator predate the created_by field and could
    not be attributed from the audit log during its backfill migration. They
    fall back to any verified organizer so they are not orphaned; once every
    row carries a creator this branch should become `return False`.
    """
    if not _is_organizer(user):
        return False
    if _is_site_admin(user):
        return True
    if tournament is None:
        return False
    if tournament.created_by_id is None:
        return True
    return tournament.created_by_id == user.pk


def _is_captain(user, team=None):
    """Check if user is captain of their active team, or of a specific team if provided."""
    if not user.is_authenticated:
        return False

    if team is None:
        # Fall back to the user's active team. A user with no assignment row
        # raises RelatedObjectDoesNotExist (an AttributeError); that is "no
        # active team", not an error.
        try:
            team = user.team_assignment.active_team
        except AttributeError:
            return False
        if not team:
            return False

    # Deliberately outside the try: a database failure here is a real fault and
    # must surface, not quietly read as "not a captain".
    return TeamMembership.objects.filter(
        user=user, team=team, role="captain"
    ).exists()


def _can_manage_reschedule(user, tournament, team):
    """Return True when user can create/respond to reschedules for the given competitor."""
    if _is_organizer(user):
        return True
    if not user.is_authenticated or not tournament or not team:
        return False
    if tournament.registration_mode == "individual":
        # The competitor is a shadow team; the user must own that registration.
        return TournamentIndividualRegistration.objects.filter(
            tournament=tournament, shadow_team=team, user=user, status="active"
        ).exists()
    return _is_captain(user, team)


def _get_active_team(user):
    """Get user's active team, or None."""
    if not user.is_authenticated:
        return None
    try:
        assignment = user.team_assignment
    except AttributeError:
        # No UserTeamAssignment row for this user.
        return None
    return assignment.active_team or None


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
        if membership:
            return membership.team
        # A substitute acts for their team, but only in the tournament they were
        # added for — that scoping is the whole point of TournamentSubstitute.
        substitute = (
            TournamentSubstitute.objects.filter(
                user=user, participation__tournament=tournament
            )
            .select_related("participation__team")
            .first()
        )
        return substitute.participation.team if substitute else None
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
    qs = OrganizerProfile.objects.filter(verified=True)
    if exclude_user_id is not None:
        qs = qs.exclude(user_id=exclude_user_id)
    return qs.count()


def _safe_next_url(request, default="dashboard"):
    """Return a POSTed/GET 'next' target only when it is local to this site.

    redirect() passes any string containing '/' or '.' straight through, so an
    unvalidated 'next' is an open redirect.
    """
    candidate = (request.POST.get("next") or request.GET.get("next") or "").strip()
    if not candidate:
        return default
    if url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return default


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
    if _is_htmx_request(request):
        return True
    return (
        request.GET.get("partial") == "1"
        and request.headers.get("x-requested-with", "").lower() == "xmlhttprequest"
    )


def _is_htmx_request(request):
    return request.headers.get("HX-Request", "").lower() == "true"


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


def _htmx_or_redirect(request, view_callable, redirect_name, **kwargs):
    if _is_htmx_request(request):
        return view_callable(request, **kwargs)
    return redirect(redirect_name, **kwargs)


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
        advance_loser_to_third_place(match)
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

    elif fmt == "double_elimination":
        # The winners-bracket final settles nothing here: its loser drops into
        # the losers bracket, and it now has a next_match (the grand final), so
        # the winners-final check below would never find it. Completion is the
        # last grand-final match that is still live being confirmed.
        grand_finals = tournament.matches.filter(bracket_type="grand_final")
        if not grand_finals.exists():
            return False
        live = grand_finals.exclude(status="cancelled").order_by("-round_number")
        decider = live.first()
        if not decider or decider.status not in ("confirmed", "forfeited"):
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
    """Minutes an opponent has to dispute before the score auto-locks.

    Stored per tournament so it survives a restart and is consistent across
    worker processes; the module constants are defaults only.
    """
    base = match.tournament.dispute_window_minutes or DEFAULT_DISPUTE_WINDOW_MINUTES
    if _is_critical_stage_match(match):
        return min(base, CRITICAL_STAGE_DISPUTE_WINDOW_MINUTES)
    return base


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

    # An auto-lock passes None; that must not erase a recorded confirmer.
    if confirmed_by_user is not None:
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
        advance_loser_to_third_place(match)
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
        # Only the comparison matters here, so stop counting at the threshold.
        # A short count is exact, which keeps the error message below honest.
        available_slots = count_available_slots(tournament, limit=required_matches or None)
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
    team_ids_for_labels = {team.pk for team in teams}
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
                    if opponent:
                        team_ids_for_labels.add(opponent.pk)
                    schedule_by_team_day[(team.pk, match_day)].append({
                        "match_number": related_match.match_number,
                        "time_label": (
                            f"{local_start.strftime('%H:%M')} - {local_end.strftime('%H:%M')}"
                            if local_end else local_start.strftime("%H:%M")
                        ),
                        "court_name": related_match.court.name if related_match.court else "TBD court",
                        "opponent_name": opponent.pk if opponent else None,
                    })

    team_name_map = _team_display_map(match.tournament, team_ids_for_labels)

    for entries in schedule_by_team_day.values():
        for entry in entries:
            opponent_id = entry.pop("opponent_name", None)
            entry["opponent_name"] = team_name_map.get(opponent_id, "TBD")

    return [
        {
            "slot": slot,
            "team_schedules": [
                {
                    "team_name": team_name_map.get(team.pk, team.name),
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


def _claim_participant_slot(tournament):
    """Lock the tournament row and re-check capacity. True if a slot is free.

    Must be called inside transaction.atomic(), and the caller must create the
    participation before that transaction commits: the row lock is what stops a
    second request slipping in between the check and the insert.

    Registration was a plain check-then-act -- count the active participants,
    then create one -- with nothing in between. On SQLite that was masked by
    accident: SQLite locks the whole table, so the losing request died with
    "database table is locked" rather than overfilling the tournament.
    PostgreSQL commits both, so a one-slot tournament ends up with two
    participants and no error anywhere. Both were reproduced before this was
    written.

    select_for_update is a no-op on SQLite, so there the old table-level
    locking still decides the outcome.
    """
    locked = Tournament.objects.select_for_update().get(pk=tournament.pk)
    if not locked.expected_teams_count:
        return True
    return active_participant_count(locked) < locked.expected_teams_count


def _roster_conflicts_for_joining(user, team):
    """Return reasons `user` cannot join `team` right now, as display strings.

    A team competes in several tournaments, so joining it can break the roster
    cap or the one-team-per-tournament rule in any of them. join_team_view
    enforces both for the tournament being joined; every other path that adds a
    member must apply the same rules across all of the team's live tournaments.
    """
    live_statuses = (
        "setup", "registration_open", "ready", "scheduled", "active", "paused",
    )
    conflicts = []
    current_size = team.memberships.count()
    participations = TeamTournamentParticipation.objects.filter(
        team=team, status__in=["pending", "active", "waitlisted"]
    ).select_related("tournament")

    for participation in participations:
        tournament = participation.tournament
        if tournament.status not in live_statuses:
            continue
        capacity = max(1, tournament.players_per_team or 1)
        if current_size >= capacity:
            conflicts.append(
                f"'{team.name}' already has {current_size} of {capacity} "
                f"player(s) for '{tournament.name}'."
            )
            continue
        if _is_user_enrolled_in_tournament(user, tournament):
            conflicts.append(
                f"{user.username} is already registered for '{tournament.name}' "
                f"with another team."
            )
    return conflicts


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
        strength_errors = password_strength_errors(password)
        if strength_errors:
            messages.warning(
                request,
                f"Team '{team_name}' skipped — the password for '{username}' is not "
                f"strong enough: {strength_errors[0]}",
            )
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
