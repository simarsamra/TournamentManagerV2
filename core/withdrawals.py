"""Withdrawal handling logic."""
from django.utils import timezone
from .models import Match, Team
from .standings import advance_winner
from .audit import log_action


def handle_withdrawal(request, team, tournament):
    """Process a team withdrawal."""
    team.status = "withdrawn"
    team.withdrawn_at = timezone.now()
    team.save(update_fields=["status", "withdrawn_at"])

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

    # Cancel pending reschedule requests
    from .models import RescheduleRequest
    RescheduleRequest.objects.filter(
        requested_by=team, status="pending"
    ).update(status="cancelled")

    log_action(
        request,
        "team_withdrawal",
        f"Team '{team.name}' withdrew. Policy: {policy}. "
        f"Affected matches: {future_matches.count()}",
        tournament=tournament,
    )


def models_q_team(team):
    """Return Q object matching team in either position."""
    from django.db.models import Q
    return Q(team1=team) | Q(team2=team)
