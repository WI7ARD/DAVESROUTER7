"""Ollama setup against a mock Ollama server on a real local socket.

The real Ollama can't run in CI; this server speaks the same HTTP API (native
/api/* and the OpenAI-compatible /v1/*), so everything up to the model itself runs.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

from pcbrouter.ai import ollama
from pcbrouter.ai.command_parser import parse_planner_response
from pcbrouter.ai.credentials import CredentialService, SessionCredentialStore
from pcbrouter.ai.openai_compatible_provider import OpenAICompatibleProvider
from pcbrouter.ai.requests import AIMode, AIRequest, ChatMessage
from pcbrouter.ai.wire_schema import planner_wire_schema
from pcbrouter.settings.settings import AppSettings

ANSWER = {
    "schema_version": 2,
    "mode": "analyze",
    "message": "hello from the local model",
    "analysis": None,
    "plan_steps": None,
    "commands": None,
    "clarification_needed": None,
    "unsupported_request": None,
}


class _Ollama(BaseHTTPRequestHandler):
    models: ClassVar[list[str]] = ["qwen2.5:3b"]
    requests: ClassVar[list[dict[str, Any]]] = []

    def log_message(self, *args: Any) -> None:
        pass

    def _json(self, body: Any, code: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/api/version":
            self._json({"version": "0.9.0-mock"})
        elif self.path == "/api/tags":
            self._json({"models": [{"name": m} for m in self.models]})
        elif self.path == "/v1/models":
            self._json(
                {
                    "object": "list",
                    "data": [
                        {"id": m, "object": "model", "created": 0, "owned_by": "library"}
                        for m in self.models
                    ],
                }
            )
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(
            {"path": self.path, "body": body, "auth": self.headers.get("Authorization")}
        )
        if self.path == "/api/pull":
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            for done in (0, 50, 100):
                line = {"status": "pulling", "total": 100, "completed": done}
                self.wfile.write((json.dumps(line) + "\n").encode())
            self.wfile.write(b'{"status": "success"}\n')
            self.models.append(body["model"])
            return
        if self.path == "/v1/chat/completions":
            small = body.get("max_tokens") == 20  # chat_check's tiny request
            content = '{"ok": true}' if small else json.dumps(ANSWER)
            self._json(
                {
                    "id": "c",
                    "object": "chat.completion",
                    "created": 0,
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": content},
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
                }
            )
            return
        self._json({"error": "not found"}, 404)


@pytest.fixture
def server() -> Iterator[str]:
    _Ollama.models = ["qwen2.5:3b"]
    _Ollama.requests = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Ollama)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_status_pull_profile_and_chat(server: str) -> None:
    st = ollama.ollama_status(server)
    assert st.running and st.models == ["qwen2.5:3b"] and "0.9.0-mock" in st.text()
    seen: list[tuple[str, float | None]] = []
    ollama.pull_model("qwen2.5:7b", server, lambda s, f: seen.append((s, f)))
    assert (seen[-2][1] == 1.0) and "qwen2.5:7b" in ollama.ollama_status(server).models
    settings = AppSettings()
    ollama.apply_profile(settings.ai, ollama.ollama_profile("qwen2.5:7b", server))
    ollama.apply_profile(settings.ai, ollama.ollama_profile("qwen2.5:7b", server))  # idempotent
    assert [p.profile_id for p in settings.ai.profiles] == ["ollama-local"]
    assert settings.ai.default_profile_id == "ollama-local"
    p = settings.ai.profiles[0]
    assert p.base_url == f"{server}/v1" and not p.requires_api_key
    assert "ok" in ollama.chat_check("qwen2.5:7b", server)


def test_not_running_and_remote_hosts_refused() -> None:
    st = ollama.ollama_status("http://127.0.0.1:9", timeout=0.5)
    assert not st.running and "ollama.com" in st.text()
    with pytest.raises(ollama.OllamaError):
        ollama.ollama_status("http://example.com:11434")


def test_the_app_ai_adapter_talks_to_ollama(server: str) -> None:
    """The profile the setup creates works with the app's real OpenAI-compatible
    adapter (real SDK, real socket): no API key, structured planner JSON."""
    OpenAICompatibleProvider._format_level.clear()
    profile = ollama.ollama_profile("qwen2.5:3b", server)
    provider = OpenAICompatibleProvider(profile, CredentialService(secure=SessionCredentialStore()))
    result = asyncio.run(provider.test_connection())
    assert result.model_available
    req = AIRequest(
        "req-ollama",
        AIMode.ANALYZE,
        "qwen2.5:3b",
        "SYS",
        (ChatMessage("user", "Analyze this board."),),
        planner_wire_schema(),
    )
    resp = asyncio.run(provider.generate(req))
    parsed = parse_planner_response(resp.content)
    assert parsed.response.message == "hello from the local model"
    chat = [r for r in _Ollama.requests if r["path"] == "/v1/chat/completions"]
    assert chat and chat[-1]["auth"] in (None, "Bearer not-required")


def test_setup_dialog_buttons(qtbot: Any, server: str, tmp_path: Path) -> None:
    from pcbrouter.settings import SettingsStore
    from pcbrouter.ui.ollama_dialog import OllamaDialog
    from tests.integration.test_route_jobs import run_until
    from tests.integration.test_ui import make_window

    w = make_window(qtbot, SettingsStore(tmp_path / "config" / "settings.json"))
    dlg = OllamaDialog(w, url=server)
    dlg.show()
    run_until(lambda: dlg.state is not None, 10)
    assert dlg.state.running
    dlg.model.setCurrentText("qwen2.5:7b")
    dlg.use_button.click()
    assert "not downloaded yet" in dlg.output.toPlainText()
    dlg.pull_button.click()
    run_until(lambda: "is ready" in dlg.output.toPlainText(), 10)
    run_until(lambda: "qwen2.5:7b" in (dlg.state.models if dlg.state else []), 10)
    dlg.model.setCurrentText("qwen2.5:7b")
    dlg.use_button.click()
    assert w.settings.ai.default_profile_id == "ollama-local"
    dlg.test_button.click()
    run_until(lambda: "Local AI works" in dlg.output.toPlainText(), 10)
    dlg.reject()
    w.close()
