from django import forms
from django.contrib.auth.models import User
from django.utils import timezone
from .models import Tournament, Court, Team, Match, RescheduleRequest, TimeSlot, Player, CourtAvailability, OpenSlot


class TournamentForm(forms.ModelForm):
    class Meta:
        model = Tournament
        fields = [
            "name", "sport_type", "format", "players_per_team",
            "start_date", "expected_teams_count",
            "points_per_win", "points_per_loss",
            "points_per_draw", "num_groups", "teams_per_group_advance",
            "withdrawal_policy", "default_match_duration",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "sport_type": forms.Select(attrs={"class": "form-select"}),
            "format": forms.Select(attrs={"class": "form-select"}),
            "players_per_team": forms.NumberInput(attrs={"class": "form-control", "min": "1"}),
            "start_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "expected_teams_count": forms.NumberInput(attrs={"class": "form-control", "min": "2"}),
            "points_per_win": forms.NumberInput(attrs={"class": "form-control"}),
            "points_per_loss": forms.NumberInput(attrs={"class": "form-control"}),
            "points_per_draw": forms.NumberInput(attrs={"class": "form-control"}),
            "num_groups": forms.NumberInput(attrs={"class": "form-control"}),
            "teams_per_group_advance": forms.NumberInput(attrs={"class": "form-control"}),
            "withdrawal_policy": forms.Select(attrs={"class": "form-select"}),
            "default_match_duration": forms.NumberInput(attrs={"class": "form-control", "min": "5"}),
        }


class CourtForm(forms.ModelForm):
    class Meta:
        model = Court
        fields = ["name", "is_available"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "is_available": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["is_available"].required = False
        self.fields["is_available"].initial = True


class TimeSlotForm(forms.Form):
    court = forms.ModelChoiceField(
        queryset=Court.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    date = forms.DateField(widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}))
    start_time = forms.TimeField(widget=forms.TimeInput(attrs={"class": "form-control", "type": "time"}))
    end_time = forms.TimeField(widget=forms.TimeInput(attrs={"class": "form-control", "type": "time"}))

    def __init__(self, *args, tournament=None, **kwargs):
        super().__init__(*args, **kwargs)
        if tournament:
            self.fields["court"].queryset = Court.objects.filter(tournament=tournament, is_available=True)


class CourtAvailabilityForm(forms.Form):
    courts = forms.ModelMultipleChoiceField(
        queryset=Court.objects.none(),
        widget=forms.CheckboxSelectMultiple(),
        error_messages={"required": "Select at least one court."},
        help_text="Apply this schedule to one or more courts at once.",
    )
    weekdays = forms.MultipleChoiceField(
        choices=CourtAvailability.WEEKDAY_CHOICES,
        widget=forms.CheckboxSelectMultiple(),
        error_messages={"required": "Select at least one weekday."},
        help_text="Choose every day that should reuse this time range.",
    )
    start_time = forms.TimeField(widget=forms.TimeInput(attrs={"class": "form-control", "type": "time"}))
    end_time = forms.TimeField(widget=forms.TimeInput(attrs={"class": "form-control", "type": "time"}))
    start_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    end_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    is_active = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def __init__(self, *args, tournament=None, **kwargs):
        super().__init__(*args, **kwargs)
        if tournament:
            self.fields["courts"].queryset = Court.objects.filter(tournament=tournament).order_by("name")

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_time")
        end = cleaned.get("end_time")
        start_date = cleaned.get("start_date")
        end_date = cleaned.get("end_date")
        if start and end and end <= start:
            raise forms.ValidationError("End time must be after start time.")
        if start_date and end_date and end_date < start_date:
            raise forms.ValidationError("End date cannot be before start date.")
        return cleaned


class AccountRegistrationForm(forms.Form):
    """Step 1: Create a user account (no team yet)."""
    full_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Full Name"}),
        help_text="Your real name, shown to your team.",
    )
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Username"}),
        help_text="Used to log in.",
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": "form-control", "placeholder": "Password"})
    )
    password_confirm = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput(attrs={"class": "form-control", "placeholder": "Confirm Password"}),
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password") != cleaned.get("password_confirm"):
            raise forms.ValidationError("Passwords do not match.")
        if User.objects.filter(username=cleaned.get("username", "").strip()).exists():
            raise forms.ValidationError("Username already taken.")
        return cleaned


