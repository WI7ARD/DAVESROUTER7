"""Bounded retries for transient provider failures.

The SDKs' own retry loops are disabled (``max_retries=0``) so every provider
follows the same, testable policy:

* retry: timeouts, connection failures, 5xx/overloaded, and rate limits **only when
  the provider says how long to wait** (``Retry-After``) and that wait is short;
* never retry: authentication, unknown model, invalid request/schema, cancellation;
* bounded exponential backoff with optional jitter; never infinite.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pcbrouter.ai.exceptions import AIProviderError, AIRateLimitError, AIRequestCancelled

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int = 2
    base_delay_s: float = 1.0
    multiplier: float = 2.0
    max_delay_s: float = 20.0
    #: A rate-limit wait longer than this is reported instead of silently waited out.
    max_rate_limit_wait_s: float = 30.0
    jitter_fraction: float = 0.2

    def __post_init__(self) -> None:
        if not 0 <= self.max_retries <= 10:
            raise ValueError("max_retries must be between 0 and 10")

    def delay_for(
        self, attempt: int, error: AIProviderError, rng: random.Random | None = None
    ) -> float | None:
        """Seconds to wait before retry number ``attempt`` (0-based), or ``None`` to give up."""
        if attempt >= self.max_retries or not error.retryable:
            return None
        if isinstance(error, AIRateLimitError):
            if error.retry_after_s is None or error.retry_after_s > self.max_rate_limit_wait_s:
                return None
            return max(0.0, error.retry_after_s)
        delay = min(self.max_delay_s, self.base_delay_s * self.multiplier**attempt)
        if self.jitter_fraction:
            r = rng or random.Random()
            delay *= 1 + r.uniform(-self.jitter_fraction, self.jitter_fraction)
        return delay


async def call_with_retries[T](
    call: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_retry: Callable[[int, AIProviderError, float], None] | None = None,
) -> T:
    attempt = 0
    while True:
        try:
            return await call()
        except AIRequestCancelled:
            raise
        except AIProviderError as exc:
            delay = policy.delay_for(attempt, exc)
            if delay is None:
                raise
            attempt += 1
            log.warning(
                "ai.retry attempt=%d delay_s=%.1f error=%s", attempt, delay, type(exc).__name__
            )
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            await sleep(delay)
