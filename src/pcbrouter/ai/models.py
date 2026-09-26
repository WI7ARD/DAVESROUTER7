"""Provider-neutral descriptions of models, capabilities and connection state.

Rule: never invent facts. Any capability or number a provider did not report is
``None`` (displayed as "unknown"). Pricing is never hard-coded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass(frozen=True, slots=True)
class AIModelInfo:
    provider_kind: str
    model_id: str
    display_name: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_structured_output: bool | None = None
    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    #: USD per million tokens. Always ``None`` unless a reliable source supplies it.
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        if self.display_name and self.display_name != self.model_id:
            return f"{self.display_name} ({self.model_id})"
        return self.model_id


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    model_listing: bool
    native_structured_output: bool  # may still be refused by a particular model
    requires_api_key: bool
    cancellation: bool = True
    custom_base_url: bool = False


class ConnectionStatus(Enum):
    NOT_CONFIGURED = "not_configured"
    CONNECTED = "connected"
    MODEL_UNAVAILABLE = "model_unavailable"
    AUTH_FAILED = "auth_failed"
    UNREACHABLE = "unreachable"
    PACKAGE_MISSING = "package_missing"
    ERROR = "error"
    UNKNOWN = "unknown"  # never tested in this session

    @property
    def label(self) -> str:
        return {
            ConnectionStatus.NOT_CONFIGURED: "Not configured",
            ConnectionStatus.CONNECTED: "Connected",
            ConnectionStatus.MODEL_UNAVAILABLE: "Connected, model unavailable",
            ConnectionStatus.AUTH_FAILED: "Authentication failed",
            ConnectionStatus.UNREACHABLE: "Offline / unreachable",
            ConnectionStatus.PACKAGE_MISSING: "Support package not installed",
            ConnectionStatus.ERROR: "Error",
            ConnectionStatus.UNKNOWN: "Not tested",
        }[self]


@dataclass(frozen=True, slots=True)
class ConnectionResult:
    status: ConnectionStatus
    message: str
    model_available: bool | None = None
    latency_s: float | None = None
    checked_at: float = field(default_factory=time.time)
