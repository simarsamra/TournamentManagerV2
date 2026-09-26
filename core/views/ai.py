"""Ask the AI about a tournament's analytics (AI_ANALYTICS_PLAN.md AI-6).

The site only queues questions and shows their state; `manage.py ai_worker`
does the model calls (plan §1.2). Both URLs answer 404 while
AI_ANALYTICS_ENABLED is off, so the feature is invisible until configured.
"""
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import analytics
from ..models import AIQuestion, Tournament
from .helpers import _is_htmx_request, _team_display_label, throttled
from .reporting import ANALYTICS_WIDGET_PARAMS

__all__ = ["ai_ask", "ai_question_status", "ai_recap", "news_main", "news_team_take", "news_team_take_status"]

# After this long without an answer, say the question is queued.
QUEUED_NOTICE_SECONDS = 20

CARD_LABELS = {
    "h2h": "Head-to-Head",
    "form": "Rolling Form",
    "prep": "Next-Opponent Prep",
    "sim": "What-If Simulator",
    "standings": "Points Overview",
    "team_performance": "Team Performance",
}


def _require_enabled():
    if not settings.AI_ANALYTICS_ENABLED:
        raise Http404()


def _analytics_url(tournament_pk):
    return f"{reverse('analytics')}?{urlencode({'tournament': tournament_pk})}"


def _ask_redirect(request, *args, **kwargs):
    return _analytics_url(request.POST.get("tournament", ""))


def _page_link(request, question):
    """The analytics URL that shows the routed card: the page's current
    widget state (from htmx's HX-Current-URL) with the routed parameters
    replacing that card's own, anchored on the card."""
    route = question.route or {}
    card, params = route.get("card"), route.get("params") or {}
    current = request.headers.get("HX-Current-URL", "")
    query = dict(parse_qsl(urlsplit(current).query)) if current else {}
    if urlsplit(current).path not in ("", reverse("analytics")):
        query = {}
    for name in ANALYTICS_WIDGET_PARAMS.get(card, ()):
        query.pop(name, None)
    if card == "sim":
        query = {k: v for k, v in query.items() if not k.startswith("sim_")}
    query.update({k: str(v) for k, v in params.items()})
    query["tournament"] = str(question.tournament_id)
    anchor = {
        "standings": "analytics-points"
        if question.tournament.format in analytics.STANDINGS_FORMATS else "analytics-team-performance",
        "team_performance": "analytics-team-performance",
    }.get(card, f"analytics-{card}")
    return f"{reverse('analytics')}?{urlencode(query)}#{anchor}"


def _status_context(request, question):
    route = question.route or {}
    timings = question.timings or {}
    total_ms = sum((step or {}).get("total_ms") or 0 for step in timings.values())
    return {
        "q": question,
        "route": route,
        "facts": question.facts or {},
        "card_label": (
            "Team Performance"
            if route.get("card") == "standings"
            and question.tournament.format not in analytics.STANDINGS_FORMATS
            else CARD_LABELS.get(route.get("card"), "")
        ),
        "page_link": _page_link(request, question) if route.get("card") else "",
        "seconds": round(total_ms / 1000, 1) if total_ms else None,
        "queued_long": not question.is_finished
        and timezone.now() - question.created_at > timedelta(seconds=QUEUED_NOTICE_SECONDS),
    }


def _ask_error(request, tournament_pk, message):
    if _is_htmx_request(request):
        return render(request, "core/partials/ai_ask_error.html", {"message": message})
    messages.error(request, message)
    return redirect(_analytics_url(tournament_pk))


