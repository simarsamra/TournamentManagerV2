"""Tests for AI_ANALYTICS_PLAN.md (question answering over the analytics)."""
import urllib.error
from datetime import timedelta
import urllib.request
from io import StringIO
from unittest import mock

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from core import analytics
from core.ai import client, jobs
from core.ai.facts import MAX_FACTS_CHARS, Route, build_facts, serialise, team_keys
from core.ai.testing import FakeOllama, chat_reply, http_error
from core.checks import check_ai_analytics_settings
from core.models import (
    AIQuestion, AuditLog, Match, OrganizerProfile, Player, Team, TeamMembership,
    TeamTournamentParticipation, Tournament, TournamentIndividualRegistration,
)
from core.standings import calculate_standings
from core.test_runner import NetworkAccessBlocked


def _make_organizer(username):
    user = User.objects.create_user(username=username, password="Regression-Pass-1")
    OrganizerProfile.objects.filter(user=user).update(verified=True)
    user.refresh_from_db()
    return user


class AnalyticsFunctionTests(TestCase):
    """AI-1: the analytics calculations are callable without a request, which
    is how the AI layer will use them."""

    def setUp(self):
        self.organizer = _make_organizer("org")
        self.tournament = Tournament.objects.create(
            name="T", format="round_robin", status="active", players_per_team=1,
            created_by=self.organizer,
        )
        self.a, self.b, self.c = (Team.objects.create(name=n) for n in ("Aces", "Bolts", "Comets"))
        for team in (self.a, self.b, self.c):
            TeamTournamentParticipation.objects.create(
                team=team, tournament=self.tournament, status="active"
            )
        Match.objects.create(
            tournament=self.tournament, match_number=1, team1=self.a, team2=self.b,
            score_team1=3, score_team2=1, winner=self.a, status="confirmed",
        )
        self.upcoming = Match.objects.create(
            tournament=self.tournament, match_number=2, team1=self.b, team2=self.c,
            status="upcoming",
        )

    def test_can_view_analytics_follows_a1(self):
        other = _make_organizer("other")
        player = User.objects.create_user(username="p", password="Regression-Pass-1")
        TeamMembership.objects.create(team=self.c, user=player, role="captain")
        self.assertEqual(analytics.can_view_analytics(self.organizer, self.tournament), (True, True))
        self.assertEqual(analytics.can_view_analytics(player, self.tournament), (True, False))
        self.assertEqual(analytics.can_view_analytics(other, self.tournament), (False, False))

    def test_head_to_head_needs_two_different_teams(self):
        self.assertIsNone(analytics.head_to_head(self.tournament, self.a, self.a))
        self.assertIsNone(analytics.head_to_head(self.tournament, self.a, None))
        card = analytics.head_to_head(self.tournament, self.a, self.b)
        self.assertEqual((card["total_matches"], card["team1_wins"], card["team2_wins"]), (1, 1, 0))

    def test_rolling_form_and_prep_for_a_named_team(self):
        self.assertEqual(analytics.rolling_form(self.tournament, None, 5), [])
        rows = analytics.rolling_form(self.tournament, self.b, 5)
        self.assertEqual([(r["result"], r["opponent"]) for r in rows], [("L", "Aces")])
        prep = analytics.next_opponent_prep(self.tournament, self.b)
        self.assertEqual((prep["opponent"], prep["opponent_record"]["wins"]), (self.c, 0))
        self.assertIsNone(analytics.next_opponent_prep(self.tournament, self.a))

    def test_simulate_with_picks_by_match_pk(self):
        standings = calculate_standings(self.tournament)
        analytics.label_standings(self.tournament, standings)
        offered, total = analytics.simulator_matches(self.tournament)
        self.assertEqual(([m.pk for m in offered], total), ([self.upcoming.pk], 1))
        self.assertEqual(analytics.simulate(self.tournament, standings, [], {}), (None, False))
        simulated, has_choices = analytics.simulate(
            self.tournament, standings, offered, {self.upcoming.pk: "team2"}
        )
        self.assertTrue(has_choices)
        comets = next(row for row in simulated if row["team"] == self.c)
        self.assertEqual((comets["points"], comets["point_change"]), (3, 3))
        # The real rows are untouched.
        self.assertEqual(next(r for r in standings if r["team"] == self.c)["points"], 0)


