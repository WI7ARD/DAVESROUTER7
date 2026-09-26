"""Local AI with Ollama (no API key, nothing leaves this computer).

Ollama serves an OpenAI-compatible API at ``http://localhost:11434/v1``; the app
talks to it through the existing OpenAI-compatible adapter (structured output
with automatic json_schema → json_object → plain fallbacks). This module only:

* checks whether Ollama is running and which models are installed,
* downloads ("pulls") a model with progress,
* creates/updates the app's "Ollama (local)" provider profile.

Only loopback addresses are accepted: board context is never sent to another
machine through this path. No API key is stored or needed.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from pcbrouter.ai.profiles import ProviderKind, ProviderProfile

DEFAULT_URL = "http://localhost:11434"
PROFILE_ID = "ollama-local"
PROFILE_NAME = "Ollama (local)"
LOCAL_TIMEOUT_S = 300.0  # CPU/iGPU inference of a 7B model can take minutes
#: (model, why) — structured JSON output quality matters more than size
RECOMMENDED: tuple[tuple[str, str], ...] = (
    ("qwen2.5:7b", "recommended: best JSON/engineering answers, ~4.7 GB, 16 GB RAM"),
    ("qwen2.5:3b", "faster, ~1.9 GB download, fits 8 GB RAM"),
    ("llama3.1:8b", "alternative, ~4.9 GB, 16 GB RAM"),
)
INSTALL_HINT = (
    "Install Ollama from https://ollama.com/download (Windows: run the installer, or "
    "'winget install -e --id Ollama.Ollama'), start it once, then check again."
)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class OllamaError(RuntimeError):
    pass


@dataclass
class OllamaStatus:
    running: bool
    url: str
    version: str | None = None
    models: list[str] = field(default_factory=list)
    error: str = ""

    def text(self) -> str:
        if not self.running:
            return f"Ollama is not running at {self.url}. {INSTALL_HINT}"
        models = ", ".join(self.models) if self.models else "none yet"
        return f"Ollama {self.version or ''} is running at {self.url}. Installed models: {models}."


def check_local(url: str) -> str:
    url = url.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "http" or (parsed.hostname or "") not in _LOCAL_HOSTS:
        raise OllamaError("only a local Ollama (http://localhost:…) is supported here")
    return url


def _get(url: str, timeout: float) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ollama_status(url: str = DEFAULT_URL, timeout: float = 3.0) -> OllamaStatus:
    url = check_local(url)
    try:
        version = _get(f"{url}/api/version", timeout).get("version")
        tags = _get(f"{url}/api/tags", timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return OllamaStatus(False, url, error=str(exc))
    models = sorted(m.get("name", "") for m in tags.get("models", []) if m.get("name"))
    return OllamaStatus(True, url, version, models)


def pull_model(
    model: str,
    url: str = DEFAULT_URL,
    progress: Callable[[str, float | None], None] | None = None,
    cancel: threading.Event | None = None,
    timeout: float = 30.0,
) -> None:
    """Download ``model`` (streamed progress: status text, fraction or None)."""
    url = check_local(url)
    if not model or any(c.isspace() for c in model):
        raise OllamaError("invalid model name")
    body = json.dumps({"model": model, "stream": True}).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/api/pull", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                if cancel is not None and cancel.is_set():
                    raise OllamaError("download canceled")
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                msg = json.loads(line)
                if msg.get("error"):
                    raise OllamaError(str(msg["error"]))
                total, done = msg.get("total"), msg.get("completed")
                frac = done / total if total and done is not None else None
                if progress is not None:
                    progress(str(msg.get("status", "")), frac)
    except (urllib.error.URLError, OSError) as exc:
        raise OllamaError(f"could not reach Ollama at {url}: {exc}") from exc


def ollama_profile(model: str, url: str = DEFAULT_URL) -> ProviderProfile:
    url = check_local(url)
    return ProviderProfile(
        profile_id=PROFILE_ID,
        name=PROFILE_NAME,
        kind=ProviderKind.OPENAI_COMPATIBLE,
        model_id=model,
        base_url=f"{url}/v1",
        requires_api_key=False,
        timeout_s=LOCAL_TIMEOUT_S,
    )


def apply_profile(ai_settings: Any, profile: ProviderProfile, make_default: bool = True) -> None:
    """Insert or replace the Ollama profile in ``AISettings`` (and make it default)."""
    others = [p for p in ai_settings.profiles if p.profile_id != profile.profile_id]
    ai_settings.profiles = [*others, profile]
    if make_default:
        ai_settings.default_profile_id = profile.profile_id
    if ai_settings.request_timeout_s < LOCAL_TIMEOUT_S:
        ai_settings.request_timeout_s = LOCAL_TIMEOUT_S


def chat_check(model: str, url: str = DEFAULT_URL, timeout: float = 300.0) -> str:
    """A tiny JSON-mode request through the OpenAI-compatible endpoint (no board
    data). Returns the model's reply text; raises OllamaError on failure."""
    url = check_local(url)
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": 'Reply with the JSON {"ok": true}'}],
        "response_format": {"type": "json_object"},
        "max_tokens": 20,
    }).encode("utf-8")  # fmt: skip
    req = urllib.request.Request(
        f"{url}/v1/chat/completions", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OllamaError(f"test request failed: {exc}") from exc
    try:
        return str(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise OllamaError(f"unexpected reply: {str(data)[:200]}") from exc
