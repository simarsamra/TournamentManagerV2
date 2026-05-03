"""Custom context processors for the tournament manager."""


def notification_count(request):
    """Inject unread notification count into every template context."""
    if not request.user.is_authenticated:
        return {"unread_notification_count": 0}
    from .models import Notification
    count = Notification.objects.filter(user=request.user, is_read=False).count()
    return {"unread_notification_count": count}


def user_organizer_status(request):
    """Inject a safe 'user_is_organizer' and 'user_can_apply_organizer' flag."""
    if not request.user.is_authenticated:
        return {"user_is_organizer": False, "user_can_apply_organizer": False}
    from .models import OrganizerProfile
    is_org = request.user.is_staff or request.user.is_superuser
    can_apply = False
    if not is_org:
        try:
            is_org = request.user.organizer_profile.verified
        except OrganizerProfile.DoesNotExist:
            pass
        can_apply = not is_org
    return {"user_is_organizer": is_org, "user_can_apply_organizer": can_apply}
