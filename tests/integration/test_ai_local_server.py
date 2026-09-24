"""OpenAI-compatible adapter against a real local HTTP server (like LM Studio/Ollama).

Uses the adapter's *default* client construction (real SDK, real sockets), so this
is the closest automated stand-in for "Local - RTX Server" without a model.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest

from pcbrouter.ai.command_parser import parse_planner_response
from pcbrouter.ai.credentials import CredentialService, SessionCredentialStore
from pcbrouter.ai.models import ConnectionStatus
from pcbrouter.ai.openai_compatible_provider import OpenAICompatibleProvider
from pcbrouter.ai.profiles import ProviderKind, ProviderProfile
from pcbrouter.ai.requests import AIMode, AIRequest, ChatMessage
from pcbrouter.ai.wire_schema import planner_wire_schema

ANSWER = {
    "schema_version": 2,
    "mode": "analyze",
    "message": "Local model says hi",
    "analysis": None,
    "plan_steps": None,
    "commands": None,
    "clarification_needed": None,
    "unsupported_request": None,
}


class _Handler(BaseHTTPRequestHandler):
    log: ClassVar[list[dict[str, Any]]] = []
    support_schema: ClassVar[bool] = True

    def log_message(self, *args: Any) -> None:  # keep test output quiet
        pass

    def _send(self, code: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.log.append({"path": self.path, "auth": self.headers.get("Authorization")})
        if self.path.endswith("/models"):
            self._send(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": "local-eng-model", "object": "model", "created": 0, "owned_by": "me"}
                    ],
                },
            )
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.log.append(
            {"path": self.path, "body": body, "auth": self.headers.get("Authorization")}
        )
        fmt = body.get("response_format", {}).get("type")
        if fmt == "json_schema" and not self.support_schema:
            self._send(400, {"error": {"message": "response_format json_schema unsupported"}})
            return
        self._send(
            200,
            {
                "id": "c",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "usage": {"prompt_tokens": 50, "completion_tokens": 9, "total_tokens": 59},
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(ANSWER)},
                    }
                ],
            },
        )


@pytest.fixture
def server() -> Iterator[str]:
    _Handler.log = []
    _Handler.support_schema = True
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    httpd.shutdown()
    httpd.server_close()


def provider(base_url: str, model: str = "local-eng-model") -> OpenAICompatibleProvider:
    OpenAICompatibleProvider._format_level.clear()
    profile = ProviderProfile(
        profile_id="local-rtx",
        name="Local - RTX Server",
        kind=ProviderKind.OPENAI_COMPATIBLE,
        base_url=base_url,
        model_id=model,
        requires_api_key=False,
        timeout_s=10,
    )
    return OpenAICompatibleProvider(profile, CredentialService(secure=SessionCredentialStore()))


def test_connection_models_and_generation_over_real_http(server: str) -> None:
    p = provider(server)
    result = asyncio.run(p.test_connection())
    assert result.status is ConnectionStatus.CONNECTED and result.model_available
    assert [m.model_id for m in asyncio.run(p.list_models())] == ["local-eng-model"]
    req = AIRequest(
        "req-local",
        AIMode.ANALYZE,
        "local-eng-model",
        "SYS",
        (ChatMessage("user", "Analyze this board."),),
        planner_wire_schema(),
    )
    resp = asyncio.run(p.generate(req))
    assert parse_planner_response(resp.content).response.message == "Local model says hi"
    assert resp.usage is not None and resp.usage.input_tokens == 50
    post = next(e for e in _Handler.log if "body" in e)
    assert post["body"]["response_format"]["type"] == "json_schema"
    assert post["body"]["messages"][0] == {"role": "system", "content": "SYS"}
    assert post["auth"] == "Bearer not-required"  # optional key: harmless placeholder


def test_unknown_model_and_schema_fallback(server: str) -> None:
    assert (
        asyncio.run(provider(server, "missing").test_connection()).status
        is ConnectionStatus.MODEL_UNAVAILABLE
    )
    _Handler.support_schema = False
    p = provider(server)
    req = AIRequest(
        "r2",
        AIMode.ANALYZE,
        "local-eng-model",
        "SYS",
        (ChatMessage("user", "hi"),),
        planner_wire_schema(),
    )
    resp = asyncio.run(p.generate(req))
    assert not resp.used_native_schema
    formats = [
        e["body"].get("response_format", {}).get("type") for e in _Handler.log if "body" in e
    ]
    assert formats == ["json_schema", "json_object"]


def test_offline_server_is_reported_unreachable() -> None:
    p = provider("http://127.0.0.1:9/v1")  # discard port: nothing listens
    result = asyncio.run(p.test_connection())
    assert result.status is ConnectionStatus.UNREACHABLE
