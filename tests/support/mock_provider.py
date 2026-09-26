"""MockAIProvider for automated tests — no network, no API credits.

Lives under ``tests/`` on purpose so it can never be accidentally active in the
application. It implements the real :class:`AIProvider` interface and records every
request it receives, so tests can assert exactly what would have been sent.
"""

from __future__ import annotations

import asyncio
import json
from enum import StrEnum
from typing import Any

from pcbrouter.ai.exceptions import (
    AIAuthenticationError,
    AIRateLimitError,
    AITimeoutError,
)
from pcbrouter.ai.models import (
    AIModelInfo,
    ConnectionResult,
    ConnectionStatus,
    ProviderCapabilities,
)
from pcbrouter.ai.profiles import ProviderKind, ProviderProfile
from pcbrouter.ai.provider import AIProvider
from pcbrouter.ai.requests import AIRequest
from pcbrouter.ai.responses import AIResponse, FinishStatus, TokenUsage

FAKE_KEY = "sk-test-FAKE-KEY-0000000000000000000000"  # obviously fake, used by tests only


class Scenario(StrEnum):
    ANALYSIS = "analysis"
    COMMAND = "command"
    INVALID_JSON = "invalid_json"
    INVALID_SCHEMA = "invalid_schema"
    UNKNOWN_NET = "unknown_net"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    AUTH_FAILURE = "auth_failure"
    HANG = "hang"  # never answers: used to test cancellation
    CUSTOM = "custom"


ANALYSIS_PAYLOAD: dict[str, Any] = {
    "schema_version": 2,
    "mode": "analyze",
    "message": "CAN_H and CAN_L form the CAN bus; CAN_L has one via, CAN_H has none.",
    "analysis": {
        "summary": "Small CAN node. The CAN pair is partially routed.",
        "observations": ["CAN_L uses a via; CAN_H does not."],
        "potential_issues": ["Potential concern: asymmetric via count on the CAN pair."],
        "recommended_priorities": [
            {"item": "Route CAN_H/CAN_L first", "rationale": "keeps the pair symmetric"}
        ],
        "unknowns": ["No net-class information was provided."],
        "warnings": None,
    },
    "plan_steps": None,
    "commands": None,
    "clarification_needed": None,
    "unsupported_request": None,
}

COMMAND_PAYLOAD: dict[str, Any] = {
    "schema_version": 2,
    "mode": "command",
    "message": "Proposed: route the CAN pair first with minimal vias.",
    "analysis": None,
    "plan_steps": None,
    "commands": [
        {
            "operation": "route_group",
            "targets": [{"type": "net_group", "names": ["CAN_H", "CAN_L"]}],
            "constraints": {
                "preserve_existing_routes": True,
                "allow_component_movement": False,
                "minimize_vias": True,
                "max_vias": 4,
                "avoid_net_classes": ["SWITCHING_POWER"],
                "preferred_layers": ["F.Cu", "B.Cu"],
                "priority": "critical",
                # Nullable-but-required wire fields arrive as null and are stripped.
                "min_clearance_mm": None,
            },
            "reasoning_summary": "Routing the CAN pair first stops later traces constraining it.",
            "warnings": None,
            "confidence": "high",
            "requires_user_confirmation": True,
        }
    ],
    "clarification_needed": None,
    "unsupported_request": None,
}

UNKNOWN_NET_PAYLOAD: dict[str, Any] = {
    **COMMAND_PAYLOAD,
    "commands": [
        {
            "operation": "route_net",
            "targets": [{"type": "net", "name": "CAN_X"}],
            "constraints": None,
            "reasoning_summary": None,
            "warnings": None,
            "confidence": "high",
            "requires_user_confirmation": True,
        }
    ],
}

INVALID_SCHEMA_PAYLOAD: dict[str, Any] = {
    "schema_version": 2,
    "mode": "command",
    "message": "sure",
    "commands": [{"operation": "execute_shell", "command": "rm -rf /"}],
}


def profile(name: str = "Mock", model: str = "mock-model-1") -> ProviderProfile:
    return ProviderProfile(
        profile_id="mock-main", name=name, kind=ProviderKind.OPENAI, model_id=model
    )


class MockAIProvider(AIProvider):
    def __init__(
        self,
        prof: ProviderProfile | None = None,
        scenario: Scenario = Scenario.ANALYSIS,
        *,
        payload: dict[str, Any] | None = None,
        delay_s: float = 0.0,
        usage: tuple[int | None, int | None] = (1200, 180),
        fail_times: int = 0,
    ) -> None:
        super().__init__(prof or profile())
        self.scenario = scenario
        self.payload = payload
        self.delay_s = delay_s
        self.usage = usage
        self.fail_times = fail_times  # transient failures before success (retry tests)
        self.requests: list[AIRequest] = []
        self.calls = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            model_listing=True, native_structured_output=True, requires_api_key=False
        )

    async def test_connection(self) -> ConnectionResult:
        if self.scenario is Scenario.AUTH_FAILURE:
            return ConnectionResult(ConnectionStatus.AUTH_FAILED, "Authentication failed.")
        return ConnectionResult(
            ConnectionStatus.CONNECTED, "Connected (mock). Model available.", True, 0.01
        )

    async def list_models(self) -> list[AIModelInfo]:
        return [
            AIModelInfo(provider_kind="mock", model_id="mock-model-1"),
            AIModelInfo(provider_kind="mock", model_id="mock-model-2", context_window=200_000),
        ]

    async def generate(self, request: AIRequest) -> AIResponse:
        self.requests.append(request)
        self.calls += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.calls <= self.fail_times:
            raise AITimeoutError("mock transient timeout")
        s = self.scenario
        if s is Scenario.TIMEOUT:
            raise AITimeoutError("mock timeout")
        if s is Scenario.RATE_LIMIT:
            raise AIRateLimitError("mock 429", retry_after_s=None)
        if s is Scenario.AUTH_FAILURE:
            raise AIAuthenticationError("mock 401 Incorrect API key provided: sk-live-abcdef123456")
        if s is Scenario.HANG:
            await asyncio.sleep(3600)
        text = {
            Scenario.ANALYSIS: lambda: json.dumps(ANALYSIS_PAYLOAD),
            Scenario.COMMAND: lambda: json.dumps(COMMAND_PAYLOAD),
            Scenario.UNKNOWN_NET: lambda: json.dumps(UNKNOWN_NET_PAYLOAD),
            Scenario.INVALID_SCHEMA: lambda: json.dumps(INVALID_SCHEMA_PAYLOAD),
            Scenario.INVALID_JSON: lambda: "Sure! Here is the plan: {not json",
            Scenario.CUSTOM: lambda: json.dumps(self.payload),
        }[s]()
        return AIResponse(
            request_id=request.request_id,
            provider="mock",
            model=request.model,
            content=text,
            usage=TokenUsage(*self.usage),
            latency_s=0.01,
            finish_status=FinishStatus.COMPLETE,
            used_native_schema=True,
        )
