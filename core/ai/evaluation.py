"""Measure a model on the labelled question set (AI_ANALYTICS_PLAN.md AI-8).

Runs the real router prompt and schema, and the real explanation prompt and
grounding check, against core/ai/eval/questions.json. Nothing touches the
database: teams are made up and explanation facts are fixed, so it is safe
to run on a production server.
"""
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from . import client
from .explain import explain, ungrounded_numbers
from .router import build_messages, build_schema

QUESTIONS_PATH = Path(__file__).parent / "eval" / "questions.json"


def load_questions(path=QUESTIONS_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def score_route(expect, reply):
    """Return (correct, reason). Mirrors what router.validate accepts: a
    single team may be in either slot, a pair in either order, and a
    what-if winner is judged by team, not by slot."""
    if not isinstance(reply, dict):
        return False, "reply is not a JSON object"
    intent = expect["intent"]
    if reply.get("intent") != intent:
        return False, f"intent {reply.get('intent')!r}"
    if "group" in expect and reply.get("group", "none") != expect["group"]:
        return False, f"group {reply.get('group')!r}"
    team_a, team_b = reply.get("team_a"), reply.get("team_b")
    if intent in ("form", "next_match"):
        team = team_a if team_a not in (None, "none") else team_b
        if team != expect["team"]:
            return False, f"team {team!r}"
        if intent == "form" and "window" in expect and reply.get("window") != expect["window"]:
            return False, f"window {reply.get('window')!r}"
    elif intent in ("head_to_head", "what_if"):
        if {team_a, team_b} != set(expect["teams"]):
            return False, f"teams {team_a!r}, {team_b!r}"
        if intent == "what_if":
            winner = {"team_a": team_a, "team_b": team_b, "draw": "draw"}.get(reply.get("winner"))
            if winner != expect["winner"]:
                return False, f"winner {reply.get('winner')!r}"
    return True, ""


def wording_problem(text, case):
    """Why an explanation's words are wrong for the tournament's structure,
    or "" (ST-12): the number check can't see a table invented for a
    knockout, or two group leaders called a 'nail-biter'."""
    lowered = text.lower()
    for phrase in case.get("must_not_contain", []):
        if phrase.lower() in lowered:
            return f"says {phrase!r}"
    missing = [p for p in case.get("must_mention_all", []) if p.lower() not in lowered]
    if missing:
        return f"doesn't mention {', '.join(repr(p) for p in missing)}"
    anyof = case.get("must_mention_any", [])
    if anyof and not any(p.lower() in lowered for p in anyof):
        return f"mentions none of {', '.join(repr(p) for p in anyof)}"
    return ""


def percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[index]


@dataclass
class ModelReport:
    model: str
    routed: int = 0
    route_total: int = 0
    misrouted: list = field(default_factory=list)       # (question, expected, reply or error)
    explained: int = 0
    explain_total: int = 0
    explain_hidden: list = field(default_factory=list)  # (question, text, ungrounded numbers)
    wording_failed: list = field(default_factory=list)  # (question, text, problem)
    explain_empty: int = 0
    route_ms: list = field(default_factory=list)
    explain_ms: list = field(default_factory=list)
    cold_load_ms: int | None = None
    errors: int = 0

    def as_dict(self):
        return {
            "model": self.model,
            "routing": {"correct": self.routed, "total": self.route_total, "misrouted": self.misrouted},
            "explanations": {"passed": self.explained, "total": self.explain_total,
                             "hidden": self.explain_hidden, "wording_failed": self.wording_failed,
                             "empty": self.explain_empty},
            "latency_ms": {
                "route_p50": percentile(self.route_ms, 50), "route_p95": percentile(self.route_ms, 95),
                "explain_p50": percentile(self.explain_ms, 50), "explain_p95": percentile(self.explain_ms, 95),
            },
            "cold_load_ms": self.cold_load_ms,
            "errors": self.errors,
        }


def evaluate(model, data=None, explain_answers=True, limit=None, progress=None):
    """Run the question set against `model`. Raises client.OllamaUnavailable
    if Ollama can't be reached at all; other per-question errors are counted."""
    data = data or load_questions()
    keys = {key: SimpleNamespace(pk=key, display_label=name) for key, name in data["teams"].items()}
    report = ModelReport(model=model)
    # The main set is a league; the group set is the same teams in groups (ST-12).
    grouped = data.get("group_questions") or {}
    runs = [(item, None) for item in data["questions"]]
    runs += [(item, grouped["groups"]) for item in grouped.get("questions", [])]
    if limit:
        runs = runs[:limit]

    for item, groups in runs:
        report.route_total += 1
        started = time.monotonic()
        try:
            result = client.chat(build_messages(item["q"], keys, groups=groups),
                                 schema=build_schema(keys, list(groups or ())),
                                 temperature=0.0, num_predict=128, model=model)
        except client.OllamaUnavailable:
            raise
        except client.OllamaError as exc:
            report.errors += 1
            report.misrouted.append((item["q"], item["expect"], f"error: {exc}"))
            continue
        report.route_ms.append(round((time.monotonic() - started) * 1000))
        if report.cold_load_ms is None:
            report.cold_load_ms = result.load_ms
        try:
            reply = json.loads(result.content)
        except ValueError:
            reply = result.content
        correct, reason = score_route(item["expect"], reply)
        if correct:
            report.routed += 1
        else:
            report.misrouted.append((item["q"], item["expect"], reason))
        if progress:
            progress(report)

    if explain_answers:
        for case in data["explain_cases"]:
            report.explain_total += 1
            started = time.monotonic()
            try:
                text, _ = explain(case["q"], case["facts"])
            except client.OllamaUnavailable:
                raise
            except client.OllamaError:
                report.errors += 1
                continue
            report.explain_ms.append(round((time.monotonic() - started) * 1000))
            unsupported = ungrounded_numbers(text, case["facts"], case["q"])
            problem = wording_problem(text, case) if text else ""
            if not text:
                report.explain_empty += 1
            elif unsupported:
                report.explain_hidden.append((case["q"], text, unsupported))
            elif problem:
                report.wording_failed.append((case["q"], text, problem))
            else:
                report.explained += 1
    return report


def summary_lines(report):
    d = report.as_dict()
    lat = d["latency_ms"]
    pct = lambda n, t: f"{n}/{t} ({n / t:.0%})" if t else "n/a"  # noqa: E731
    lines = [
        f"Model {report.model}",
        f"  Routing       {pct(report.routed, report.route_total)} correct",
    ]
    if report.explain_total:
        lines.append(
            f"  Explanations  {pct(report.explained, report.explain_total)} passed the number and wording checks"
            f" ({len(report.explain_hidden)} hidden, {len(report.wording_failed)} wrong words,"
            f" {report.explain_empty} empty)"
        )
    lines.append(f"  Latency       route p50 {lat['route_p50']} ms, p95 {lat['route_p95']} ms"
                 + (f"; explain p50 {lat['explain_p50']} ms, p95 {lat['explain_p95']} ms"
                    if report.explain_ms else ""))
    lines.append(f"  Cold load     {report.cold_load_ms} ms (first call)" if report.cold_load_ms
                 else "  Cold load     model was already loaded")
    if report.errors:
        lines.append(f"  Errors        {report.errors}")
    for question, expected, got in report.misrouted:
        lines.append(f"  MISROUTED  {question!r}: expected {expected}, got {got}")
    for question, text, numbers in report.explain_hidden:
        lines.append(f"  HIDDEN     {question!r}: {text!r} (numbers not in facts: {', '.join(numbers)})")
    for question, text, problem in report.wording_failed:
        lines.append(f"  WORDING    {question!r}: {text!r} ({problem})")
    return lines
