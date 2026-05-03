"""Withdrawal handling logic."""
from django.utils import timezone
from .models import Match, Team, TeamTournamentParticipation
from .standings import advance_winner
from .audit import log_action


def _promote_from_waitlist(tournament, request=None):
    """Promote the oldest waitlisted participation to pending/active (4.7)."""
    first_waitlisted = (
        TeamTournamentParticipation.objects.filter(
            tournament=tournament, status="waitlisted"
        )
        .order_by("created_at")
        .first()
    )
    if not first_waitlisted:
        return None

    required = max(1, tournament.players_per_team or 1)
    first_waitlisted.status = "active" if required == 1 else "pending"
    first_waitlisted.save(update_fields=["status"])

    # Notify team captain
    from django.contrib.auth.models import User
    captain_user = User.objects.filter(
        memberships__team=first_waitlisted.team, memberships__role="captain"
    ).first()
    if captain_user:
        from .views import _notify
        _notify(
            captain_user,
            "registration_promoted_from_waitlist",
            f"A spot opened up! Your team '{first_waitlisted.team.name}' has been promoted from the waitlist for '{tournament.name}'.",
            link=f"/tournaments/{tournament.pk}/",
            tournament=tournament,
        )

    log_action(
        request,
        "waitlist_promoted",
        f"Team '{first_waitlisted.team.name}' promoted from waitlist for '{tournament.name}'",
        tournament=tournament,
    )
    return first_waitlisted


def handle_withdrawal(request, team, tournament):
    """Process a team withdrawal."""
    participation = TeamTournamentParticipation.objects.filter(
        team=team, tournament=tournament
    ).first()
    if participation:
        participation.status = "withdrawn"
        participation.withdrawn_at = timezone.now()
        participation.save(update_fields=["status", "withdrawn_at"])

    # Before publication (active), treat withdrawal as deregistration: do not apply forfeits.
    pre_active_statuses = {"setup", "registration_open", "ready", "scheduled"}
    if tournament.status in pre_active_statuses:
        draft_matches = Match.objects.filter(
            tournament=tournament,
            status__in=["upcoming", "in_progress", "pending_confirmation", "disputed"],
        ).filter(models_q_team(team))

        for match in draft_matches:
            match.status = "cancelled"
            match.winner = None
            match.notes = f"{team.name} withdrew before tournament activation"
            match.save(update_fields=["status", "winner", "notes"])

        from .models import RescheduleRequest
        from django.contrib.auth.models import User
        team_member_users = User.objects.filter(memberships__team=team)
        RescheduleRequest.objects.filter(
            requested_by__in=team_member_users,
            match__tournament=tournament,
            status="pending",
        ).update(status="cancelled")

        log_action(
            request,
            "team_withdrawal_pre_activation",
            f"Team '{team.name}' withdrew before activation. Cancelled draft matches: {draft_matches.count()}",
            tournament=tournament,
        )
        # Promote the first waitlisted team when a spot frees up (4.7)
        _promote_from_waitlist(tournament, request)
        return

    policy = tournament.withdrawal_policy

    # Handle future matches
    future_matches = Match.objects.filter(
        tournament=tournament,
        status__in=["upcoming", "in_progress"],
    ).filter(
        models_q_team(team)
    )

    for match in future_matches:
        opponent = match.get_opponent(team)
        if policy == "forfeit":
            match.status = "forfeited"
            match.winner = opponent
            match.notes = f"{team.name} withdrew - match forfeited"
            match.save(update_fields=["status", "winner", "notes"])

            # Advance opponent in knockout brackets
            if tournament.format in ("knockout", "double_elimination", "consolation", "hybrid"):
                advance_winner(match)
            if tournament.format == "consolation":
                from .scheduling import generate_consolation_if_ready

                generate_consolation_if_ready(tournament)
        elif policy == "void":
            match.status = "cancelled"
            match.notes = f"{team.name} withdrew - match voided"
            match.save(update_fields=["status", "notes"])

        # Create open slot
        if match.scheduled_time and match.court:
            from .models import OpenSlot
            OpenSlot.objects.create(
                tournament=tournament,
                court=match.court,
                start_time=match.scheduled_time,
                end_time=match.scheduled_end_time or match.scheduled_time,
                reason=f"Withdrawal of {team.name}",
            )

    # Cancel pending reschedule requests from any team member
    from .models import RescheduleRequest
    from django.contrib.auth.models import User
    team_member_users = User.objects.filter(memberships__team=team)
    RescheduleRequest.objects.filter(
        requested_by__in=team_member_users,
        match__tournament=tournament,
        status="pending",
    ).update(status="cancelled")

    log_action(
        request,
        "team_withdrawal",
        f"Team '{team.name}' withdrew. Policy: {policy}. "
        f"Affected matches: {future_matches.count()}",
        tournament=tournament,
    )
    # Promote the first waitlisted team when a spot frees up (4.7)
    if tournament.status in ("registration_open", "ready", "scheduled"):
        _promote_from_waitlist(tournament, request)


def models_q_team(team):
    """Return Q object matching team in either position."""
    from django.db.models import Q
    return Q(team1=team) | Q(team2=team)
