from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"

    def ready(self):
        """Register signal handlers when the app registry is populated."""
        from . import checks  # noqa: F401 -- registers the AI analytics checks
        from .signals import connect_user_signals

        connect_user_signals()
