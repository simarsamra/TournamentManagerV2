import json
from datetime import date, datetime, timedelta

from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


class Tournament(models.Model):
    FORMAT_CHOICES = [
        ("round_robin", "Round Robin"),
        ("double_round_robin", "Double Round Robin"),
        ("knockout", "Knockout"),
        ("double_elimination", "Double Elimination"),
        ("consolation", "Consolation"),
        ("hybrid", "Hybrid (Groups + Knockout)"),
    ]
    SPORT_CHOICES = [
        ("badminton", "Badminton"),
        ("tennis", "Tennis"),
        ("volleyball", "Volleyball"),
        ("basketball", "Basketball"),
        ("soccer", "Soccer"),
        ("cricket", "Cricket"),
        ("table_tennis", "Table Tennis"),
        ("other", "Other"),
    ]
    WITHDRAWAL_POLICY_CHOICES = [
        ("forfeit", "Forfeit remaining matches"),
        ("void", "Void remaining matches"),
    ]
    REGISTRATION_MODE_CHOICES = [
        ("team", "Register Teams"),
        ("individual", "Register Individuals"),
    ]

    name = models.CharField(max_length=200)
    sport_type = models.CharField(
        max_length=30, choices=SPORT_CHOICES, default="other",
        help_text="Type of sport for this tournament",
    )
    registration_mode = models.CharField(
        max_length=20,
        choices=REGISTRATION_MODE_CHOICES,
        default="team",
        help_text="Choose whether this tournament registers full teams or individual players.",
    )
    format = models.CharField(max_length=30, choices=FORMAT_CHOICES)
    players_per_team = models.IntegerField(
        default=1, help_text="Number of players required per team",
    )
    status = models.CharField(
        max_length=20,
        choices=[
            ("setup", "Setup"),
            ("registration_open", "Registration Open"),
            ("ready", "Ready for Scheduling"),
            ("scheduled", "Schedule Draft Ready"),
            ("active", "Active"),
            ("completed", "Completed"),
        ],
        default="setup",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    start_date = models.DateField(
        default=timezone.localdate,
        null=True,
        blank=True,
        help_text="Date from which automatic scheduling should begin",
    )
    end_date = models.DateField(
        null=True,
        blank=True,
        help_text="Expected end date (auto-calculated if left blank)",
    )
    expected_teams_count = models.PositiveIntegerField(
        default=0,
        blank=True,
        help_text="Expected number of teams before the tournament can start",
    )

    points_per_win = models.IntegerField(default=3)
    points_per_loss = models.IntegerField(default=0)
    points_per_draw = models.IntegerField(default=1)
    tiebreaker_order = models.TextField(
        default='["game_diff", "games_won", "head_to_head"]'
    )

    num_groups = models.IntegerField(default=2, help_text="For hybrid format")
    teams_per_group_advance = models.IntegerField(
        default=2, help_text="Teams advancing from each group"
    )

    withdrawal_policy = models.CharField(
        max_length=20, choices=WITHDRAWAL_POLICY_CHOICES, default="forfeit"
    )
    default_match_duration = models.IntegerField(
        default=30, help_text="Minutes per match"
    )
    matches_per_court_per_day = models.PositiveIntegerField(
        default=0,
        blank=True,
        help_text="Maximum matches per court per day for scheduling (0 = no limit, derived from time slots)",
    )
    champion = models.ForeignKey(
        "Team",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="championships",
        help_text="Team that won the tournament",
    )

    def get_tiebreaker_order(self):
        return json.loads(self.tiebreaker_order)

    @property
    def participant_label(self):
        """Return 'Player' when players_per_team == 1, otherwise 'Team'."""
        return "Player" if self.players_per_team == 1 else "Team"

    @property
    def participant_label_plural(self):
        """Return 'Players' when players_per_team == 1, otherwise 'Teams'."""
        return "Players" if self.players_per_team == 1 else "Teams"

    def __str__(self):
        return f"{self.name} ({self.get_format_display()})"


class Court(models.Model):
    tournament = models.ForeignKey(
        Tournament, on_delete=models.CASCADE, related_name="courts"
    )
    name = models.CharField(max_length=100)
    is_available = models.BooleanField(default=True)

    class Meta:
        unique_together = ["tournament", "name"]

    def __str__(self):
        return self.name


class TimeSlot(models.Model):
    tournament = models.ForeignKey(
        Tournament, on_delete=models.CASCADE, related_name="time_slots"
    )
    court = models.ForeignKey(
        "Court", on_delete=models.CASCADE, related_name="time_slots", null=True, blank=True
    )
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()

    class Meta:
        ordering = ["start_time"]

    def __str__(self):
        court_name = f" ({self.court.name})" if self.court else ""
        return f"{self.start_time.strftime('%Y-%m-%d %H:%M')} - {self.end_time.strftime('%H:%M')}{court_name}"


class CourtAvailability(models.Model):
    WEEKDAY_CHOICES = [
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday"),
    ]

    court = models.ForeignKey(
        Court, on_delete=models.CASCADE, related_name="availabilities"
    )
    weekday = models.IntegerField(choices=WEEKDAY_CHOICES)
    start_time = models.TimeField()
    end_time = models.TimeField()
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    additional_start_times = models.TextField(
        blank=True,
        default="",
        help_text="Comma-separated additional match start times after the first match, e.g. 13:00, 15:30.",
    )
    matches_per_court_per_day = models.PositiveIntegerField(
        default=1,
        help_text="How many matches should be scheduled on each court for each selected weekday.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["court__name", "weekday", "start_time"]

    def get_additional_start_times(self):
        times = []
        if not self.additional_start_times:
            return times

        for part in self.additional_start_times.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                times.append(datetime.strptime(part, "%H:%M").time())
            except ValueError:
                continue
        return sorted(set(times))

    @property
    def match_start_times(self):
        return [self.start_time] + self.get_additional_start_times()

    @property
    def match_intervals(self):
        duration = self.court.tournament.default_match_duration or 35
        intervals = []
        for start_time in self.match_start_times:
            start_dt = datetime.combine(date.today(), start_time)
            end_dt = start_dt + timedelta(minutes=duration)
            if end_dt.date() != start_dt.date():
                continue
            intervals.append((start_time, end_dt.time()))
        return intervals

    @property
    def match_time_summary(self):
        if not self.match_intervals:
            return f"{self.start_time.strftime('%H:%M')} - {self.end_time.strftime('%H:%M')}"
        return ", ".join(
            f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}" for start, end in self.match_intervals
        )

    def __str__(self):
        day = dict(self.WEEKDAY_CHOICES).get(self.weekday, self.weekday)
        return f"{self.court.name}: {day} {self.match_time_summary}"


class Team(models.Model):
    name = models.CharField(max_length=100, unique=True)
    department = models.CharField(max_length=120, blank=True, default="")
    sport_type = models.CharField(
        max_length=30,
        choices=Tournament.SPORT_CHOICES,
        default="other",
        blank=True,
    )
    is_internal = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Hidden shadow competitor for individual-mode tournaments; exclude from normal team UX.",
    )

    def __str__(self):
        return self.name


