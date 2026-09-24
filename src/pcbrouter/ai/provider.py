"""AI provider abstraction — **interfaces only in Stage 1**.

No network code exists in this stage. Future adapters (``OpenAIProvider``,
``AnthropicProvider``, ``OpenAICompatibleProvider``, ``LocalProvider``) will
implement :class:`AIProvider`.

Security model (enforced by design, documented in docs/architecture.md):

1. A provider receives an :class:`AIRequest` built from a *summarised*
   :class:`BoardContext`, never a raw ``.kicad_pcb`` file, unless the user
   explicitly authorises more (``ContextPolicy.FULL_BOARD_WITH_CONSENT``).
2. A provider returns *text*. That text is only ever parsed into the strict
   :mod:`pcbrouter.ai.command_schema` models. Anything that fails validation is
   discarded. Returned text is never executed as code.
3. Provider configuration holds a *reference* to a keyring entry, never the key.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

STAGE_UNAVAILABLE_MESSAGE = "Available in a later stage"


class ProviderKind(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OPENAI_COMPATIBLE = "openai_compatible"  # e.g. vLLM, LM Studio, OpenRouter
    LOCAL = "local"  # e.g. llama.cpp / Ollama on this machine

    @property
    def display_name(self) -> str:
        return {
            ProviderKind.OPENAI: "OpenAI",
            ProviderKind.ANTHROPIC: "Anthropic",
            ProviderKind.OPENAI_COMPATIBLE: "OpenAI-compatible endpoint",
            ProviderKind.LOCAL: "Local model",
        }[self]


class ContextPolicy(StrEnum):
    """How much board information may be sent to a provider."""

    SUMMARY_ONLY = "summary_only"  # default: counts, net names, layer names
    SELECTED_OBJECTS = "selected_objects"  # summary + the user's current selection
    FULL_BOARD_WITH_CONSENT = "full_board_with_consent"  # explicit, per-request consent


class ProviderConfig(BaseModel):
    """Non-secret provider configuration (safe to persist in settings)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ProviderKind
    display_name: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=200)
    base_url: HttpUrl | None = None
    #: Name of the OS keyring entry holding the key. The key itself is never stored.
    credential_ref: str | None = Field(default=None, max_length=200)
    context_policy: ContextPolicy = ContextPolicy.SUMMARY_ONLY
    timeout_s: float = Field(default=60.0, gt=0, le=600)


class BoardContext(BaseModel):
    """Summarised engineering context that *may* be sent to a provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    board_name: str
    copper_layers: list[str]
    net_names: list[str] = Field(default_factory=list)
    component_references: list[str] = Field(default_factory=list)
    selected_objects: list[str] = Field(default_factory=list)
    statistics: dict[str, int] = Field(default_factory=dict)


class AIRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(min_length=1, max_length=20_000)
    context: BoardContext
    policy: ContextPolicy = ContextPolicy.SUMMARY_ONLY


class AIResponse(BaseModel):
    """Raw provider output. Must pass ``parse_command`` before anything acts on it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ProviderKind
    model: str
    raw_text: str
    usage_tokens: int | None = None


class AIProviderError(RuntimeError):
    """A provider call failed (network, auth, quota, malformed response)."""


class AIProvider(ABC):
    """Interface every future LLM adapter implements."""

    @property
    @abstractmethod
    def kind(self) -> ProviderKind: ...

    @property
    @abstractmethod
    def config(self) -> ProviderConfig: ...

    @abstractmethod
    def is_configured(self) -> bool:
        """True when credentials/endpoint are present. Must not make network calls."""

    @abstractmethod
    def complete(self, request: AIRequest) -> AIResponse:
        """Send ``request`` and return raw text. Only called on explicit user action."""
