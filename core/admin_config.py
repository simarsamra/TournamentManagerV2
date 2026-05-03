from django.contrib import admin
from .models import (
    Tournament, Court, TimeSlot, Team, Match,
    RescheduleRequest, OpenSlot, AuditLog, BackupRecord, CourtAvailability,
    TeamTournamentParticipation, TeamTournamentCourtPreference,
    TournamentIndividualRegistration, OrganizerProfile, UserTeamAssignment,
    TeamRegistration, IndividualRegistration, TeamMembership,
)


@admin.register(Tournament)
class TournamentAdmin(admin.ModelAdmin):
    list_display = ["name", "format", "status", "start_date", "expected_teams_count", "created_at"]


@admin.register(Court)
class CourtAdmin(admin.ModelAdmin):
    list_display = ["name", "tournament", "is_available"]


@admin.register(CourtAvailability)
class CourtAvailabilityAdmin(admin.ModelAdmin):
    list_display = [
        "court",
        "weekday",
        "matches_per_court_per_day",
        "additional_start_times",
        "start_time",
        "end_time",
        "start_date",
        "end_date",
        "is_active",
    ]
    list_filter = ["weekday", "is_active", "court__tournament"]


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ["name", "department", "is_internal", "sport_type"]
    list_filter = ["is_internal", "sport_type"]


@admin.register(TeamTournamentParticipation)
class TeamTournamentParticipationAdmin(admin.ModelAdmin):
    list_display = ["team", "tournament", "status", "group", "seed"]
    list_filter = ["status", "tournament", "team__is_internal"]


@admin.register(TournamentIndividualRegistration)
class TournamentIndividualRegistrationAdmin(admin.ModelAdmin):
    list_display = [
        "display_name",
        "user",
        "tournament",
        "status",
        "shadow_team",
        "group",
        "seed",
        "created_at",
    ]
    list_filter = ["status", "tournament", "tournament__registration_mode"]
    search_fields = ["display_name", "user__username"]
    raw_id_fields = ["user", "shadow_team"]


@admin.register(TeamTournamentCourtPreference)
class TeamTournamentCourtPreferenceAdmin(admin.ModelAdmin):
    list_display = ["participation", "court"]
    list_filter = ["court__tournament"]


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    list_display = ["match_number", "team1", "team2", "status", "scheduled_time"]
    list_filter = ["status", "tournament"]


@admin.register(RescheduleRequest)
class RescheduleRequestAdmin(admin.ModelAdmin):
    list_display = ["match", "requested_by", "status", "new_time"]


@admin.register(OpenSlot)
class OpenSlotAdmin(admin.ModelAdmin):
    list_display = ["court", "start_time", "end_time", "reason"]


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ["user", "action", "timestamp"]
    list_filter = ["action"]


@admin.register(BackupRecord)
class BackupRecordAdmin(admin.ModelAdmin):
    list_display = ["filename", "created_at", "created_by", "size_bytes"]


@admin.register(OrganizerProfile)
class OrganizerProfileAdmin(admin.ModelAdmin):
    list_display = ["user", "org_name", "verified", "created_at"]
    list_filter = ["verified", "created_at"]
    search_fields = ["user__username", "org_name"]


@admin.register(UserTeamAssignment)
class UserTeamAssignmentAdmin(admin.ModelAdmin):
    list_display = ["user", "active_team"]
    search_fields = ["user__username"]
    raw_id_fields = ["user", "active_team"]


@admin.register(TeamMembership)
class TeamMembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "team", "role", "joined_at"]
    list_filter = ["role", "team", "joined_at"]
    search_fields = ["user__username", "team__name"]


@admin.register(TeamRegistration)
class TeamRegistrationAdmin(admin.ModelAdmin):
    list_display = ["tournament", "team", "status", "registered_by", "created_at"]
    list_filter = ["status", "tournament", "created_at"]
    search_fields = ["team__name", "tournament__name"]
    raw_id_fields = ["tournament", "team", "registered_by"]


@admin.register(IndividualRegistration)
class IndividualRegistrationAdmin(admin.ModelAdmin):
    list_display = ["tournament", "user", "status", "created_at"]
    list_filter = ["status", "tournament", "created_at"]
    search_fields = ["user__username", "tournament__name"]
    raw_id_fields = ["tournament", "user"]