class PrepSheetDefaultTests(TestCase):
    """AI-1b: with no prep_team in the URL, the prep sheet fell back to the
    rolling-form team. Since A-12 swaps only the form card over HTMX, the live
    prep card and a reload of the pushed URL then showed different teams."""

    def test_prep_sheet_defaults_to_first_active_team_not_form_team(self):
        organizer = _make_organizer("org")
        tournament = Tournament.objects.create(
            name="T", format="round_robin", status="active", players_per_team=1,
            created_by=organizer,
        )
        aces, bolts = (Team.objects.create(name=n) for n in ("Aces", "Bolts"))
        for team in (aces, bolts):
            TeamTournamentParticipation.objects.create(team=team, tournament=tournament, status="active")
        self.client.force_login(organizer)
        response = self.client.get("/analytics/", {"tournament": tournament.pk, "form_team": bolts.pk})
        self.assertEqual(response.context["form_team"], bolts)
        self.assertEqual(response.context["prep_team"], aces)

    def test_explicit_prep_team_still_wins(self):
        organizer = _make_organizer("org")
        tournament = Tournament.objects.create(
            name="T", format="round_robin", status="active", players_per_team=1,
            created_by=organizer,
        )
        aces, bolts = (Team.objects.create(name=n) for n in ("Aces", "Bolts"))
        for team in (aces, bolts):
            TeamTournamentParticipation.objects.create(team=team, tournament=tournament, status="active")
        self.client.force_login(organizer)
        response = self.client.get("/analytics/", {"tournament": tournament.pk, "prep_team": bolts.pk})
        self.assertEqual(response.context["prep_team"], bolts)


# AI-2

AI_SETTINGS = dict(
    OLLAMA_URL="http://127.0.0.1:11434", OLLAMA_MODEL="qwen3.5:9b", OLLAMA_THINK=False,
    OLLAMA_NUM_CTX=8192, OLLAMA_TIMEOUT_SECONDS=60, OLLAMA_KEEP_ALIVE="30m",
)


class AISettingsDefaultTests(SimpleTestCase):
    def test_off_by_default_with_the_chosen_model(self):
        self.assertFalse(settings.AI_ANALYTICS_ENABLED)
        self.assertEqual(settings.OLLAMA_MODEL, "qwen3.5:9b")
        self.assertIs(settings.OLLAMA_THINK, False)
        self.assertEqual(settings.OLLAMA_URL, "http://127.0.0.1:11434")
        self.assertEqual(settings.AI_ANALYTICS_AUDIENCE, "managers")


