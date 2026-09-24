"""Test runner that forbids outbound HTTP through urllib (AI_ANALYTICS_PLAN.md rule 7).

CI has no model. A test that forgets to use core.ai.testing.FakeOllama must
fail at once rather than hang on, or depend on, a real Ollama.
"""
import urllib.request
from unittest import mock

from django.test.runner import DiscoverRunner


class NetworkAccessBlocked(AssertionError):
    pass


def _blocked_open(self, fullurl, *args, **kwargs):
    url = getattr(fullurl, "full_url", fullurl)
    raise NetworkAccessBlocked(
        f"Test tried to open {url}. Use core.ai.testing.FakeOllama instead."
    )


class NoNetworkTestRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._network_patch = mock.patch.object(
            urllib.request.OpenerDirector, "open", _blocked_open
        )
        self._network_patch.start()

    def teardown_test_environment(self, **kwargs):
        self._network_patch.stop()
        super().teardown_test_environment(**kwargs)
