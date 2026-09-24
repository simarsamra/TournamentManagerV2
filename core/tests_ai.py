"""Tests for AI_ANALYTICS_PLAN.md (question answering over the analytics)."""
import urllib.error
import urllib.request
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from core import analytics
from core.ai import client
from core.ai.testing import FakeOllama, chat_reply, http_error
from core.checks import check_ai_analytics_settings
from core.models import (
    Match, OrganizerProfile, Team, TeamMembership, TeamTournamentParticipation, Tournament,
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
