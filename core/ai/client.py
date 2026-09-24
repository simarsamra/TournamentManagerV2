"""Minimal Ollama HTTP client (AI-2). Standard library only.

Endpoints, request fields and response fields follow Ollama's API reference
(docs/api.md): POST /api/chat with stream=false, GET /api/version,
GET /api/tags.
"""
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from django.conf import settings


class OllamaError(Exception):
    """Base class. Messages are for logs and ai_doctor, not for end users."""


class OllamaUnavailable(OllamaError):
    """Ollama couldn't be reached (not running, wrong URL)."""


class OllamaOverloaded(OllamaUnavailable):
    """Ollama answered 503: its request queue (OLLAMA_MAX_QUEUE) is full."""


class OllamaTimeout(OllamaError):
    """No reply within OLLAMA_TIMEOUT_SECONDS."""


class OllamaBadResponse(OllamaError):
    """An HTTP error status, or a body that isn't what the API documents."""


@dataclass
class ChatResult:
    content: str
    model: str
    total_ms: int | None = None
    load_ms: int | None = None
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    raw: dict = field(default_factory=dict, repr=False)

    def timings(self):
        return {
            "total_ms": self.total_ms, "load_ms": self.load_ms,
            "prompt_tokens": self.prompt_tokens, "output_tokens": self.output_tokens,
        }


# Ignore HTTP(S)_PROXY from the environment: Ollama is local by design, and
# urllib would otherwise send loopback requests to a configured proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(request, timeout):
    """Send `request` and return the body. The single seam tests replace."""
    with _OPENER.open(request, timeout=timeout) as response:
        return response.read()


def _request(method, path, body=None, timeout=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        settings.OLLAMA_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    timeout = timeout or settings.OLLAMA_TIMEOUT_SECONDS
    try:
        raw = _open(request, timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:500].decode(errors="replace") if exc.fp else ""
        if exc.code == 503:
            raise OllamaOverloaded(f"Ollama is overloaded (HTTP 503): {detail}") from exc
        raise OllamaBadResponse(f"HTTP {exc.code} from Ollama {path}: {detail}") from exc
    except TimeoutError as exc:
        raise OllamaTimeout(f"No reply from Ollama {path} within {timeout}s") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise OllamaTimeout(f"No reply from Ollama {path} within {timeout}s") from exc
        raise OllamaUnavailable(
            f"Can't reach Ollama at {settings.OLLAMA_URL}: {exc.reason}"
        ) from exc
    except OSError as exc:  # connection reset and friends
        raise OllamaUnavailable(f"Can't reach Ollama at {settings.OLLAMA_URL}: {exc}") from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise OllamaBadResponse(f"Ollama {path} returned non-JSON: {raw[:200]!r}") from exc


def build_chat_body(messages, *, schema=None, temperature=0.0, num_predict=256, model=None):
    """The /api/chat request body. `schema` (a JSON schema) goes in `format`,
    which constrains the reply to it."""
    body = {
        "model": model or settings.OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_ctx": settings.OLLAMA_NUM_CTX,
            "num_predict": num_predict,
        },
        "keep_alive": settings.OLLAMA_KEEP_ALIVE,
    }
    if schema is not None:
        body["format"] = schema
    if settings.OLLAMA_THINK is not None:
        body["think"] = settings.OLLAMA_THINK
    return body


def _ms(nanoseconds):
    return round(nanoseconds / 1_000_000) if isinstance(nanoseconds, (int, float)) else None


def chat(messages, *, schema=None, temperature=0.0, num_predict=256, model=None, timeout=None):
    """POST /api/chat (non-streaming) and return the reply."""
    body = build_chat_body(
        messages, schema=schema, temperature=temperature, num_predict=num_predict, model=model,
    )
    data = _request("POST", "/api/chat", body, timeout=timeout)
    message = data.get("message") if isinstance(data, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise OllamaBadResponse(f"Ollama /api/chat reply has no message.content: {str(data)[:200]}")
    return ChatResult(
        content=content,
        model=data.get("model", body["model"]),
        total_ms=_ms(data.get("total_duration")),
        load_ms=_ms(data.get("load_duration")),
        prompt_tokens=data.get("prompt_eval_count"),
        output_tokens=data.get("eval_count"),
        raw=data,
    )


def version(timeout=5):
    data = _request("GET", "/api/version", timeout=timeout)
    if not isinstance(data, dict) or "version" not in data:
        raise OllamaBadResponse(f"Unexpected /api/version reply: {str(data)[:200]}")
    return data["version"]


def installed_models(timeout=5):
    """Names of pulled models, e.g. {"qwen3.5:9b"}."""
    data = _request("GET", "/api/tags", timeout=timeout)
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise OllamaBadResponse(f"Unexpected /api/tags reply: {str(data)[:200]}")
    return {m.get("name") or m.get("model") for m in models if isinstance(m, dict)}
