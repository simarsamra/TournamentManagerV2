"""Test doubles for the Ollama client (AI-2).

FakeOllama replaces the client's single network seam (`client._open`), so
tests exercise the real request building and response parsing without a
model. Replies are consumed in order; anything unconsumed or missing fails
the test.
"""
import io
import json
import urllib.error
from unittest import mock


def chat_reply(content, *, model="fake-model", total_ns=1_500_000_000, load_ns=0,
               prompt_tokens=100, output_tokens=20):
    """A /api/chat response body as Ollama documents it."""
    return {
        "model": model, "created_at": "2026-01-01T00:00:00Z",
        "message": {"role": "assistant", "content": content},
        "done": True, "done_reason": "stop",
        "total_duration": total_ns, "load_duration": load_ns,
        "prompt_eval_count": prompt_tokens, "eval_count": output_tokens,
    }


def http_error(code, body=b""):
    return urllib.error.HTTPError(
        "http://ollama.test", code, "error", hdrs=None, fp=io.BytesIO(body)
    )


class FakeOllama:
    def __init__(self):
        self.requests = []
        self._replies = []

    def respond(self, payload):
        """Queue a JSON reply (a dict) or an exception to raise."""
        self._replies.append(payload)
        return self

    def respond_chat(self, content, **kwargs):
        return self.respond(chat_reply(content, **kwargs))

    def _open(self, request, timeout):
        self.requests.append({
            "method": request.get_method(),
            "url": request.full_url,
            "timeout": timeout,
            "body": json.loads(request.data) if request.data else None,
        })
        if not self._replies:
            raise AssertionError(f"FakeOllama: unexpected request to {request.full_url}")
        reply = self._replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply if isinstance(reply, bytes) else json.dumps(reply).encode()

    def __enter__(self):
        self._patch = mock.patch("core.ai.client._open", self._open)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False
