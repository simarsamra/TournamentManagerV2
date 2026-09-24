"""Answer queued AI analytics questions (AI-3). Run as a service:

    python manage.py ai_worker

One question at a time, which matches Ollama's default OLLAMA_NUM_PARALLEL=1.
SIGTERM / SIGINT stop it after the current question.
"""
import signal
import threading

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, connection

from core.ai import jobs


class Command(BaseCommand):
    help = "Process pending AI analytics questions from the queue."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true",
                            help="Process at most one question, then exit.")
        parser.add_argument("--idle-sleep", type=float, default=1.0,
                            help="Seconds to wait when the queue is empty (default 1).")

    def handle(self, *args, **options):
        if not settings.AI_ANALYTICS_ENABLED:
            raise CommandError("AI analytics is disabled; set DJANGO_AI_ANALYTICS_ENABLED=true.")
        stop = threading.Event()
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())

        self.stdout.write(f"ai_worker: model {settings.OLLAMA_MODEL} at {settings.OLLAMA_URL}")
        while not stop.is_set():
            # A long-running process must not hold connections past CONN_MAX_AGE.
            # Never inside a transaction (only happens under TestCase): Django
            # would see the non-autocommit connection as unusable and close it,
            # as its own test client avoids doing for requests.
            if not connection.in_atomic_block:
                close_old_connections()
            jobs.reap_stale()
            job = jobs.claim_next()
            if job is None:
                if options["once"]:
                    break
                stop.wait(options["idle_sleep"])
                continue
            jobs.process(job)
            self.stdout.write(f"ai_worker: question #{job.pk} {job.status}"
                              + (f" ({job.error})" if job.error else ""))
            if options["once"]:
                break
        self.stdout.write("ai_worker: stopped")
