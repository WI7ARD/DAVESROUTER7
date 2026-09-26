"""Contract tests every provider adapter must satisfy (spec item 48).

The real ``openai``/``anthropic`` SDKs run against an in-process mock HTTP transport,
so request building, response parsing and SDK exception types are all exercised —
with no network access and no API credits.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import anthropic
import httpx2
import openai
import pytest

from pcbrouter.ai._sdk_common import AINotConfiguredError, SDKProvider
from pcbrouter.ai.anthropic_provider import AnthropicProvider
from pcbrouter.ai.credentials import CredentialService, SessionCredentialStore
from pcbrouter.ai.exceptions import (
    AIAuthenticationError,
    AIConnectionError,
    AIModelNotFoundError,
    AIProviderError,
    AIRateLimitError,
    AIServerError,
    AITimeoutError,
)
from pcbrouter.ai.models import ConnectionStatus
from pcbrouter.ai.openai_compatible_provider import OpenAICompatibleProvider
from pcbrouter.ai.openai_provider import OpenAIProvider
from pcbrouter.ai.profiles import ProviderKind, ProviderProfile
from pcbrouter.ai.requests import AIMode, AIRequest, ChatMessage
from pcbrouter.ai.responses import AIResponse, FinishStatus
from pcbrouter.ai.wire_schema import planner_wire_schema
from tests.support.mock_provider import FAKE_KEY

PAYLOAD = json.dumps({"schema_version": 2, "mode": "analyze", "message": "ok"})
Handler = Callable[[httpx2.Request], Any]


def openai_response(text: str = PAYLOAD, status: str = "completed") -> dict[str, Any]:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "status": status,
        "model": "gpt-test",
        "output": [
            {
                "type": "message",
                "id": "m1",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": {
            "input_tokens": 11,
            "output_tokens": 7,
            "total_tokens": 18,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }


def anthropic_response(text: str = PAYLOAD, stop: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }


def chat_response(text: str = PAYLOAD, finish: str = "stop") -> dict[str, Any]:
    return {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": "local-test",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish}
        ],
    }


@dataclass
class Kind:
    name: str
    kind: ProviderKind
    cls: type[SDKProvider]
    endpoint: str  # path suffix of the generation endpoint
    ok_body: Callable[[], dict[str, Any]]
    base_url: str | None = None


KINDS = [
    Kind("openai", ProviderKind.OPENAI, OpenAIProvider, "/responses", openai_response),
    Kind("anthropic", ProviderKind.ANTHROPIC, AnthropicProvider, "/messages", anthropic_response),
    Kind(
        "compatible",
        ProviderKind.OPENAI_COMPATIBLE,
        OpenAICompatibleProvider,
        "/chat/completions",
        chat_response,
        "http://localhost:1234/v1",
    ),
]


class Recorder:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx2.Request] = []

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        result = self.handler(request)
        if asyncio.iscoroutine(result):
            result = await result
        return result  # type: ignore[no-any-return]

    def bodies(self, suffix: str) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith(suffix)]


def make(
    k: Kind, handler: Handler, *, key: str | None = FAKE_KEY, model: str = "model-x"
) -> tuple[SDKProvider, Recorder]:
    rec = Recorder(handler)
    profile = ProviderProfile(
        profile_id=f"{k.name}-t",
        name=f"{k.name} test",
        kind=k.kind,
        model_id=model,
        base_url=k.base_url,
        requires_api_key=k.kind is not ProviderKind.OPENAI_COMPATIBLE,
    )
    creds = CredentialService(secure=SessionCredentialStore())
    if key:
        creds.save(profile.credential_ref, key, session_only=True)

    def factory(api_key: str | None) -> Any:
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(rec))
        if k.kind is ProviderKind.ANTHROPIC:
            return anthropic.AsyncAnthropic(
                api_key=api_key,
                base_url=profile.effective_base_url,
                http_client=http,
                max_retries=0,
            )
        return openai.AsyncOpenAI(
            api_key=api_key or "not-required",
            base_url=profile.effective_base_url,
            http_client=http,
            max_retries=0,
        )

    return k.cls(profile, creds, client_factory=factory), rec


def request(schema: bool = True) -> AIRequest:
    return AIRequest(
        "req-contract",
        AIMode.ANALYZE,
        "model-x",
        "SYSTEM PROMPT",
        (ChatMessage("user", "hello"),),
        planner_wire_schema() if schema else None,
        timeout_s=5.0,
    )


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def status(code: int, message: str = "error", headers: dict[str, str] | None = None) -> Handler:
    body = {"error": {"message": message, "type": "error"}, "type": "error"}
    return lambda _r: httpx2.Response(code, json=body, headers=headers or {})


@pytest.fixture(autouse=True)
def reset_caches() -> None:
    SDKProvider._schema_unsupported.clear()
    OpenAICompatibleProvider._format_level.clear()
    AnthropicProvider._structured_support.clear()


@pytest.mark.parametrize("k", KINDS, ids=lambda k: k.name)
class TestContract:
    def test_returns_normalised_response(self, k: Kind) -> None:
        provider, rec = make(k, lambda r: httpx2.Response(200, json=k.ok_body()))
        resp = run(provider.generate(request()))
        assert isinstance(resp, AIResponse)
        assert resp.request_id == "req-contract" and resp.provider == k.kind.value
        assert json.loads(resp.content)["message"] == "ok"
        assert resp.finish_status is FinishStatus.COMPLETE
        assert resp.latency_s >= 0 and resp.used_native_schema
        body = rec.bodies(k.endpoint)[0]
        assert "temperature" not in body and "top_p" not in body  # no forced sampling
        sent = json.dumps(body)
        assert "SYSTEM PROMPT" in sent and "hello" in sent

    def test_identity_and_repr_never_expose_key(self, k: Kind) -> None:
        provider, _ = make(k, lambda r: httpx2.Response(200, json=k.ok_body()))
        assert provider.provider_id == f"{k.name}-t"
        assert provider.display_name == f"{k.name} test" and provider.kind is k.kind
        assert FAKE_KEY not in repr(provider) and "sk-" not in repr(provider)
        assert FAKE_KEY not in str(vars(provider))

    def test_uses_explicit_endpoint_not_environment(
        self, k: Kind, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_BASE_URL", "https://evil.invalid/v1")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.invalid")
        provider, rec = make(k, lambda r: httpx2.Response(200, json=k.ok_body()))
        run(provider.generate(request()))
        assert all("evil.invalid" not in str(r.url) for r in rec.requests)

    @pytest.mark.parametrize(
        ("handler", "error"),
        [
            (
                status(401, "Incorrect API key provided: sk-live-abcdefghijklmnop1234"),
                AIAuthenticationError,
            ),
            (status(403, "forbidden"), AIAuthenticationError),
            (status(404, "model not found"), AIModelNotFoundError),
            (status(500, "boom"), AIServerError),
            (status(503, "overloaded"), AIServerError),
        ],
        ids=["401", "403", "404", "500", "503"],
    )
    def test_http_errors_are_normalised(
        self, k: Kind, handler: Handler, error: type[AIProviderError]
    ) -> None:
        provider, _ = make(k, handler)
        with pytest.raises(error) as info:
            run(provider.generate(request(schema=False)))
        assert "sk-live-abcdefghijklmnop1234" not in str(info.value)  # redacted
        assert "sk-live-abcdefghijklmnop1234" not in info.value.user_message

    def test_rate_limit_carries_retry_after(self, k: Kind) -> None:
        provider, _ = make(k, status(429, "slow down", {"retry-after": "7"}))
        with pytest.raises(AIRateLimitError) as info:
            run(provider.generate(request()))
        assert info.value.retry_after_s == 7.0 and info.value.retryable

    def test_timeout_and_connection_errors(self, k: Kind) -> None:
        def timeout(r: httpx2.Request) -> Any:
            raise httpx2.ReadTimeout("slow", request=r)

        def refused(r: httpx2.Request) -> Any:
            raise httpx2.ConnectError("refused", request=r)

        with pytest.raises(AITimeoutError):
            run(make(k, timeout)[0].generate(request()))
        with pytest.raises(AIConnectionError):
            run(make(k, refused)[0].generate(request()))

    def test_malformed_http_body_is_a_provider_error(self, k: Kind) -> None:
        from pcbrouter.ai.exceptions import AIInvalidResponseError

        provider, _ = make(k, lambda r: httpx2.Response(200, text="<html>gateway</html>"))
        with pytest.raises(AIProviderError) as info:
            run(provider.generate(request()))
        assert isinstance(info.value, AIInvalidResponseError | AIProviderError)
        assert not type(info.value).__module__.startswith(("openai", "anthropic"))

    def test_cancellation_aborts_promptly(self, k: Kind) -> None:
        async def slow(r: httpx2.Request) -> httpx2.Response:
            await asyncio.sleep(30)
            return httpx2.Response(200, json=k.ok_body())

        provider, _ = make(k, slow)

        async def scenario() -> float:
            task = asyncio.create_task(provider.generate(request()))
            await asyncio.sleep(0.05)
            start = time.perf_counter()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return time.perf_counter() - start

        assert run(scenario()) < 1.0

    def test_connection_test_sends_no_board_data(self, k: Kind) -> None:
        def handler(r: httpx2.Request) -> httpx2.Response:
            if "/models" in r.url.path:
                if r.url.path.endswith("/models"):
                    return httpx2.Response(
                        200,
                        json={
                            "object": "list",
                            "data": [
                                {
                                    "id": "model-x",
                                    "object": "model",
                                    "created": 0,
                                    "owned_by": "x",
                                    "type": "model",
                                    "display_name": "Model X",
                                    "created_at": "2025-01-01T00:00:00Z",
                                }
                            ],
                            "has_more": False,
                            "first_id": "model-x",
                            "last_id": "model-x",
                        },
                    )
                return httpx2.Response(
                    200,
                    json={
                        "id": "model-x",
                        "object": "model",
                        "created": 0,
                        "owned_by": "x",
                        "type": "model",
                        "display_name": "Model X",
                        "created_at": "2025-01-01T00:00:00Z",
                    },
                )
            return httpx2.Response(500)

        provider, rec = make(k, handler)
        result = run(provider.test_connection())
        assert result.status is ConnectionStatus.CONNECTED, result.message
        assert all(r.method == "GET" for r in rec.requests)  # nothing generated or uploaded

    def test_missing_key(self, k: Kind) -> None:
        provider, rec = make(k, lambda r: httpx2.Response(200, json=k.ok_body()), key=None)
        if k.kind is ProviderKind.OPENAI_COMPATIBLE:  # keys are optional for local servers
            assert run(provider.generate(request())).content
            return
        assert run(provider.test_connection()).status is ConnectionStatus.NOT_CONFIGURED
        with pytest.raises(AINotConfiguredError):
            run(provider.generate(request()))
        assert rec.requests == []

    def test_missing_sdk_package(self, k: Kind, monkeypatch: pytest.MonkeyPatch) -> None:
        profile = ProviderProfile(
            profile_id="x-1", name="X", kind=k.kind, model_id="m", base_url=k.base_url
        )
        creds = CredentialService(secure=SessionCredentialStore())
        creds.save(profile.credential_ref, FAKE_KEY, session_only=True)
        monkeypatch.setattr(k.cls, "package_installed", classmethod(lambda cls: False))
        result = run(k.cls(profile, creds).test_connection())
        assert result.status is ConnectionStatus.PACKAGE_MISSING
        assert "support package is not installed" in result.message


# ------------------------------------------------------------------ adapter specifics
def test_openai_uses_responses_api_strict_schema_and_no_storage() -> None:
    provider, rec = make(KINDS[0], lambda r: httpx2.Response(200, json=openai_response()))
    resp = run(provider.generate(request()))
    body = rec.bodies("/responses")[0]
    assert body["store"] is False
    assert body["text"]["format"]["type"] == "json_schema" and body["text"]["format"]["strict"]
    assert body["instructions"] == "SYSTEM PROMPT"
    assert resp.usage is not None and (resp.usage.input_tokens, resp.usage.output_tokens) == (11, 7)
    assert str(rec.requests[0].url).startswith("https://api.openai.com/v1/")


def test_openai_incomplete_response_is_truncated() -> None:
    provider, _ = make(
        KINDS[0],
        lambda r: httpx2.Response(
            200, json=openai_response(text='{"schema_version": 2', status="incomplete")
        ),
    )
    assert run(provider.generate(request())).finish_status is FinishStatus.TRUNCATED


def test_openai_schema_rejection_falls_back_to_prompt_schema() -> None:
    calls: list[dict[str, Any]] = []

    def handler(r: httpx2.Request) -> httpx2.Response:
        body = json.loads(r.content)
        calls.append(body)
        if "text" in body:
            return httpx2.Response(
                400,
                json={
                    "error": {
                        "message": "Invalid schema for response_format 'x': unsupported keyword",
                        "type": "invalid_request_error",
                    }
                },
            )
        return httpx2.Response(200, json=openai_response())

    provider, _ = make(KINDS[0], handler)
    resp = run(provider.generate(request()))
    assert not resp.used_native_schema and len(calls) == 2
    assert "JSON Schema" in calls[1]["instructions"]  # schema moved into the instructions
    run(provider.generate(request()))
    assert "text" not in calls[2]  # remembered for this model: no second failed attempt


def test_anthropic_output_config_and_stop_reasons() -> None:
    provider, rec = make(KINDS[1], lambda r: httpx2.Response(200, json=anthropic_response()))
    run(provider.generate(request()))
    body = rec.bodies("/messages")[0]
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["system"] == "SYSTEM PROMPT" and body["max_tokens"] > 0
    assert rec.requests[0].headers["x-api-key"] == FAKE_KEY  # sent to the provider only
    assert str(rec.requests[0].url).startswith("https://api.anthropic.com/")
    for stop, finish in (("max_tokens", FinishStatus.TRUNCATED), ("refusal", FinishStatus.REFUSED)):
        p, _ = make(KINDS[1], _anthropic_with_stop(stop))
        assert run(p.generate(request())).finish_status is finish


def _anthropic_with_stop(stop: str) -> Handler:
    return lambda _r: httpx2.Response(200, json=anthropic_response(stop=stop))


def test_anthropic_model_listing_reports_real_capabilities_only() -> None:
    model = {
        "type": "model",
        "id": "claude-test",
        "display_name": "Claude Test",
        "created_at": "2025-01-01T00:00:00Z",
        "max_input_tokens": 200000,
        "max_tokens": 64000,
        "capabilities": {"structured_outputs": {"supported": False}},
    }
    listing = {
        "data": [
            model,
            {
                "type": "model",
                "id": "claude-bare",
                "display_name": "Bare",
                "created_at": "2025-01-01T00:00:00Z",
            },
        ],
        "has_more": False,
        "first_id": "claude-test",
        "last_id": "claude-bare",
    }
    provider, rec = make(
        KINDS[1],
        lambda r: (
            httpx2.Response(200, json=listing)
            if r.url.path.endswith("/models")
            else httpx2.Response(200, json=anthropic_response())
        ),
    )
    models = {m.model_id: m for m in run(provider.list_models())}
    assert models["claude-test"].context_window == 200000
    assert models["claude-test"].supports_structured_output is False
    bare = models["claude-bare"]
    assert bare.context_window is None and bare.supports_structured_output is None  # not invented
    assert bare.input_cost_per_mtok is None  # pricing never invented
    req = AIRequest(
        "r", AIMode.ANALYZE, "claude-test", "S", (ChatMessage("user", "x"),), planner_wire_schema()
    )
    run(provider.generate(req))  # model reported no structured outputs -> prompt schema
    assert "output_config" not in rec.bodies("/messages")[-1]


def test_compatible_format_ladder_and_optional_key() -> None:
    formats: list[str] = []

    def handler(r: httpx2.Request) -> httpx2.Response:
        body = json.loads(r.content)
        fmt = body.get("response_format", {}).get("type", "none")
        formats.append(fmt)
        assert "authorization" in r.headers  # placeholder bearer, ignored by local servers
        if fmt == "json_schema":
            return httpx2.Response(400, json={"error": {"message": "json_schema not supported"}})
        return httpx2.Response(200, json=chat_response())

    provider, _ = make(KINDS[2], handler, key=None)
    resp = run(provider.generate(request()))
    assert formats == ["json_schema", "json_object"] and not resp.used_native_schema
    assert resp.usage is None  # the server reported none: not invented
    run(provider.generate(request()))
    assert formats[-1] == "json_object"  # remembered


def test_compatible_without_model_listing_uses_one_token_ping() -> None:
    seen: list[str] = []

    def handler(r: httpx2.Request) -> httpx2.Response:
        seen.append(r.url.path)
        if r.url.path.endswith("/models"):
            return httpx2.Response(404, json={"error": {"message": "not found"}})
        body = json.loads(r.content)
        assert body["max_tokens"] == 1 and body["messages"] == [{"role": "user", "content": "ping"}]
        return httpx2.Response(200, json=chat_response(text="p"))

    provider, _ = make(KINDS[2], handler, key=None)
    result = run(provider.test_connection())
    assert result.status is ConnectionStatus.CONNECTED and "listing" in result.message
    from pcbrouter.ai.exceptions import AICapabilityError

    with pytest.raises(AICapabilityError):
        run(provider.list_models())
