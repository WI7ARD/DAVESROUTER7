"""The provider interface every LLM adapter implements.

Security model (see docs/security.md):

1. A provider receives an :class:`~pcbrouter.ai.requests.AIRequest` built from a
   *summarised* board context — never a ``.kicad_pcb`` file.
2. A provider returns *text*. That text is only ever parsed into the strict command
   schema and validated locally. Nothing returned is executed.
3. Credentials are fetched from the credential service at call time; they are never
   part of the profile, never logged, and never shown by ``repr()``.

SDK types (``openai.*``, ``anthropic.*``) must not escape an adapter module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pcbrouter.ai.models import AIModelInfo, ConnectionResult, ProviderCapabilities
from pcbrouter.ai.profiles import ProviderKind, ProviderProfile
from pcbrouter.ai.requests import AIRequest
from pcbrouter.ai.responses import AIResponse

STAGE_UNAVAILABLE_MESSAGE = "Available in a later stage"


class AIProvider(ABC):
    def __init__(self, profile: ProviderProfile) -> None:
        self._profile = profile

    @property
    def profile(self) -> ProviderProfile:
        return self._profile

    @property
    def provider_id(self) -> str:
        return self._profile.profile_id

    @property
    def display_name(self) -> str:
        return self._profile.name

    @property
    def kind(self) -> ProviderKind:
        return self._profile.kind

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...

    @abstractmethod
    async def test_connection(self) -> ConnectionResult:
        """Cheapest possible authenticated call. Sends no board data."""

    @abstractmethod
    async def list_models(self) -> list[AIModelInfo]:
        """Models the endpoint reports. Raises ``AICapabilityError`` if unsupported."""

    @abstractmethod
    async def generate(self, request: AIRequest) -> AIResponse:
        """One request/response round trip. Cancellable via asyncio cancellation."""

    def __repr__(self) -> str:  # never includes credentials
        p = self._profile
        return (
            f"<{type(self).__name__} profile={p.profile_id!r} kind={p.kind.value} "
            f"model={p.model_id!r}>"
        )