@override_settings(**AI_SETTINGS)
class OllamaClientTests(SimpleTestCase):
    def test_chat_sends_the_documented_body(self):
        schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
        with FakeOllama() as fake:
            fake.respond_chat('{"x": "y"}')
            result = client.chat([{"role": "user", "content": "hi"}], schema=schema, num_predict=64)
        request = fake.requests[0]
        self.assertEqual((request["method"], request["url"]), ("POST", "http://127.0.0.1:11434/api/chat"))
        self.assertEqual(request["timeout"], 60)
        self.assertEqual(request["body"], {
            "model": "qwen3.5:9b",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "options": {"temperature": 0.0, "num_ctx": 8192, "num_predict": 64},
            "keep_alive": "30m",
            "format": schema,
            "think": False,
        })
        self.assertEqual(result.content, '{"x": "y"}')
        self.assertEqual(result.timings(), {
            "total_ms": 1500, "load_ms": 0, "prompt_tokens": 100, "output_tokens": 20,
        })

    @override_settings(OLLAMA_THINK=None)
    def test_think_and_format_are_omitted_when_not_set(self):
        body = client.build_chat_body([{"role": "user", "content": "hi"}])
        self.assertNotIn("think", body)
        self.assertNotIn("format", body)

    def test_version_and_installed_models(self):
        with FakeOllama() as fake:
            fake.respond({"version": "0.33.1"})
            fake.respond({"models": [{"name": "qwen3.5:9b", "model": "qwen3.5:9b"}, {"model": "gemma4:12b"}]})
            self.assertEqual(client.version(), "0.33.1")
            self.assertEqual(client.installed_models(), {"qwen3.5:9b", "gemma4:12b"})
        self.assertEqual(
            [r["url"] for r in fake.requests],
            ["http://127.0.0.1:11434/api/version", "http://127.0.0.1:11434/api/tags"],
        )

    def test_error_mapping(self):
        cases = [
            (http_error(503, b"server overloaded"), client.OllamaOverloaded),
            (http_error(404, b'{"error":"model not found"}'), client.OllamaBadResponse),
            (urllib.error.URLError(ConnectionRefusedError(111, "refused")), client.OllamaUnavailable),
            (urllib.error.URLError(TimeoutError("timed out")), client.OllamaTimeout),
            (TimeoutError("timed out"), client.OllamaTimeout),
            (ConnectionResetError(104, "reset"), client.OllamaUnavailable),
            (b"<html>not json</html>", client.OllamaBadResponse),
            ({"message": {"role": "assistant"}}, client.OllamaBadResponse),
        ]
        for reply, expected in cases:
            with self.subTest(reply=reply), FakeOllama() as fake:
                fake.respond(reply)
                with self.assertRaises(expected):
                    client.chat([{"role": "user", "content": "hi"}])
        self.assertTrue(issubclass(client.OllamaOverloaded, client.OllamaUnavailable))

    def test_environment_proxies_are_ignored(self):
        def proxy_handlers(opener):
            return [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]

        with mock.patch.dict("os.environ", {"HTTP_PROXY": "http://proxy.test:3128"}):
            # A default opener picks the proxy up from the environment...
            self.assertEqual(
                [h.proxies.get("http") for h in proxy_handlers(urllib.request.build_opener())],
                ["http://proxy.test:3128"],
            )
        # ...the client's opener has no proxy handler at all.
        self.assertEqual(proxy_handlers(client._OPENER), [])

    def test_tests_cannot_reach_a_real_model(self):
        with self.assertRaises(NetworkAccessBlocked):
            client.version()


class AISettingsCheckTests(SimpleTestCase):
    def _ids(self):
        return [problem.id for problem in check_ai_analytics_settings()]

    @override_settings(AI_ANALYTICS_ENABLED=False, OLLAMA_MODEL="")
    def test_nothing_checked_while_disabled(self):
        self.assertEqual(self._ids(), [])

    @override_settings(AI_ANALYTICS_ENABLED=True, **AI_SETTINGS)
    def test_default_configuration_passes(self):
        self.assertEqual(self._ids(), [])

    @override_settings(AI_ANALYTICS_ENABLED=True, **{**AI_SETTINGS, "OLLAMA_MODEL": ""})
    def test_enabled_without_a_model_is_an_error(self):
        self.assertEqual(self._ids(), ["core.E101"])

    @override_settings(AI_ANALYTICS_ENABLED=True, **{**AI_SETTINGS, "OLLAMA_URL": "http://10.0.0.5:11434"})
    def test_remote_ollama_warns(self):
        self.assertEqual(self._ids(), ["core.W101"])

    @override_settings(AI_ANALYTICS_ENABLED=True, **{**AI_SETTINGS, "OLLAMA_URL": "127.0.0.1:11434"})
    def test_url_without_scheme_is_an_error(self):
        self.assertEqual(self._ids(), ["core.E102"])


