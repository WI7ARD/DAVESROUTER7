"""Shared plumbing for SDK-based adapters (OpenAI, Anthropic, OpenAI-compatible).

Error mapping works on class names and HTTP status codes, so this module never
imports an SDK. Both official SDKs expose the same exception taxonomy
(``AuthenticationError``, ``RateLimitError``, ``APITimeoutError``…).
"""

from __future__ import annotations

import asyncio
import contextlib
import email.utils
import importlib.util
import logging
import time
from abc import abstractmethod
from collections.abc import Callable
from typing import Any, ClassVar

from pcbrouter.ai.credentials import CredentialService
from pcbrouter.ai.exceptions import (
    AIAuthenticationError,
    AICapabilityError,
    AIConnectionError,
    AIInvalidRequestError,
    AIInvalidResponseError,
    AIModelNotFoundError,
    AIProviderError,
    AIProviderUnavailableError,
    AIRateLimitError,
    AIRequestCancelled,
    AIServerError,
    AITimeoutError,
)
from pcbrouter.ai.models import AIModelInfo, ConnectionResult, ConnectionStatus
from pcbrouter.ai.profiles import ProviderProfile
from pcbrouter.ai.prompt_builder import schema_instruction
from pcbrouter.ai.provider import AIProvider
from pcbrouter.ai.requests import AIRequest
from pcbrouter.ai.responses import AIResponse

log = logging.getLogger(__name__)

ClientFactory = Callable[[str | None], Any]


class AINotConfiguredError(AIProviderUnavailableError):
    default_user_message = "No API key is saved for this provider."


class AIStructuredOutputUnsupportedError(AICapabilityError):
    default_user_message = "The model rejected native structured output."


_SCHEMA_HINTS = (
    "response_format",
    "json_schema",
    "output_config",
    "text.format",
    "structured",
    "output_format",
    "format",
)
_MODEL_HINTS = ("model",)
_MODEL_MISSING = (
    "not found",
    "does not exist",
    "invalid model",
    "unknown model",
    "not available",
    "no such model",
    "not supported",
)


def _retry_after(exc: Any) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        if (ms := headers.get("retry-after-ms")) is not None:
            return float(ms) / 1000.0
        value = headers.get("retry-after")
        if value is None:
            return None
        try:
            return float(value)
        except ValueError:
            parsed = email.utils.parsedate_to_datetime(value)
            return max(0.0, parsed.timestamp() - time.time())
    except (ValueError, TypeError):
        return None


def map_sdk_error(exc: BaseException, provider: str) -> AIProviderError:
    """Translate any SDK/HTTP exception into the application's error model."""
    if isinstance(exc, AIProviderError):
        return exc
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    message = str(getattr(exc, "message", "") or exc)
    lowered = message.lower()
    detail = f"{provider}: {name}" + (f" (HTTP {status})" if status else "") + f": {message}"
    if name == "APITimeoutError" or status == 408:
        return AITimeoutError(detail)
    if name == "APIConnectionError":
        return AIConnectionError(detail)
    if name == "APIResponseValidationError":
        return AIInvalidResponseError(detail)
    if status in (401, 403) or name in ("AuthenticationError", "PermissionDeniedError"):
        return AIAuthenticationError(detail)
    if status == 404:
        return AIModelNotFoundError(detail)
    if status == 429:
        return AIRateLimitError(detail, retry_after_s=_retry_after(exc))
    if isinstance(status, int) and status >= 500:
        return AIServerError(detail)
    if status in (400, 413, 422):
        if any(h in lowered for h in _MODEL_HINTS) and any(m in lowered for m in _MODEL_MISSING):
            return AIModelNotFoundError(detail)
        if any(h in lowered for h in _SCHEMA_HINTS):
            return AIStructuredOutputUnsupportedError(detail)
        return AIInvalidRequestError(
            detail, user_message=f"The provider rejected the request: " f"{message[:200]}"
        )
    return AIProviderError(detail)


