"""Check the AI analytics configuration against the running Ollama (AI-2).

Run by hand on the server after installing Ollama and pulling the model:

    python manage.py ai_doctor

Exits non-zero if anything the feature needs is broken.
"""
import json
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.ai import client

PROBE_SCHEMA = {
    "type": "object",
    "properties": {"colour": {"type": "string", "enum": ["blue", "green", "red"]}},
    "required": ["colour"],
}
PROBE_MESSAGES = [
    {"role": "system", "content": "Answer with JSON only."},
    {"role": "user", "content": "What colour is a clear daytime sky?"},
]


def _model_installed(model, installed):
    # "qwen3.5" in settings matches "qwen3.5:latest" in /api/tags.
    return model in installed or (":" not in model and f"{model}:latest" in installed)


class Command(BaseCommand):
    help = "Check that Ollama is reachable and the configured model answers structured questions."

    def add_arguments(self, parser):
        parser.add_argument("--model", help="Check this model instead of DJANGO_OLLAMA_MODEL.")

    def handle(self, *args, **options):
        model = options.get("model") or settings.OLLAMA_MODEL
        failures = []

        def ok(msg):
            self.stdout.write(self.style.SUCCESS("  OK    ") + msg)

        def warn(msg):
            self.stdout.write(self.style.WARNING("  WARN  ") + msg)

        def fail(msg):
            failures.append(msg)
            self.stdout.write(self.style.ERROR("  FAIL  ") + msg)

        self.stdout.write(f"Ollama at {settings.OLLAMA_URL}, model {model or '(none)'}")

        # 1. Configuration
        if settings.AI_ANALYTICS_ENABLED:
            ok("AI analytics is enabled")
        else:
            warn("AI analytics is disabled (DJANGO_AI_ANALYTICS_ENABLED); checking Ollama anyway")
        if not model:
            fail("No model configured: set DJANGO_OLLAMA_MODEL")
            raise CommandError("ai_doctor found problems.")

        # 2. Reachable
        try:
            ok(f"Ollama {client.version()} is reachable")
        except client.OllamaError as exc:
            fail(f"{exc}. Is Ollama running? (systemctl status ollama)")
            raise CommandError("ai_doctor found problems.")

        # 3. Model pulled
        try:
            installed = client.installed_models()
        except client.OllamaError as exc:
            fail(str(exc))
            installed = set()
        if _model_installed(model, installed):
            ok(f"Model {model} is pulled")
        else:
            fail(f"Model {model} is not pulled: run `ollama pull {model}`"
                 + (f" (installed: {', '.join(sorted(installed))})" if installed else ""))
            raise CommandError("ai_doctor found problems.")

        # 4-6. A structured call: JSON schema honoured, `think` accepted, latency
        think = settings.OLLAMA_THINK
        started = time.monotonic()
        try:
            result = client.chat(PROBE_MESSAGES, schema=PROBE_SCHEMA, model=model, num_predict=32)
        except client.OllamaBadResponse as exc:
            if think is not None and "think" in str(exc).lower():
                fail(f"{model} rejected the `think` flag: set DJANGO_OLLAMA_THINK= (empty) "
                     f"for this model. ({exc})")
            else:
                fail(f"Structured chat call failed: {exc}")
        except client.OllamaError as exc:
            fail(f"Structured chat call failed: {exc}")
        else:
            wall_ms = round((time.monotonic() - started) * 1000)
            try:
                answer = json.loads(result.content)
            except ValueError:
                answer = None
            if isinstance(answer, dict) and answer.get("colour") in PROBE_SCHEMA["properties"]["colour"]["enum"]:
                ok(f"Structured output honoured the JSON schema ({answer['colour']!r})")
            else:
                fail(f"Reply didn't match the JSON schema: {result.content[:200]!r}. "
                     "Ollama 0.5 or newer is needed for structured outputs.")
            if think is not None:
                ok(f"`think: {str(think).lower()}` accepted")
            load = f", of which loading the model {result.load_ms} ms" if result.load_ms else ""
            ok(f"Round trip {wall_ms} ms{load}; "
               f"{result.prompt_tokens} prompt / {result.output_tokens} output tokens")
            if result.load_ms and result.load_ms > 1000:
                warn("The model was loaded for this call. Later calls within "
                     f"OLLAMA_KEEP_ALIVE ({settings.OLLAMA_KEEP_ALIVE}) skip that.")

        if failures:
            raise CommandError(f"ai_doctor found {len(failures)} problem(s).")
        self.stdout.write(self.style.SUCCESS("All checks passed."))
