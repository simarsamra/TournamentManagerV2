"""Measure models on the labelled AI analytics question set (AI-8).

    python manage.py ai_eval                          # the configured model
    python manage.py ai_eval --model qwen3.5:9b --model gemma4:12b
    python manage.py ai_eval --json eval-results.json

Safe on a production server: no database access. Not run in CI (CI has no
model); its logic is tested there with a fake.
"""
import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.ai import client
from core.ai.evaluation import evaluate, load_questions, summary_lines


class Command(BaseCommand):
    help = "Measure routing accuracy, explanation grounding and latency per model."

    def add_arguments(self, parser):
        parser.add_argument("--model", action="append", dest="models",
                            help="Model to evaluate (repeatable). Default: DJANGO_OLLAMA_MODEL.")
        parser.add_argument("--no-explain", action="store_true", help="Skip the explanation cases.")
        parser.add_argument("--limit", type=int, help="Only the first N routing questions.")
        parser.add_argument("--json", dest="json_path", help="Also write the full results to this file.")

    def handle(self, *args, **options):
        models = options["models"] or [settings.OLLAMA_MODEL]
        data = load_questions()
        results = []
        for model in models:
            extra = "" if options["no_explain"] else f" and {len(data['explain_cases'])} explanations"
            count = min(options["limit"] or len(data["questions"]), len(data["questions"]))
            self.stdout.write(f"Evaluating {model} on {count} questions{extra}…")

            def progress(report):
                if report.route_total % 10 == 0:
                    self.stdout.write(f"  …{report.route_total} questions")

            try:
                report = evaluate(model, data, explain_answers=not options["no_explain"],
                                  limit=options["limit"], progress=progress)
            except client.OllamaUnavailable as exc:
                raise CommandError(f"{exc}. Run manage.py ai_doctor first.")
            for line in summary_lines(report):
                self.stdout.write(line)
            results.append(report.as_dict())
        if options["json_path"]:
            with open(options["json_path"], "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
            self.stdout.write(f"Wrote {options['json_path']}")