@override_settings(**AI_SETTINGS, AI_ANALYTICS_ENABLED=True)
class AIDoctorTests(SimpleTestCase):
    def _run(self, fake):
        out = StringIO()
        with fake:
            try:
                call_command("ai_doctor", stdout=out)
            except CommandError as exc:
                return out.getvalue(), exc
        return out.getvalue(), None

    def _healthy(self, content='{"colour": "blue"}', **chat_kwargs):
        return (FakeOllama()
                .respond({"version": "0.33.1"})
                .respond({"models": [{"name": "qwen3.5:9b"}]})
                .respond(chat_reply(content, **chat_kwargs)))

    def test_all_checks_pass(self):
        output, error = self._run(self._healthy(load_ns=2_500_000_000))
        self.assertIsNone(error, output)
        self.assertIn("Ollama 0.33.1 is reachable", output)
        self.assertIn("Model qwen3.5:9b is pulled", output)
        self.assertIn("honoured the JSON schema ('blue')", output)
        self.assertIn("`think: false` accepted", output)
        self.assertIn("loading the model 2500 ms", output)
        self.assertIn("All checks passed.", output)

    def test_unreachable(self):
        fake = FakeOllama().respond(urllib.error.URLError(ConnectionRefusedError(111, "refused")))
        output, error = self._run(fake)
        self.assertIsNotNone(error)
        self.assertIn("Is Ollama running?", output)

    def test_model_not_pulled(self):
        fake = FakeOllama().respond({"version": "0.33.1"}).respond({"models": [{"name": "gemma4:12b"}]})
        output, error = self._run(fake)
        self.assertIsNotNone(error)
        self.assertIn("run `ollama pull qwen3.5:9b` (installed: gemma4:12b)", output)

    def test_reply_outside_the_schema(self):
        output, error = self._run(self._healthy(content="The sky is blue."))
        self.assertIsNotNone(error)
        self.assertIn("didn't match the JSON schema", output)

    def test_model_rejecting_think_gets_a_hint(self):
        fake = (FakeOllama().respond({"version": "0.33.1"})
                .respond({"models": [{"name": "qwen3.5:9b"}]})
                .respond(http_error(400, b'{"error":"model does not support thinking"}')))
        output, error = self._run(fake)
        self.assertIsNotNone(error)
        self.assertIn("set DJANGO_OLLAMA_THINK= (empty)", output)



# AI-3

@override_settings(AI_ANALYTICS_ENABLED=True, AI_ANALYTICS_AUDIENCE="managers",
                   AI_JOB_STALE_SECONDS=600, AI_RETENTION_DAYS=30)