class CreateTeamForm(forms.Form):
    """Step 2: Create a new team inside an open tournament."""
    team_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Team Name"}),
    )
    department = forms.CharField(
        max_length=120,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Department (optional)"}),
    )
    preferred_courts = forms.ModelMultipleChoiceField(
        queryset=Court.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple(),
        help_text="Select the courts your team prefers for scheduled matches.",
    )

    def __init__(self, *args, tournament=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tournament = tournament
        if tournament:
            courts = Court.objects.filter(tournament=tournament, is_available=True).order_by("name")
            self.fields["preferred_courts"].queryset = courts
            self.fields["preferred_courts"].required = courts.exists()
            if not courts.exists():
                self.fields["preferred_courts"].help_text = "No courts are currently available for preference selection."

    def clean(self):
        cleaned = super().clean()
        courts = cleaned.get("preferred_courts")
        if self.tournament and self.tournament.courts.filter(is_available=True).exists() and not courts:
            raise forms.ValidationError("Please select at least one preferred court.")
        return cleaned


# Keep for backwards compat with bulk-add (organiser tool) — still used in _create_teams_from_data
class TeamRegistrationForm(forms.Form):
    team_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Team Name"})
    )
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Username"})
    )
    department = forms.CharField(
        max_length=120,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Department (optional)"})
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": "form-control", "placeholder": "Password"})
    )
    password_confirm = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": "form-control", "placeholder": "Confirm Password"})
    )


class ScoreSubmitForm(forms.Form):
    score_team1 = forms.IntegerField(
        min_value=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "placeholder": "Score"})
    )
    score_team2 = forms.IntegerField(
        min_value=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "placeholder": "Score"})
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "Notes (optional)"})
    )


class RescheduleForm(forms.Form):
    open_slot = forms.ModelChoiceField(
        queryset=OpenSlot.objects.none(),
        required=False,
        empty_label=None,
        widget=forms.RadioSelect(),
    )
    new_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    new_time = forms.TimeField(
        required=False,
        widget=forms.TimeInput(attrs={"class": "form-control", "type": "time"}),
    )
    new_court = forms.ModelChoiceField(
        queryset=Court.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    reason = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2})
    )

    def __init__(self, *args, tournament=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["open_slot"].label_from_instance = lambda slot: (
            f"{timezone.localtime(slot.start_time).strftime('%a, %b %d, %Y · %H:%M')} - "
            f"{timezone.localtime(slot.end_time).strftime('%H:%M')} · {slot.court.name}"
        )
        if tournament:
            self.fields["open_slot"].queryset = OpenSlot.objects.filter(
                tournament=tournament,
                end_time__gt=timezone.now(),
            ).select_related("court").order_by("start_time")
            self.fields["new_court"].queryset = Court.objects.filter(
                tournament=tournament, is_available=True
            )

    def clean(self):
        cleaned = super().clean()
        open_slot = cleaned.get("open_slot")
        new_date = cleaned.get("new_date")
        new_time = cleaned.get("new_time")
        if not open_slot and (not new_date or not new_time):
            raise forms.ValidationError("Choose an open slot or enter a new date and time.")
        return cleaned


class TeamPreferencesForm(forms.Form):
    preferred_courts = forms.ModelMultipleChoiceField(
        queryset=Court.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple(),
    )
    availability_notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3})
    )

    def __init__(self, *args, tournament=None, **kwargs):
        super().__init__(*args, **kwargs)
        if tournament:
            self.fields["preferred_courts"].queryset = Court.objects.filter(
                tournament=tournament
            )


class BulkTeamForm(forms.Form):
    """Add multiple teams at once via text."""
    teams_text = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            "class": "form-control",
            "rows": 10,
            "placeholder": "One team per line: team_name,username,password,player1;player2;player3"
        }),
        help_text="Format: team_name,username,password,player1;player2;... (one per line)"
    )


class BulkTeamFileForm(forms.Form):
    """Add multiple teams via CSV or text file upload."""
    file = forms.FileField(
        required=False,
        widget=forms.FileInput(attrs={"class": "form-control", "accept": ".csv,.txt"}),
        help_text="CSV/TXT file: team_name,username,password,player1;player2;... (one per line)"
    )


class TeamMemberInviteForm(forms.Form):
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Username"}),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": "form-control", "placeholder": "Password"}),
    )
    password_confirm = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput(attrs={"class": "form-control", "placeholder": "Confirm Password"}),
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password") != cleaned.get("password_confirm"):
            raise forms.ValidationError("Passwords do not match.")
        username = cleaned.get("username", "").strip()
        if username and User.objects.filter(username=username).exists():
            raise forms.ValidationError("Username already taken.")
        return cleaned
