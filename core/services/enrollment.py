"""Enrollment-domain helpers used by views and workflows."""

from core.models import TeamTournamentParticipation


def active_participant_count(tournament):
    """Return active participant count for the tournament's registration mode."""
    if tournament.registration_mode == "individual":
        return tournament.individual_registrations.filter(status="active").count()
    return TeamTournamentParticipation.objects.filter(
        tournament=tournament,
        status="active",
        team__is_internal=False,
    ).count()


def is_registration_capacity_reached(tournament):
    """Return True when expected participant/team capacity has been met or exceeded."""
    if not tournament.expected_teams_count:
        return False
    return active_participant_count(tournament) >= tournament.expected_teams_count
