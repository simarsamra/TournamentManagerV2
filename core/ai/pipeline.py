"""Answering one question: route, compute, explain (AI_ANALYTICS_PLAN.md §1.1).

1. ROUTE   (AI-5) the model picks the card and teams, schema-constrained;
2. COMPUTE (AI-4) the analytics code builds the facts for that route;
3. EXPLAIN (AI-7) the model writes up to three sentences from the facts, and
   the text is only shown if every number in it is in the facts.
"""
import logging

from django.conf import settings

from .client import OllamaError
from .explain import explain, ungrounded_numbers
from .facts import build_facts
from .router import route_question

logger = logging.getLogger("core.ai")


def answer_question(job):
    """Fill in job.route / facts / answer / answer_verified / model_name /
    timings for a claimed job.

    Routing errors propagate (jobs.process turns them into user-safe
    messages). An explanation error doesn't: the routed card is already a
    correct answer, so the job finishes without text.
    """
    routed = route_question(job.tournament, job.question)
    job.route = routed.as_json()
    job.model_name = routed.chat.model
    job.timings = {"route": routed.chat.timings()}
    if routed.route.intent == "unknown":
        return
    job.facts = build_facts(job.tournament, job.user, routed.route)
    if not settings.AI_EXPLANATIONS_ENABLED:
        return
    try:
        text, result = explain(job.question, job.facts)
    except OllamaError:
        logger.warning("AI question #%s: explanation failed; showing the card only", job.pk, exc_info=True)
        return
    job.timings["explain"] = result.timings()
    job.answer = text
    unsupported = ungrounded_numbers(text, job.facts, job.question)
    job.answer_verified = bool(text) and not unsupported
    if unsupported:
        logger.info("AI question #%s: explanation hidden, numbers not in facts: %s", job.pk, unsupported)
