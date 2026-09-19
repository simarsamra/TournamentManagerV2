"""Audit logging utility."""
from django.conf import settings

from .models import AuditLog


def _client_ip(request):
    """Return the client IP, trusting X-Forwarded-For only behind known proxies.

    The header is attacker-controlled: without a trusted-proxy count, anyone can
    set the IP recorded against their own actions in the audit log.
    """
    if request is None:
        return None

    remote_addr = request.META.get("REMOTE_ADDR")
    proxy_count = getattr(settings, "TRUSTED_PROXY_COUNT", 0)
    if proxy_count <= 0:
        return remote_addr

    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    parts = [part.strip() for part in forwarded.split(",") if part.strip()]
    if len(parts) < proxy_count:
        # Fewer hops than expected — the chain is not what we configured, so
        # fall back rather than trust a client-supplied value.
        return remote_addr
    # Our own proxies append to the right; step back past them to the last
    # value a client could not have forged.
    return parts[-proxy_count]


def log_action(request, action, details="", tournament=None):
    AuditLog.objects.create(
        user=request.user if request and request.user.is_authenticated else None,
        action=action,
        details=details,
        ip_address=_client_ip(request),
        tournament=tournament,
    )
