"""Answering one question: route, compute, answer (AI_ANALYTICS_PLAN.md §1.1).

1. ROUTE   (AI-5) the model picks the card and teams, schema-constrained,
   seeing the conversation's earlier questions for follow-ups;
2. COMPUTE (AI-4) the analytics code builds the facts for that card, and
   the whole-tournament snapshot (snapshot.py) when it fits;
3. ANSWER  with the snapshot, the model answers in conversation
   (conversation.py) whether or not a card matched; its numbers are checked
   and any that aren't in the data are flagged. Without a snapshot it falls
   back to the AI-7 explanation: up to three sentences about the card, only
   shown if every number is in the facts.
"""
import logging

from django.conf import settings

from . import conversation
from .client import OllamaError
from .explain import explain, ungrounded_numbers
from .facts import build_facts
from .router import route_question
from .snapshot import build_snapshot

logger = logging.getLogger("core.ai")


def answer_question(job):
    """Fill in job.route / facts / snapshot / answer / answer_verified /
    unchecked_numbers / model_name / timings for a claimed job.

    Routing errors propagate (jobs.process turns them into user-safe
    messages). An answer error doesn't when a card matched: the card is
    already a correct answer, so the job finishes without text.
    """
    turns = conversation.history(job) if job.parent_id else []
    snapshot = build_snapshot(job.tournament, job.user) if settings.AI_CONVERSATION_ENABLED else None

    routed = route_question(job.tournament, job.question, earlier=[q for q, _ in turns])
    job.route = routed.as_json()
    job.model_name = routed.chat.model
    job.timings = {"route": routed.chat.timings()}
    if routed.route.intent != "unknown":
        job.facts = build_facts(job.tournament, job.user, routed.route)

    if snapshot is not None:
        return _converse(job, snapshot, turns, card_matched=routed.route.intent != "unknown")
    if routed.route.intent == "unknown" or not settings.AI_EXPLANATIONS_ENABLED:
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


def _converse(job, snapshot, turns, card_matched):
    job.snapshot = snapshot
    try:
        text, result = conversation.reply(job.question, snapshot, turns)
    except OllamaError:
        if not card_matched:
            raise
        logger.warning("AI question #%s: answer failed; showing the card only", job.pk, exc_info=True)
        return
    job.timings["answer"] = result.timings()
    job.model_name = result.model
    job.answer = text
    # The question and earlier questions may name numbers ("last 10 games").
    asked = " ".join([job.question, *(q for q, _ in turns)])
    job.unchecked_numbers = ungrounded_numbers(text, [snapshot, job.facts], asked)
    job.answer_verified = bool(text) and not job.unchecked_numbers
    if job.unchecked_numbers:
        logger.info("AI question #%s: numbers not in the snapshot: %s", job.pk, job.unchecked_numbers)
