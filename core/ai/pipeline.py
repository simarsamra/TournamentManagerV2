"""Answering one question: route, compute, explain (AI_ANALYTICS_PLAN.md §1.1).

1. ROUTE   (AI-5) the model picks the card and teams, schema-constrained;
2. COMPUTE (AI-4) the analytics code builds the facts for that route;
3. EXPLAIN (AI-7) not yet: a finished job carries the route and facts, and
   the page shows the routed card, which is a correct answer on its own.
"""
from .facts import build_facts
from .router import route_question


def answer_question(job):
    """Fill in job.route / facts / model_name / timings for a claimed job.

    Raises core.ai.client.OllamaError subclasses as they come; jobs.process
    turns them into user-safe messages.
    """
    routed = route_question(job.tournament, job.question)
    job.route = routed.as_json()
    job.model_name = routed.chat.model
    job.timings = {"route": routed.chat.timings()}
    if routed.route.intent != "unknown":
        job.facts = build_facts(job.tournament, job.user, routed.route)
