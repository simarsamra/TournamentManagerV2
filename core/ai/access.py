"""Who may ask the AI about a tournament (AI-3; used again by AI-6)."""
from django.conf import settings

from core import analytics


def may_ask(user, tournament):
    """A1's analytics rule, narrowed by AI_ANALYTICS_AUDIENCE (D-3):
    "managers" = the tournament's managers only; "all" = anyone who may
    open the tournament's analytics."""
    allowed, can_manage = analytics.can_view_analytics(user, tournament)
    if not allowed:
        return False
    if settings.AI_ANALYTICS_AUDIENCE == "managers":
        return can_manage
    return True
