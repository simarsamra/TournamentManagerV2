from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from core.models import OrganizerProfile, UserTeamAssignment, TeamMembership


class Command(BaseCommand):
    help = "Backfill OrganizerProfile and UserTeamAssignment for existing users"

    def handle(self, *args, **options):
        # Create OrganizerProfile for existing organizers (is_staff=True)
        organizer_count = 0
        for user in User.objects.filter(is_staff=True):
            obj, created = OrganizerProfile.objects.get_or_create(
                user=user,
                defaults={'verified': True, 'org_name': ''}
            )
            if created:
                organizer_count += 1
                self.stdout.write(f"Created OrganizerProfile for {user.username}")

        # Create UserTeamAssignment for all users, setting active_team if user is captain
        team_assignment_count = 0
        for user in User.objects.all():
            # Find captain memberships (highest priority)
            captain_membership = user.memberships.filter(role="captain", team__is_internal=False).first()
            active_team = captain_membership.team if captain_membership else None

            obj, created = UserTeamAssignment.objects.get_or_create(
                user=user,
                defaults={'active_team': active_team}
            )
            if created:
                team_assignment_count += 1
                team_name = active_team.name if active_team else "No Team"
                self.stdout.write(f"Created UserTeamAssignment for {user.username} -> {team_name}")

        self.stdout.write(
            self.style.SUCCESS(
                f"\nBackfill complete!\n"
                f"OrganizerProfiles created: {organizer_count}\n"
                f"UserTeamAssignments created: {team_assignment_count}"
            )
        )
