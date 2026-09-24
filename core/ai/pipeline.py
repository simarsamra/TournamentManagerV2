"""Answering one question: route, compute, explain (AI_ANALYTICS_PLAN.md §1.1).

AI-5 (routing) and AI-7 (explanation) fill this in. Until then there is no
way to create a question from the site (the Ask box arrives in AI-6), and a
job that reaches the worker fails cleanly.
"""


def answer_question(job):
    """Fill in job.route / facts / answer / answer_verified / model_name /
    timings. Raise core.ai.client.OllamaError subclasses as they come."""
    raise NotImplementedError("Question answering arrives in AI-5.")