class TeamTournamentParticipation(models.Model):
    STATUS_CHOICES = [
        ("active", "Active"),
        ("withdrawn", "Withdrawn"),
    ]

    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="participations")
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name="team_participations")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    group = models.CharField(max_length=5, blank=True, default="")
    seed = models.IntegerField(default=0)
    availability_notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tournament_id", "seed", "id"]
        unique_together = [["team", "tournament"]]

    def __str__(self):
        return f"{self.team.name} @ {self.tournament.name}"


class TournamentIndividualRegistration(models.Model):
    """Source of truth for individual-mode enrollment; shadow_team bridges to the team-based match engine."""

    STATUS_CHOICES = TeamTournamentParticipation.STATUS_CHOICES

    tournament = models.ForeignKey(
        Tournament, on_delete=models.CASCADE, related_name="individual_registrations"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="individual_registrations"
    )
    display_name = models.CharField(max_length=100)
    shadow_team = models.ForeignKey(
        "Team",
        on_delete=models.CASCADE,
        related_name="individual_registration_shadows",
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    group = models.CharField(max_length=5, blank=True, default="")
    seed = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tournament_id", "seed", "display_name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tournament", "user"],
                name="uniq_tournamentindividualregistration_tournament_user",
            ),
            models.UniqueConstraint(
                fields=["tournament", "display_name"],
                name="uniq_tournamentindividualregistration_tournament_display_name",
            ),
            models.UniqueConstraint(
                fields=["tournament", "shadow_team"],
                name="uniq_tournamentindividualregistration_tournament_shadow_team",
            ),
        ]

    def __str__(self):
        return f"{self.display_name} @ {self.tournament.name}"


