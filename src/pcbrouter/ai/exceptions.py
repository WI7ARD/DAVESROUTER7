"""Normalised AI errors.

Provider adapters translate every SDK/HTTP failure into one of these types, so the
rest of the application never sees ``openai.*`` or ``anthropic.*`` exceptions.

Every error carries:

* ``user_message`` — short, friendly text for dialogs;
* ``str(error)`` — technical detail for logs (already secret-redacted);
* ``retryable`` — whether the retry policy may try again.
"""

from __future__ import annotations

from pcbrouter.app_logging.setup import redact_secrets


class AIProviderError(Exception):
    """Base class for all AI-layer failures."""

    retryable = False
    default_user_message = "The AI request failed."

    def __init__(self, message: str = "", *, user_message: str | None = None) -> None:
        # Provider error bodies can echo parts of a key ("Incorrect API key: sk-ab…").
        clean = redact_secrets(message or self.default_user_message)
        super().__init__(clean)
        self.user_message = redact_secrets(user_message or self.default_user_message)


class AIProviderUnavailableError(AIProviderError):
    """The provider's Python package is not installed, or it is not configured."""

    default_user_message = "This AI provider is not available."


class AIAuthenticationError(AIProviderError):
    default_user_message = "Authentication failed. Check the API key for this provider."


class AIRateLimitError(AIProviderError):
    retryable = True
    default_user_message = "The provider rate-limited the request. Try again shortly."

    def __init__(
        self,
        message: str = "",
        *,
        retry_after_s: float | None = None,
        user_message: str | None = None,
    ) -> None:
        super().__init__(message, user_message=user_message)
        self.retry_after_s = retry_after_s


class AITimeoutError(AIProviderError):
    retryable = True
    default_user_message = "The AI request timed out."


class AIConnectionError(AIProviderError):
    retryable = True
    default_user_message = "Could not reach the AI provider. Check the network or base URL."


class AIServerError(AIProviderError):
    """5xx / overloaded: transient on the provider side."""

    retryable = True
    default_user_message = "The AI provider reported a temporary server error."


class AIModelNotFoundError(AIProviderError):
    default_user_message = "The selected model is not available for this provider/API key."


class AIInvalidRequestError(AIProviderError):
    """The provider rejected the request (400/422) for a reason other than the model."""

    default_user_message = "The provider rejected the request."


class AICapabilityError(AIProviderError):
    """The endpoint does not support a feature (model listing, JSON schema output…)."""

    default_user_message = "The provider does not support this feature."


class AIInvalidResponseError(AIProviderError):
    """The response could not be read (empty, truncated, not JSON…)."""

    default_user_message = "The AI returned a response that could not be read."


class AISchemaValidationError(AIProviderError):
    """The response was JSON but did not match the application's schema."""

    default_user_message = "The AI response did not match the required command schema."

    def __init__(self, errors: list[str], *, raw_excerpt: str = "") -> None:
        super().__init__("; ".join(errors))
        self.errors = [redact_secrets(e) for e in errors]
        self.raw_excerpt = redact_secrets(raw_excerpt[:500])


class AIRequestCancelled(AIProviderError):  # noqa: N818 - name mandated by the spec
    default_user_message = "The request was cancelled."