class AIJobQueueTests(TestCase):
    def setUp(self):
        self.organizer = _make_organizer("org")
        self.tournament = Tournament.objects.create(
            name="T", format="round_robin", status="active", players_per_team=1,
            created_by=self.organizer,
        )

    def _job(self, user=None, **fields):
        return AIQuestion.objects.create(
            user=user or self.organizer, tournament=self.tournament, question="How are the Aces?",
            **fields,
        )

    def _age(self, job, **delta):
        AIQuestion.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(**delta))

    # -- claiming --

    def test_claims_the_oldest_pending_job_exactly_once(self):
        newer, older = self._job(), self._job()
        self._age(older, minutes=5)
        claimed = jobs.claim_next()
        self.assertEqual((claimed.pk, claimed.status), (older.pk, "running"))
        self.assertIsNotNone(claimed.started_at)
        self.assertEqual(jobs.claim_next().pk, newer.pk)
        self.assertIsNone(jobs.claim_next())
        self.assertEqual(AIQuestion.objects.filter(status="running").count(), 2)

    def test_a_job_taken_by_another_worker_is_skipped(self):
        job = self._job()
        real_filter = AIQuestion.objects.filter
        raced = []

        def racing_filter(*args, **kwargs):
            # Just before our compare-and-set UPDATE, another worker claims it.
            if kwargs.get("pk") == job.pk and kwargs.get("status") == "pending" and not raced:
                raced.append(True)
                real_filter(pk=job.pk).update(status="running")
            return real_filter(*args, **kwargs)

        with mock.patch.object(AIQuestion.objects, "filter", side_effect=racing_filter):
            self.assertIsNone(jobs.claim_next())
        self.assertEqual(raced, [True])

    # -- reaping --

    def test_stale_running_jobs_are_reaped(self):
        stale = self._job(status="running", started_at=timezone.now() - timedelta(minutes=20))
        fresh = self._job(status="running", started_at=timezone.now() - timedelta(minutes=2))
        pending = self._job()
        self.assertEqual(jobs.reap_stale(), 1)
        stale.refresh_from_db()
        self.assertEqual((stale.status, stale.error), ("failed", jobs.MSG_STALE))
        self.assertIsNotNone(stale.finished_at)
        self.assertEqual(AIQuestion.objects.get(pk=fresh.pk).status, "running")
        self.assertEqual(AIQuestion.objects.get(pk=pending.pk).status, "pending")

    # -- processing --

    def _run(self, job, processor):
        jobs.claim_next()
        job.refresh_from_db()
        jobs.process(job, processor)
        job.refresh_from_db()
        return job

    def test_success_saves_what_the_processor_filled_in(self):
        def processor(job):
            job.route = {"intent": "form"}
            job.facts = {"form": {"team": "Aces"}}
            job.answer = "Aces won 2 of 5."
            job.answer_verified = True
            job.model_name = "qwen3.5:9b"
            job.timings = {"total_ms": 900}

        job = self._run(self._job(), processor)
        self.assertEqual((job.status, job.error), ("done", ""))
        self.assertEqual(job.route, {"intent": "form"})
        self.assertEqual((job.answer, job.answer_verified, job.model_name), ("Aces won 2 of 5.", True, "qwen3.5:9b"))
        self.assertIsNotNone(job.finished_at)

    def test_access_is_rechecked_before_the_model_is_called(self):
        other = _make_organizer("other")  # not this tournament's manager
        processor = mock.Mock()
        job = self._run(self._job(user=other), processor)
        processor.assert_not_called()
        self.assertEqual((job.status, job.error), ("failed", jobs.MSG_NO_ACCESS))

    def test_managers_audience_excludes_enrolled_players(self):
        team = Team.objects.create(name="Aces")
        TeamTournamentParticipation.objects.create(team=team, tournament=self.tournament, status="active")
        player = User.objects.create_user(username="p", password="Regression-Pass-1")
        TeamMembership.objects.create(team=team, user=player, role="captain")
        processor = mock.Mock()
        self.assertEqual(self._run(self._job(user=player), processor).status, "failed")
        processor.assert_not_called()
        with override_settings(AI_ANALYTICS_AUDIENCE="all"):
            self.assertEqual(self._run(self._job(user=player), processor).status, "done")

    def test_model_errors_become_friendly_messages(self):
        for exc, message in (
            (client.OllamaTimeout("slow"), jobs.MSG_SLOW),
            (client.OllamaUnavailable("down"), jobs.MSG_UNAVAILABLE),
            (client.OllamaOverloaded("503"), jobs.MSG_UNAVAILABLE),
            (ValueError("bug"), jobs.MSG_ERROR),
        ):
            with self.subTest(exc=exc), self.assertLogs("core.ai", level="WARNING") as logs:
                job = self._run(self._job(), mock.Mock(side_effect=exc))
            self.assertEqual((job.status, job.error), ("failed", message))
            self.assertIn(str(exc), "\n".join(logs.output))

    def test_a_reaped_job_is_not_resurrected(self):
        def slow_processor(job):
            # The reaper gives up on the job while the model is still working.
            AIQuestion.objects.filter(pk=job.pk).update(status="failed", error=jobs.MSG_STALE)
            job.answer = "late"

        with self.assertLogs("core.ai", level="WARNING"):
            job = self._run(self._job(), slow_processor)
        self.assertEqual((job.status, job.error, job.answer), ("failed", jobs.MSG_STALE, ""))

    def test_pipeline_placeholder_fails_cleanly_until_ai5(self):
        with self.assertLogs("core.ai", level="ERROR"):
            job = self._run(self._job(), None)
        self.assertEqual((job.status, job.error), ("failed", jobs.MSG_ERROR))

    # -- purge --

    def test_purge_deletes_only_rows_past_retention(self):
        old, recent = self._job(), self._job()
        self._age(old, days=31)
        self._age(recent, days=29)
        self.assertEqual(jobs.purge_old(dry_run=True), 1)
        self.assertEqual(AIQuestion.objects.count(), 2)
        out = StringIO()
        call_command("ai_purge", stdout=out)
        self.assertIn("Deleted 1 AI question(s) older than 30 days.", out.getvalue())
        self.assertEqual(list(AIQuestion.objects.values_list("pk", flat=True)), [recent.pk])

    # -- worker command --

    def test_worker_once_processes_one_job(self):
        first, second = self._job(), self._job()
        self._age(first, minutes=1)
        out = StringIO()
        with mock.patch("core.ai.pipeline.answer_question") as answer:
            call_command("ai_worker", "--once", stdout=out)
        answer.assert_called_once()
        self.assertIn(f"question #{first.pk} done", out.getvalue())
        self.assertEqual(AIQuestion.objects.get(pk=second.pk).status, "pending")

    def test_worker_reaps_before_claiming(self):
        stale = self._job(status="running", started_at=timezone.now() - timedelta(hours=1))
        with self.assertLogs("core.ai", level="WARNING"):
            call_command("ai_worker", "--once", stdout=StringIO())
        self.assertEqual(AIQuestion.objects.get(pk=stale.pk).status, "failed")

    @override_settings(AI_ANALYTICS_ENABLED=False)
    def test_worker_refuses_to_run_while_disabled(self):
        with self.assertRaises(CommandError):
            call_command("ai_worker", "--once", stdout=StringIO())


