"""Creates provider adapters from profiles; knows which kinds exist.

Factories are injectable so tests (and future plugins) can substitute providers
without touching the UI.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pcbrouter.ai.credentials import CredentialService
from pcbrouter.ai.profiles import DEFAULT_BASE_URLS, ProviderKind, ProviderProfile
from pcbrouter.ai.provider import AIProvider

ProviderFactory = Callable[[ProviderProfile, CredentialService], AIProvider]


@dataclass(frozen=True, slots=True)
class ProviderKindInfo:
    kind: ProviderKind
    package: str
    install_extra: str
    key_optional: bool
    needs_base_url: bool
    default_base_url: str | None
    notes: str


KIND_INFO: dict[ProviderKind, ProviderKindInfo] = {
    ProviderKind.OPENAI: ProviderKindInfo(
        ProviderKind.OPENAI,
        "openai",
        "openai",
        False,
        False,
        DEFAULT_BASE_URLS[ProviderKind.OPENAI],
        "OpenAI Responses API.",
    ),
    ProviderKind.ANTHROPIC: ProviderKindInfo(
        ProviderKind.ANTHROPIC,
        "anthropic",
        "anthropic",
        False,
        False,
        DEFAULT_BASE_URLS[ProviderKind.ANTHROPIC],
        "Anthropic Messages API.",
    ),
    ProviderKind.OPENAI_COMPATIBLE: ProviderKindInfo(
        ProviderKind.OPENAI_COMPATIBLE,
        "openai",
        "openai",
        True,
        True,
        None,
        "Any server speaking the OpenAI Chat Completions protocol (LM Studio, Ollama, vLLM…).",
    ),
}


def _default_factory(kind: ProviderKind) -> ProviderFactory:
    def create(profile: ProviderProfile, credentials: CredentialService) -> AIProvider:
        # Adapter modules import their SDK lazily, so importing them here is cheap and
        # safe even when the SDK is not installed.
        if kind is ProviderKind.OPENAI:
            from pcbrouter.ai.openai_provider import OpenAIProvider

            return OpenAIProvider(profile, credentials)
        if kind is ProviderKind.ANTHROPIC:
            from pcbrouter.ai.anthropic_provider import AnthropicProvider

            return AnthropicProvider(profile, credentials)
        from pcbrouter.ai.openai_compatible_provider import (
            OpenAICompatibleProvider,
        )

        return OpenAICompatibleProvider(profile, credentials)

    return create


class ProviderRegistry:
    def __init__(
        self,
        credentials: CredentialService,
        factories: dict[ProviderKind, ProviderFactory] | None = None,
    ) -> None:
        self.credentials = credentials
        self._factories: dict[ProviderKind, ProviderFactory] = {
            k: _default_factory(k) for k in ProviderKind
        }
        if factories:
            self._factories.update(factories)

    def create(self, profile: ProviderProfile) -> AIProvider:
        return self._factories[profile.kind](profile, self.credentials)

    @staticmethod
    def package_available(kind: ProviderKind) -> bool:
        import importlib.util

        try:
            return importlib.util.find_spec(KIND_INFO[kind].package) is not None
        except (ImportError, ValueError):
            return False
