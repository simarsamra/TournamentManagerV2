"""The AIQuestion work queue (AI-3): claim, process, reap.

Model calls never run inside a web request (plan §1.2): the site only
creates `pending` rows, and `manage.py ai_worker` works through them here,
one at a time.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from core.models import AIQuestion, Tournament

from .access import may_ask, may_write_recap
from .client import OllamaTimeout, OllamaUnavailable

logger = logging.getLogger("core.ai")

# Shown to the asker; the details go to the log.
MSG_NO_ACCESS = "You no longer have access to that tournament."
MSG_SLOW = "The model took too long to answer. Please try again."
MSG_UNAVAILABLE = "The AI service isn't available right now. Please try again later."
MSG_STALE = "This question timed out before it was answered. Please try again."
MSG_ERROR = "Something went wrong answering this question."

# The fields a processor fills in; written back only if the job is still ours.
RESULT_FIELDS = (
    "route", "facts", "snapshot", "answer", "answer_verified", "unchecked_numbers",
    "model_name", "timings",
)


def claim_next():
    """Move the oldest pending job to `running` and return it, or None.

    A compare-and-set UPDATE, so two workers can never both claim one job,
    on SQLite and PostgreSQL alike.
    """
    for _ in range(5):
        job = AIQuestion.objects.filter(status="pending").order_by("created_at", "pk").first()
        if job is None:
            return None
        started_at = timezone.now()
        if AIQuestion.objects.filter(pk=job.pk, status="pending").update(
            status="running", started_at=started_at,
        ) == 1:
            job.status, job.started_at = "running", started_at
            return job
        # Another worker took it between the SELECT and the UPDATE; try the next.
    return None


def reap_stale(now=None):
    """Fail `running` jobs older than AI_JOB_STALE_SECONDS (a worker died or
    hung mid-job). Returns how many."""
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=settings.AI_JOB_STALE_SECONDS)
    count = AIQuestion.objects.filter(status="running", started_at__lt=cutoff).update(
        status="failed", error=MSG_STALE, finished_at=now,
    )
    if count:
        logger.warning("Reaped %d stale AI question(s)", count)
    return count


def _default_processor(job):
    if job.kind == "recap":
        from .recap import write_recap

        return write_recap(job)
    from .pipeline import answer_question

    return answer_question(job)


def _allowed(job):
    check = may_write_recap if job.kind == "recap" else may_ask
    return check(job.user, job.tournament)


def _finish(job, status, error=""):
    """Write the outcome, but only while the job is still `running`: if the
    reaper failed it meanwhile, its verdict stands."""
    job.status, job.error, job.finished_at = status, error, timezone.now()
    fields = {name: getattr(job, name) for name in RESULT_FIELDS}
    updated = AIQuestion.objects.filter(pk=job.pk, status="running").update(
        status=status, error=error, finished_at=job.finished_at, **fields,
    )
    if not updated:
        logger.warning("AI question #%s was reaped before it finished; result dropped", job.pk)
    return job


def process(job, processor=None):
    """Answer one claimed job and record the outcome. Never raises."""
    processor = processor or _default_processor
    try:
        # Access may have changed while the job waited in the queue.
        if not _allowed(job):
            return _finish(job, "failed", MSG_NO_ACCESS)
        processor(job)
    except OllamaTimeout:
        logger.warning("AI question #%s: model timed out", job.pk, exc_info=True)
        return _finish(job, "failed", MSG_SLOW)
    except OllamaUnavailable:
        logger.warning("AI question #%s: Ollama unavailable", job.pk, exc_info=True)
        return _finish(job, "failed", MSG_UNAVAILABLE)
    except Exception:
        logger.exception("AI question #%s failed", job.pk)
        return _finish(job, "failed", MSG_ERROR)
    return _finish(job, "done")


def purge_old(days=None, now=None, dry_run=False):
    """Delete questions older than AI_RETENTION_DAYS (D-4). Returns the count."""
    days = settings.AI_RETENTION_DAYS if days is None else days
    now = now or timezone.now()
    # Keep each tournament's published recap however old: it's still on show.
    from .recap import latest_recap

    shown = [
        recap.pk for recap in (
            latest_recap(t) for t in Tournament.objects.filter(ai_questions__kind="recap").distinct()
        ) if recap
    ]
    old = AIQuestion.objects.filter(created_at__lt=now - timedelta(days=days)).exclude(pk__in=shown)
    count = old.count()
    if not dry_run and count:
        old.delete()
    return count
