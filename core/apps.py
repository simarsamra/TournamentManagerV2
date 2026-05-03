from django.apps import AppConfig
from django.db.models.signals import post_save
from django.dispatch import receiver


class CoreConfig(AppConfig):
    name = "core"

    def ready(self):
        """Initialize signal handlers when Django app is ready."""
        from django.contrib.auth.models import User
        from .models import OrganizerProfile, UserTeamAssignment

        @receiver(post_save, sender=User)
        def create_organizer_profile(sender, instance, created, **kwargs):
            """Auto-create OrganizerProfile for new users, respecting is_staff."""
            if created:
                OrganizerProfile.objects.get_or_create(
                    user=instance,
                    defaults={"verified": instance.is_staff}
                )
            elif not created:
                # Update verified status if is_staff changed
                org_profile = OrganizerProfile.objects.get(user=instance)
                if org_profile.verified != instance.is_staff:
                    org_profile.verified = instance.is_staff
                    org_profile.save(update_fields=["verified"])

        @receiver(post_save, sender=User)
        def create_user_team_assignment(sender, instance, created, **kwargs):
            """Auto-create UserTeamAssignment for new users."""
            if created:
                UserTeamAssignment.objects.get_or_create(user=instance)