class TeamTournamentCourtPreference(models.Model):
    participation = models.ForeignKey(
        TeamTournamentParticipation,
        on_delete=models.CASCADE,
        related_name="court_preferences",
    )
    court = models.ForeignKey(Court, on_delete=models.CASCADE, related_name="participation_preferences")

    class Meta:
        ordering = ["participation_id", "court_id"]
        unique_together = [["participation", "court"]]

    def __str__(self):
        return f"{self.participation.team.name} prefers {self.court.name}"


class Player(models.Model):
    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="players"
    )
    name = models.CharField(max_length=200)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.name} ({self.team.name})"


class OrganizerProfile(models.Model):
    """Marks users who are approved to create and manage tournaments."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="organizer_profile")
    org_name = models.CharField(max_length=200, blank=True, default="")
    verified = models.BooleanField(default=False, help_text="Admin-approved organizer")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} (Organizer)"


class UserTeamAssignment(models.Model):
    """Tracks each user's active team. One per user."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="team_assignment")
    active_team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.SET_NULL, related_name="active_members")

    class Meta:
        verbose_name = "User Team Assignment"
        verbose_name_plural = "User Team Assignments"

    def __str__(self):
        return f"{self.user.username} -> {self.active_team.name if self.active_team else 'No Team'}"


class TeamMembership(models.Model):
    ROLE_CHOICES = [
        ("captain", "Captain"),
        ("member", "Member"),
    ]

    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="member")
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["role", "joined_at"]
        unique_together = [["team", "user"]]

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()}) — {self.team.name}"


