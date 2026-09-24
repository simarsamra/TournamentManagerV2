"""System checks for the optional AI analytics settings (AI-2)."""
from urllib.parse import urlparse

from django.conf import settings
from django.core.checks import Error, Warning, register

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@register()
def check_ai_analytics_settings(app_configs=None, **kwargs):
    if not settings.AI_ANALYTICS_ENABLED:
        return []
    problems = []
    if not settings.OLLAMA_MODEL:
        problems.append(Error(
            "AI analytics is enabled but DJANGO_OLLAMA_MODEL is empty.",
            hint="Set it to a pulled model, e.g. qwen3.5:9b, and run manage.py ai_doctor.",
            id="core.E101",
        ))
    parsed = urlparse(settings.OLLAMA_URL)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        problems.append(Error(
            f"DJANGO_OLLAMA_URL {settings.OLLAMA_URL!r} is not an http(s) URL.",
            id="core.E102",
        ))
    elif parsed.hostname not in LOOPBACK_HOSTS:
        problems.append(Warning(
            f"DJANGO_OLLAMA_URL points at {parsed.hostname}, not this machine.",
            hint="Ollama has no authentication. Questions and tournament data will "
                 "travel to that host; make sure it's one you control on a private network.",
            id="core.W101",
        ))
    return problems