class AIWorkerConnectionTests(TestCase):
    """The worker recycles DB connections between jobs (a long-running process
    mustn't outlive CONN_MAX_AGE), but never inside a transaction: in a test
    that closed the TestCase's own connection on PostgreSQL ("connection
    already closed" for every later test). SQLite never showed it, because
    Django doesn't close an in-memory database."""

    @override_settings(AI_ANALYTICS_ENABLED=True)
    def test_worker_does_not_close_connections_inside_a_transaction(self):
        with mock.patch("core.management.commands.ai_worker.close_old_connections") as close:
            call_command("ai_worker", "--once", stdout=StringIO())
        close.assert_not_called()

    @override_settings(AI_ANALYTICS_ENABLED=True)
    def test_worker_recycles_connections_outside_a_transaction(self):
        with mock.patch("core.management.commands.ai_worker.close_old_connections") as close, \
                mock.patch("core.management.commands.ai_worker.connection") as conn:
            conn.in_atomic_block = False
            call_command("ai_worker", "--once", stdout=StringIO())
        close.assert_called_once()


# AI-4

class FactsBuilderTests(TestCase):
    SECRETS = ("SECRET-NOTE", "SECRET-AVAIL", "secret@example.com", "SECRET-PLAYER",
               "SECRET-AUDIT", "secretusername")

    def setUp(self):
        self.organizer = _make_organizer("org")
        self.tournament = Tournament.objects.create(
            name="League", format="round_robin", status="active", players_per_team=1,
            created_by=self.organizer,
        )
        self.teams = []
        for name in ("Aces", "Bolts", "Comets", "Drakes"):
            team = Team.objects.create(name=name)
            TeamTournamentParticipation.objects.create(
                team=team, tournament=self.tournament, status="active",
                availability_notes="SECRET-AVAIL",
            )
            Player.objects.create(team=team, name="SECRET-PLAYER")
            self.teams.append(team)
        self.aces, self.bolts, self.comets, self.drakes = self.teams
        member = User.objects.create_user(
            username="secretusername", email="secret@example.com", password="Regression-Pass-1",
        )
        TeamMembership.objects.create(team=self.aces, user=member, role="captain")
        self._match(1, self.aces, self.bolts, 3, 1)
        self._match(2, self.aces, self.comets, 2, 2)
        self._match(3, self.bolts, self.comets, 0, 1)
        self.upcoming = Match.objects.create(
            tournament=self.tournament, match_number=4, team1=self.bolts, team2=self.drakes,
            status="upcoming", notes="SECRET-NOTE",
        )
        AuditLog.objects.create(user=self.organizer, action="x", details="SECRET-AUDIT",
                                tournament=self.tournament)

    def _match(self, number, t1, t2, s1, s2):
        winner = t1 if s1 > s2 else t2 if s2 > s1 else None
        return Match.objects.create(
            tournament=self.tournament, match_number=number, team1=t1, team2=t2,
            score_team1=s1, score_team2=s2, winner=winner, status="confirmed",
            notes="SECRET-NOTE",
        )

    def _all_intent_facts(self):
        return [
            build_facts(self.tournament, self.organizer, route) for route in (
                Route("head_to_head", self.aces, self.bolts),
                Route("form", self.aces, window=3),
                Route("next_match", self.bolts),
                Route("what_if", match=self.upcoming, winner="team2"),
                Route("standings"), Route("team_performance"), Route("unknown"),
            )
        ]

    def test_route_facts_carry_the_computed_numbers(self):
        h2h, form, next_match, what_if = self._all_intent_facts()[:4]
        self.assertEqual(h2h["head_to_head"], {
            "team_a": "Aces", "team_b": "Bolts", "meetings": 1, "team_a_wins": 1,
            "team_b_wins": 0, "draws": 0, "team_a_avg_score": 3.0, "team_b_avg_score": 1.0,
        })
        self.assertEqual(form["form"]["matches"], [
            {"opponent": "Bolts", "result": "W"}, {"opponent": "Comets", "result": "D"},
        ])
        self.assertEqual(form["form"]["win_rate_pct"], 50.0)
        self.assertEqual((next_match["next_match"]["opponent"], next_match["next_match"]["when"]),
                         ("Drakes", "not yet scheduled"))
        self.assertEqual(what_if["what_if"]["assumed_result"], "Drakes beat Bolts")
        drakes = next(r for r in what_if["what_if"]["projected_standings_top"] if r["team"] == "Drakes")
        self.assertEqual((drakes["points"], drakes["points_change"]), (3, 3))
        self.assertEqual(h2h["standings_top"][0], {
            "rank": 1, "team": "Aces", "played": 2, "wins": 1, "draws": 1, "losses": 0,
            "points": 4, "game_diff": 2,
        })

    def test_no_private_fields_in_any_intent(self):
        for facts in self._all_intent_facts():
            text = serialise(facts)
            for secret in self.SECRETS:
                self.assertNotIn(secret, text)

    def test_outsider_is_refused(self):
        with self.assertRaises(PermissionDenied):
            build_facts(self.tournament, _make_organizer("other"), Route("standings"))

    def test_only_active_teams_may_be_named(self):
        TeamTournamentParticipation.objects.filter(team=self.drakes).update(status="withdrawn")
        with self.assertRaises(ValueError):
            build_facts(self.tournament, self.organizer, Route("form", self.drakes))
        # ...though a withdrawn team still appears in the table, as on the page.
        facts = build_facts(self.tournament, self.organizer, Route("standings"))
        self.assertIn("Drakes", [row["team"] for row in facts["standings_top"]])

    def test_what_if_needs_a_match_the_simulator_offers(self):
        self.upcoming.status = "confirmed"
        self.upcoming.save()
        facts = build_facts(self.tournament, self.organizer,
                            Route("what_if", match=self.upcoming, winner="team1"))
        self.assertNotIn("what_if", facts)

    def test_bracket_formats_get_results_not_points(self):
        self.tournament.format = "knockout"
        self.tournament.save()
        facts = build_facts(self.tournament, self.organizer, Route("team_performance"))
        self.assertNotIn("standings_top", facts)
        self.assertEqual(facts["results_top"][0]["team"], "Aces")

    def test_route_validation(self):
        with self.assertRaises(ValueError):
            Route("predict_the_future")
        with self.assertRaises(ValueError):
            Route("form", window=7)

    def test_team_keys_are_opaque_and_in_page_order(self):
        keys = team_keys(analytics.active_teams(self.tournament, {}))
        self.assertEqual({k: t.name for k, t in keys.items()},
                         {"T1": "Aces", "T2": "Bolts", "T3": "Comets", "T4": "Drakes"})