class Match(models.Model):
    STATUS_CHOICES = [
        ("upcoming", "Upcoming"),
        ("in_progress", "In Progress"),
        ("pending_confirmation", "Pending Confirmation"),
        ("confirmed", "Done"),
        ("disputed", "Disputed"),
        ("cancelled", "Cancelled"),
        ("forfeited", "Forfeited"),
        ("bye", "Bye"),
    ]

    tournament = models.ForeignKey(
        Tournament, on_delete=models.CASCADE, related_name="matches"
    )
    match_number = models.IntegerField()
    team1 = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="home_matches", null=True, blank=True
    )
    team2 = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="away_matches", null=True, blank=True
    )
    court = models.ForeignKey(
        Court, on_delete=models.SET_NULL, null=True, blank=True, related_name="matches"
    )
    scheduled_time = models.DateTimeField(null=True, blank=True)
    scheduled_end_time = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=25, choices=STATUS_CHOICES, default="upcoming")

    score_team1 = models.IntegerField(null=True, blank=True)
    score_team2 = models.IntegerField(null=True, blank=True)
    score_submitted_at = models.DateTimeField(null=True, blank=True)
    dispute_deadline_at = models.DateTimeField(null=True, blank=True, db_index=True)
    score_locked_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="submitted_matches"
    )
    confirmed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="confirmed_matches"
    )
    disputed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="disputed_matches"
    )
    critical_dispute = models.BooleanField(default=False)
    dispute_resolution_notes = models.TextField(blank=True, default="")
    dispute_resolved_at = models.DateTimeField(null=True, blank=True)
    winner = models.ForeignKey(
        Team, on_delete=models.SET_NULL, null=True, blank=True, related_name="won_matches"
    )

    notes = models.TextField(blank=True, default="")

    round_number = models.IntegerField(default=1)
    bracket_position = models.IntegerField(default=0)
    bracket_type = models.CharField(max_length=20, default="winners")
    next_match = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="previous_matches"
    )
    group = models.CharField(max_length=5, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["match_number"]

    def __str__(self):
        t1 = self.team1.name if self.team1 else "TBD"
        t2 = self.team2.name if self.team2 else "TBD"
        return f"Match {self.match_number}: {t1} vs {t2}"

    def get_opponent(self, team):
        if self.team1 == team:
            return self.team2
        elif self.team2 == team:
            return self.team1
        return None


class RescheduleRequest(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("cancelled", "Cancelled"),
    ]

    match = models.ForeignKey(
        Match, on_delete=models.CASCADE, related_name="reschedule_requests"
    )
    requested_by = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="reschedule_requests_made"
    )
    new_time = models.DateTimeField()
    new_court = models.ForeignKey(
        Court, on_delete=models.SET_NULL, null=True, blank=True
    )
    reason = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    responded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Reschedule for {self.match} by {self.requested_by.username}"


class TeamRegistration(models.Model):
    """Registration of a team for a specific tournament."""
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("withdrawn", "Withdrawn"),
        ("disqualified", "Disqualified"),
    ]
    
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name="team_registrations")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="registrations")
    registered_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="registered_teams")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [["tournament", "team"]]
        ordering = ["tournament_id", "created_at"]

    def __str__(self):
        return f"{self.team.name} @ {self.tournament.name} ({self.status})"


class IndividualRegistration(models.Model):
    """Registration of an individual user for a specific tournament."""
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("withdrawn", "Withdrawn"),
        ("disqualified", "Disqualified"),
    ]
    
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name="individual_registrations_new")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="individual_registrations_new")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [["tournament", "user"]]
        ordering = ["tournament_id", "user__username"]

    def __str__(self):
        return f"{self.user.username} @ {self.tournament.name} ({self.status})"


class NoShowReport(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending Response"),
        ("resolved", "Resolved"),
        ("auto_forfeited", "Auto Forfeited"),
        ("cancelled", "Cancelled"),
    ]

    match = models.ForeignKey(
        Match, on_delete=models.CASCADE, related_name="no_show_reports"
    )
    reported_by = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="no_show_reports_made"
    )
    absent_team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="no_show_reports_against"
    )
    present_team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="no_show_reports_supported"
    )
    note = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    deadline_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"No-show report for {self.match} by {self.reported_by.username}"


class OpenSlot(models.Model):
    tournament = models.ForeignKey(
        Tournament, on_delete=models.CASCADE, related_name="open_slots"
    )
    court = models.ForeignKey(Court, on_delete=models.CASCADE)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    reason = models.CharField(max_length=200, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start_time"]

    def __str__(self):
        start = timezone.localtime(self.start_time)
        end = timezone.localtime(self.end_time)
        return f"{self.court.name} · {start.strftime('%a, %b %d, %Y %H:%M')} - {end.strftime('%H:%M')}"


class AuditLog(models.Model):
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True
    )
    action = models.CharField(max_length=100)
    details = models.TextField(default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    tournament = models.ForeignKey(
        Tournament, on_delete=models.CASCADE, null=True, blank=True, related_name="audit_logs"
    )

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"[{self.timestamp}] {self.user}: {self.action}"


class BackupRecord(models.Model):
    filename = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True
    )
    size_bytes = models.IntegerField(default=0)
    is_auto = models.BooleanField(default=False)
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.filename