@login_required
@require_POST
# Per IP, above any one user's AI_QUESTIONS_PER_USER_PER_HOUR: this only
# guards against scripted floods; the per-user quota is the real limit.
@throttled("ai_ask", limit=240, window=3600, redirect_to=_ask_redirect)
def ai_ask(request):
    _require_enabled()
    from ..ai.access import may_ask

    tournament = Tournament.objects.filter(pk=request.POST.get("tournament") or None).first()
    if tournament is None:
        raise Http404()
    allowed, _ = analytics.can_view_analytics(request.user, tournament)
    if not allowed:
        messages.error(request, "You do not have access to that tournament.")
        return redirect("dashboard")
    if not may_ask(request.user, tournament):
        return _ask_error(request, tournament.pk, "Asking the AI is limited to this tournament's organizers.")

    question = " ".join((request.POST.get("question") or "").split())
    if not question:
        return _ask_error(request, tournament.pk, "Type a question first.")
    if len(question) > settings.AI_MAX_QUESTION_CHARS:
        return _ask_error(request, tournament.pk,
                          f"Please keep questions under {settings.AI_MAX_QUESTION_CHARS} characters.")
    hour_ago = timezone.now() - timedelta(hours=1)
    if AIQuestion.objects.filter(user=request.user, created_at__gte=hour_ago).count() \
            >= settings.AI_QUESTIONS_PER_USER_PER_HOUR:
        return _ask_error(request, tournament.pk,
                          "You've asked a lot of questions this hour. Please try again later.")
    if AIQuestion.objects.filter(status__in=("pending", "running")).count() >= settings.AI_MAX_PENDING:
        return _ask_error(request, tournament.pk, "The AI is busy right now. Please try again in a few minutes.")

    # A follow-up: only to the same user's earlier question about this tournament.
    parent = AIQuestion.objects.filter(
        pk=request.POST.get("parent") or None, user=request.user, tournament=tournament, kind="ask",
    ).first() if (request.POST.get("parent") or "").isdigit() else None
    job = AIQuestion.objects.create(user=request.user, tournament=tournament, question=question, parent=parent)
    if _is_htmx_request(request):
        context = _status_context(request, job)
        context["set_parent"] = True
        return render(request, "core/partials/ai_question_status.html", context)
    return redirect("ai_question_status", pk=job.pk)


@login_required
@require_POST
@throttled("ai_recap", limit=10, window=3600, redirect_to=_ask_redirect)
def ai_recap(request):
    """Commission a recap of the latest results (AI-9). Managers only; the
    published recap is then shown to everyone who can view the analytics."""
    _require_enabled()
    from ..ai.access import may_write_recap
    from ..ai.recap import latest_recap, new_results, recap_in_progress

    tournament = Tournament.objects.filter(pk=request.POST.get("tournament") or None).first()
    if tournament is None:
        raise Http404()
    allowed, _ = analytics.can_view_analytics(request.user, tournament)
    if not allowed:
        messages.error(request, "You do not have access to that tournament.")
        return redirect("dashboard")
    if not may_write_recap(request.user, tournament):
        return _ask_error(request, tournament.pk, "Only this tournament's organizers can write a recap.")
    if recap_in_progress(tournament):
        return _ask_error(request, tournament.pk, "A recap is already being written.")
    if not new_results(tournament, latest_recap(tournament)).exists():
        return _ask_error(request, tournament.pk, "There are no new results since the last recap.")
    hour_ago = timezone.now() - timedelta(hours=1)
    if AIQuestion.objects.filter(user=request.user, created_at__gte=hour_ago).count() \
            >= settings.AI_QUESTIONS_PER_USER_PER_HOUR:
        return _ask_error(request, tournament.pk,
                          "You've asked a lot of questions this hour. Please try again later.")
    if AIQuestion.objects.filter(status__in=("pending", "running")).count() >= settings.AI_MAX_PENDING:
        return _ask_error(request, tournament.pk, "The AI is busy right now. Please try again in a few minutes.")

    job = AIQuestion.objects.create(user=request.user, tournament=tournament, kind="recap",
                                    question="Recap of the latest results")
    if _is_htmx_request(request):
        return render(request, "core/partials/ai_recap_status.html", _status_context(request, job))
    return redirect("ai_question_status", pk=job.pk)


@login_required
def ai_question_status(request, pk):
    _require_enabled()
    # Only the asker may see a question (another user's id is a 404, not a 403).
    question = get_object_or_404(AIQuestion.objects.select_related("tournament"), pk=pk, user=request.user)
    context = _status_context(request, question)
    context["status_template"] = (
        "core/partials/ai_recap_status.html" if question.kind == "recap"
        else "core/partials/ai_question_status.html"
    )
    if _is_htmx_request(request):
        response = render(request, context["status_template"], context)
        if question.is_finished:
            response.status_code = 286  # htmx: stop polling
        return response
    context["tournament"] = question.tournament
    context["analytics_url"] = _analytics_url(question.tournament_id)
    return render(request, "core/ai_question.html", context)


# The dashboard news board's flip between the main news and "My team's take"
# (core/ai/team_news.py). Both are htmx fragments swapped into the board.

def _news_tournament(request, source):
    tournament = Tournament.objects.filter(pk=source.get("tournament") or None).first()
    if tournament is None:
        raise Http404()
    return tournament