class FactsSizeAndLabelTests(TestCase):
    def setUp(self):
        self.organizer = _make_organizer("org")

    def test_worst_case_fits_the_limit_for_every_intent(self):
        """Maximum-length names (Team.name is 100 chars), 30 teams, a full
        15-match form window: every intent fits without trimming."""
        tournament = Tournament.objects.create(
            name="L" * 200, format="round_robin", status="active", players_per_team=1,
            created_by=self.organizer,
        )
        teams = []
        for n in range(30):
            team = Team.objects.create(name=f"{n:02d}" + "x" * 98)
            TeamTournamentParticipation.objects.create(team=team, tournament=tournament, status="active")
            teams.append(team)
        for n, opponent in enumerate(teams[1:16], start=1):
            Match.objects.create(
                tournament=tournament, match_number=n, team1=teams[0], team2=opponent,
                score_team1=2, score_team2=1, winner=teams[0], status="confirmed",
            )
        upcoming = Match.objects.create(
            tournament=tournament, match_number=99, team1=teams[0], team2=teams[20], status="upcoming",
        )
        for route in (
            Route("head_to_head", teams[0], teams[1]), Route("form", teams[0], window=15),
            Route("next_match", teams[0]), Route("what_if", match=upcoming, winner="team2"),
            Route("standings"),
        ):
            with self.subTest(intent=route.intent):
                facts = build_facts(tournament, self.organizer, route)
                self.assertLessEqual(len(serialise(facts)), MAX_FACTS_CHARS)
                self.assertEqual(len(facts["standings_top"]), 8)  # nothing trimmed

    def test_trimming_keeps_the_question_rows_and_three_table_rows(self):
        from core.ai import facts as facts_module

        rows = [{"rank": i, "team": "t" * 80} for i in range(8)]
        facts = {"form": {"team": "Aces", "matches": ["W"] * 15}, "standings_top": rows}
        with mock.patch.object(facts_module, "MAX_FACTS_CHARS", 500):
            trimmed = facts_module._fit(facts)
        self.assertLessEqual(len(serialise(trimmed)), 500)
        self.assertLess(len(trimmed["standings_top"]), 8)
        self.assertEqual(trimmed["form"]["matches"], ["W"] * 15)
        with mock.patch.object(facts_module, "MAX_FACTS_CHARS", 100), self.assertRaises(ValueError):
            facts_module._fit({"form": {"matches": ["W"] * 50}, "standings_top": rows})

    def test_individual_mode_uses_display_names(self):
        from core.views import _ensure_shadow_team_for_registration

        tournament = Tournament.objects.create(
            name="IND", format="round_robin", status="active", players_per_team=1,
            registration_mode="individual", created_by=self.organizer,
        )
        shadows = []
        for username, display in (("pa", "Player Alpha"), ("pb", "Player Bravo")):
            user = User.objects.create_user(username=username, password="Regression-Pass-1")
            reg = TournamentIndividualRegistration.objects.create(
                tournament=tournament, user=user, display_name=display, status="active",
            )
            _ensure_shadow_team_for_registration(reg, tournament.sport_type)
            reg.refresh_from_db()
            shadows.append(reg.shadow_team)
        Match.objects.create(
            tournament=tournament, match_number=1, team1=shadows[0], team2=shadows[1],
            score_team1=2, score_team2=0, winner=shadows[0], status="confirmed",
        )
        for route in (Route("head_to_head", *shadows), Route("form", shadows[1]), Route("standings")):
            text = serialise(build_facts(tournament, self.organizer, route))
            self.assertNotIn("__tm_shadow_", text)
            self.assertIn("Player Alpha", text)
