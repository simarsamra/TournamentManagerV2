"""Audit logging utility."""
from .models import AuditLog


def log_action(request, action, details="", tournament=None):
    ip = None
    if request:
        ip = request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR"))
        if ip and "," in ip:
            ip = ip.split(",")[0].strip()
    AuditLog.objects.create(
        user=request.user if request and request.user.is_authenticated else None,
        action=action,
        details=details,
        ip_address=ip,
        tournament=tournament,
    )