def news_view_key(tournament):
    """Session key for which side of the news board the viewer picked, so the
    dashboard's own 15-second refresh (and a reload) keeps it."""
    return f"news_view_{tournament.pk}"


def flipped_team_take(request, tournament, team):
    """The team's take to show in place of the main news, if the viewer
    flipped to it and it's still for the current main update; else None."""
    from ..ai import team_news

    if team is None or request.session.get(news_view_key(tournament)) != "team":
        return None
    job = team_news.current_story(tournament, team)
    if job is None:
        # A new main update came out since: show it, not a stale take.
        request.session.pop(news_view_key(tournament), None)
        return None
    context = _team_take_context(request, tournament, team, job)
    return {"q": job, "story": context["story"], "team_label": context["team_label"],
            "queued_long": context.get("queued_long", False)}


def _team_take_context(request, tournament, team, job):
    context = _status_context(request, job) if job else {}
    context.update(tournament=tournament, team_label=_team_display_label(tournament, team),
                   story=(job.route or {}).get("story") or {} if job else {})
    return context


def _team_take_response(request, context, status=200):
    if not _is_htmx_request(request):
        if context.get("error"):
            messages.error(request, context["error"])
        return redirect("dashboard")
    return render(request, "core/partials/news_team_take.html", context, status=status)


@login_required
@require_POST
@throttled("news_team_take", limit=120, window=3600, redirect_to=lambda *a, **k: reverse("dashboard"))
def news_team_take(request):
    """Show the viewer's team's story for the current main news, queueing
    it if nobody on the team has asked yet (one per team per update)."""
    _require_enabled()
    from ..ai import team_news
    from ..ai.recap import latest_recap

    tournament = _news_tournament(request, request.POST)
    team = team_news.viewer_team(request.user, tournament)
    if team is None:
        return _team_take_response(request, {"tournament": tournament,
                                             "error": "Your team's take is for players in this tournament."})
    request.session[news_view_key(tournament)] = "team"
    job = team_news.current_story(tournament, team)
    reusable = job and (not job.is_finished or (job.status == "done" and job.answer_verified))
    if not reusable:
        hour_ago = timezone.now() - timedelta(hours=1)
        if AIQuestion.objects.filter(user=request.user, created_at__gte=hour_ago).count() \
                >= settings.AI_QUESTIONS_PER_USER_PER_HOUR:
            error = "You've asked the AI a lot this hour. Please try again later."
        elif AIQuestion.objects.filter(status__in=("pending", "running")).count() >= settings.AI_MAX_PENDING:
            error = "The AI is busy right now. Please try again in a few minutes."
        else:
            error = ""
        if error:
            context = _team_take_context(request, tournament, team, None)
            context["error"] = error
            return _team_take_response(request, context)
        source = latest_recap(tournament)
        job = AIQuestion.objects.create(
            user=request.user, tournament=tournament, kind=team_news.KIND,
            question=team_news.tag(team, source),
            route={"team_id": team.pk, "source_recap_id": source.pk if source else None},
        )
    return _team_take_response(request, _team_take_context(request, tournament, team, job))


@login_required
def news_team_take_status(request, pk):
    """Poll a team's story; anyone on that team may (it's theirs, not the
    asker's). Someone else's team is a 404."""
    _require_enabled()
    from ..ai import team_news

    job = get_object_or_404(AIQuestion.objects.select_related("tournament"), pk=pk, kind=team_news.KIND)
    team = team_news.viewer_team(request.user, job.tournament)
    if team is None or team.pk != team_news.job_team_id(job):
        raise Http404()
    response = _team_take_response(request, _team_take_context(request, job.tournament, team, job))
    if job.is_finished and _is_htmx_request(request):
        response.status_code = 286  # htmx: stop polling
    return response


@login_required
def news_main(request):
    """The main news board again, after a flip to the team's take."""
    _require_enabled()
    from ..ai import team_news
    from ..ai.recap import news_board

    tournament = _news_tournament(request, request.GET)
    allowed, _ = analytics.can_view_analytics(request.user, tournament)
    if not allowed:
        raise Http404()
    request.session.pop(news_view_key(tournament), None)
    if not _is_htmx_request(request):
        return redirect("dashboard")
    return render(request, "core/partials/news_main.html", {
        "tournament": tournament, "news": news_board(tournament),
        "news_team": team_news.viewer_team(request.user, tournament),
    })
