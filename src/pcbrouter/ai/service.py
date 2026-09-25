"""Application-level AI service (no Qt).

Owns the credential service, provider registry, background runner, usage tracker,
last-known connection states and the AI session for the currently open board. The
GUI talks to this; tests drive it directly.
"""

from __future__ import annotations

import concurrent.futures
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pcbrouter.ai.credentials import CredentialService
from pcbrouter.ai.exceptions import AIProviderError, AIProviderUnavailableError
from pcbrouter.ai.models import AIModelInfo, ConnectionResult, ConnectionStatus
from pcbrouter.ai.profiles import ProviderProfile
from pcbrouter.ai.provider import AIProvider
from pcbrouter.ai.provider_registry import ProviderRegistry
from pcbrouter.ai.responses import AIResponse
from pcbrouter.ai.retry import RetryPolicy
from pcbrouter.ai.runner import AsyncRunner, execute_request
from pcbrouter.ai.session import AIRuntimeConfig, AISession, PreparedRequest
from pcbrouter.ai.usage import UsageRecord, UsageTracker
from pcbrouter.domain.board import Board
from pcbrouter.history.history import HistoryManager
from pcbrouter.rules.overrides import RuleOverrides

if TYPE_CHECKING:
    from pcbrouter.board_engine import BoardEngine

log = logging.getLogger(__name__)


class AIService:
    def __init__(
        self,
        credentials: CredentialService | None = None,
        registry: ProviderRegistry | None = None,
        runner: AsyncRunner | None = None,
    ) -> None:
        self.credentials = credentials or CredentialService()
        self.registry = registry or ProviderRegistry(self.credentials)
        self._runner = runner
        self.usage = UsageTracker()
        self.connection_status: dict[str, ConnectionResult] = {}
        self.model_cache: dict[str, list[AIModelInfo]] = {}
        self.session: AISession | None = None

    @property
    def runner(self) -> AsyncRunner:
        if self._runner is None:  # started lazily: no thread unless AI is actually used
            self._runner = AsyncRunner()
        return self._runner

    # ------------------------------------------------------------------ sessions
    def start_session(
        self,
        board: Board,
        session_id: str,
        config: AIRuntimeConfig,
        history: HistoryManager | None = None,
        engine_provider: Callable[[], BoardEngine | None] | None = None,
        on_constraints_changed: Callable[[RuleOverrides], None] | None = None,
    ) -> AISession:
        self.end_session("another board was opened")
        self.session = AISession(
            board,
            session_id=session_id,
            config=config,
            history=history,
            engine_provider=engine_provider,
            on_constraints_changed=on_constraints_changed,
        )
        log.info("ai.session.start session=%s fingerprint=%s", session_id, board.fingerprint[:16])
        return self.session

    def end_session(self, reason: str = "board closed") -> AISession | None:
        old = self.session
        if old is not None:
            expired = old.expire_all(reason)
            old.close(reason)
            log.info("ai.session.end session=%s expired_proposals=%d", old.session_id, expired)
        self.session = None
        return old

    # ------------------------------------------------------------------ providers
    def provider(self, profile: ProviderProfile) -> AIProvider:
        return self.registry.create(profile)

    def status_of(self, profile: ProviderProfile) -> ConnectionResult:
        if profile.profile_id in self.connection_status:
            return self.connection_status[profile.profile_id]
        if not self.registry.package_available(profile.kind):
            return ConnectionResult(
                ConnectionStatus.PACKAGE_MISSING,
                f"{profile.kind.display_name} support package not installed",
            )
        if profile.requires_api_key and self.credentials.location(profile.credential_ref) is None:
            return ConnectionResult(ConnectionStatus.NOT_CONFIGURED, "No API key saved")
        if not profile.model_id:
            return ConnectionResult(ConnectionStatus.NOT_CONFIGURED, "No model selected")
        return ConnectionResult(ConnectionStatus.UNKNOWN, "Not tested in this session")

    def remember_status(self, profile: ProviderProfile, result: ConnectionResult) -> None:
        self.connection_status[profile.profile_id] = result
        log.info("ai.connection profile=%s status=%s", profile.profile_id, result.status.value)

    def forget_status(self, profile_id: str) -> None:
        self.connection_status.pop(profile_id, None)
        self.model_cache.pop(profile_id, None)

    def test_connection(
        self, profile: ProviderProfile
    ) -> concurrent.futures.Future[ConnectionResult]:
        return self.runner.submit(self.provider(profile).test_connection())

    def list_models(self, profile: ProviderProfile) -> concurrent.futures.Future[list[AIModelInfo]]:
        return self.runner.submit(self.provider(profile).list_models())

    # ------------------------------------------------------------------ requests
    def submit(
        self,
        prepared: PreparedRequest,
        profile: ProviderProfile,
        *,
        status: Callable[[str], None] | None = None,
        max_retries: int = 2,
    ) -> concurrent.futures.Future[AIResponse]:
        provider = self.provider(profile)
        policy = RetryPolicy(max_retries=max_retries)
        log.info(
            "ai.request.start request_id=%s provider=%s model=%s mode=%s context_level=%s "
            "context_chars=%d",
            prepared.request.request_id,
            profile.kind.value,
            prepared.request.model,
            prepared.request.mode.value,
            prepared.context.level.value,
            prepared.context.char_count,
        )
        return self.runner.submit(execute_request(provider, prepared.request, policy, status))

    def record_usage(
        self,
        prepared: PreparedRequest,
        profile: ProviderProfile,
        response: AIResponse | None,
        error: AIProviderError | None = None,
    ) -> None:
        usage = response.usage if response else None
        self.usage.add(
            UsageRecord(
                request_id=prepared.request.request_id,
                provider=profile.kind.value,
                profile_name=profile.name,
                model=response.model if response else prepared.request.model,
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
                latency_s=response.latency_s if response else None,
                succeeded=response is not None,
            )
        )
        if response is not None:
            log.info(
                "ai.request.done request_id=%s latency_s=%.2f input_tokens=%s output_tokens=%s "
                "finish=%s",
                response.request_id,
                response.latency_s,
                usage.input_tokens if usage else None,
                usage.output_tokens if usage else None,
                response.finish_status.value,
            )
            self.remember_status(
                profile,
                ConnectionResult(ConnectionStatus.CONNECTED, "Last request succeeded", True),
            )
        elif error is not None:
            log.warning(
                "ai.request.failed request_id=%s error=%s detail=%s",
                prepared.request.request_id,
                type(error).__name__,
                error,
            )
            status = _status_for_error(error)
            if status is not None:
                self.remember_status(profile, ConnectionResult(status, error.user_message))

    def shutdown(self) -> None:
        self.end_session("application closing")
        if self._runner is not None:
            self._runner.shutdown()