class SDKProvider(AIProvider):
    """Common behaviour: credential lookup, lazy SDK import, schema fallback, timing."""

    PACKAGE: str = ""
    INSTALL_EXTRA: str = "ai"

    #: (base_url, model) pairs for which native structured output was refused.
    _schema_unsupported: ClassVar[set[tuple[str, str]]] = set()

    def __init__(
        self,
        profile: ProviderProfile,
        credentials: CredentialService,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        super().__init__(profile)
        self._credentials = credentials
        self._client_factory = client_factory

    # ------------------------------------------------------------------ plumbing
    @classmethod
    def package_installed(cls) -> bool:
        try:
            return importlib.util.find_spec(cls.PACKAGE) is not None
        except (ImportError, ValueError):
            return False

    def _api_key(self) -> str | None:
        secret = self._credentials.get(self.profile.credential_ref)
        if secret is None:
            if self.profile.requires_api_key:
                raise AINotConfiguredError(f"no API key stored for profile {self.provider_id}")
            return None
        return secret.get_secret_value()

    def _client(self) -> Any:
        key = self._api_key()
        if self._client_factory is not None:
            return self._client_factory(key)
        if not self.package_installed():
            raise AIProviderUnavailableError(
                f"python package {self.PACKAGE!r} is not installed",
                user_message=f"{self.kind.display_name} support package is not installed. "
                f'Install it with: pip install "ai-pcb-router[{self.INSTALL_EXTRA}]"',
            )
        return self._default_client(key)

    @abstractmethod
    def _default_client(self, api_key: str | None) -> Any: ...

    @abstractmethod
    async def _generate_once(
        self, client: Any, request: AIRequest, native_schema: bool
    ) -> AIResponse: ...

    @abstractmethod
    async def _list_models(self, client: Any) -> list[AIModelInfo]: ...

    @abstractmethod
    async def _probe(self, client: Any) -> ConnectionResult: ...

    async def _run[T](self, fn: Callable[[Any], Any]) -> T:
        client = self._client()
        try:
            result: T = await fn(client)
            return result
        except asyncio.CancelledError:
            raise
        except AIProviderError:
            raise
        except Exception as exc:
            raise map_sdk_error(exc, self.kind.display_name) from exc
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                with contextlib.suppress(Exception):  # closing must never mask the outcome
                    await close()

    # ------------------------------------------------------------------ interface
    async def list_models(self) -> list[AIModelInfo]:
        models: list[AIModelInfo] = await self._run(self._list_models)
        return sorted(models, key=lambda m: m.model_id)

    async def test_connection(self) -> ConnectionResult:
        start = time.perf_counter()
        try:
            result: ConnectionResult = await self._run(self._probe)
        except AINotConfiguredError as exc:
            return ConnectionResult(ConnectionStatus.NOT_CONFIGURED, exc.user_message)
        except AIProviderUnavailableError as exc:
            return ConnectionResult(ConnectionStatus.PACKAGE_MISSING, exc.user_message)
        except AIAuthenticationError as exc:
            return ConnectionResult(ConnectionStatus.AUTH_FAILED, exc.user_message)
        except (AIConnectionError, AITimeoutError) as exc:
            return ConnectionResult(ConnectionStatus.UNREACHABLE, exc.user_message)
        except AIModelNotFoundError:
            return ConnectionResult(
                ConnectionStatus.MODEL_UNAVAILABLE,
                f'Connected, but model "{self.profile.model_id}" is unavailable.',
                model_available=False,
            )
        except AIProviderError as exc:
            return ConnectionResult(ConnectionStatus.ERROR, exc.user_message)
        return ConnectionResult(
            result.status, result.message, result.model_available, time.perf_counter() - start
        )

    async def generate(self, request: AIRequest) -> AIResponse:
        if not request.model:
            raise AIModelNotFoundError(
                "no model selected", user_message="Select or enter a model ID first."
            )
        key = (self.profile.effective_base_url, request.model)
        native = (
            request.response_schema is not None
            and self.capabilities().native_structured_output
            and key not in self._schema_unsupported
        )
        try:
            response: AIResponse = await self._run(
                lambda client: self._generate_once(client, request, native)
            )
            return response
        except AIStructuredOutputUnsupportedError as exc:
            if not native:
                raise
            log.warning(
                "ai.schema_fallback provider=%s model=%s reason=%s",
                self.kind.value,
                request.model,
                exc,
            )
            self._schema_unsupported.add(key)
            fallback: AIResponse = await self._run(
                lambda client: self._generate_once(client, request, False)
            )
            return fallback

    @staticmethod
    def system_text(request: AIRequest, native_schema: bool) -> str:
        """System prompt, plus the schema itself when it cannot be enforced natively."""
        if request.response_schema is None or native_schema:
            return request.system_prompt
        return f"{request.system_prompt}\n{schema_instruction()}"


def cancelled_error() -> AIRequestCancelled:
    return AIRequestCancelled("request cancelled by the user")