def _status_for_error(error: AIProviderError) -> ConnectionStatus | None:
    from pcbrouter.ai.exceptions import (
        AIAuthenticationError,
        AIConnectionError,
        AIModelNotFoundError,
        AITimeoutError,
    )

    mapping: list[tuple[type[AIProviderError], ConnectionStatus]] = [
        (AIAuthenticationError, ConnectionStatus.AUTH_FAILED),
        (AIModelNotFoundError, ConnectionStatus.MODEL_UNAVAILABLE),
        (AIConnectionError, ConnectionStatus.UNREACHABLE),
        (AITimeoutError, ConnectionStatus.UNREACHABLE),
        (AIProviderUnavailableError, ConnectionStatus.PACKAGE_MISSING),
    ]
    for cls, status in mapping:
        if isinstance(error, cls):
            return status
    return None


def runtime_config_from_settings(ai: Any) -> AIRuntimeConfig:
    """Build the session config from :class:`pcbrouter.settings.settings.AISettings`."""
    from pcbrouter.ai.anonymizer import AnonymizationOptions
    from pcbrouter.ai.context_builder import ContextLimits

    a = ai.anonymization
    return AIRuntimeConfig(
        context_level=ai.context_level,
        limits=ContextLimits(ai.max_context_chars, ai.max_context_nets, ai.max_context_components),
        max_conversation_turns=ai.max_conversation_turns,
        timeout_s=ai.request_timeout_s,
        max_retries=ai.max_retries,
        max_output_tokens=ai.max_output_tokens,
        anonymization=AnonymizationOptions(
            a.net_names, a.component_values, a.references, a.board_filename
        ),
        log_prompts=ai.debug_log_prompts,
        autonomy=getattr(ai, "autonomy_mode", "approval_required"),
    )
