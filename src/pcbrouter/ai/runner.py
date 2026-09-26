"""Runs provider coroutines on a private asyncio loop in a background thread.

The GUI thread never blocks on the network. ``submit`` returns a
``concurrent.futures.Future``; cancelling it cancels the underlying asyncio task,
which aborts the HTTP request inside the SDK (true cancellation, not just ignoring
the result). A per-request overall deadline bounds retries as well.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Callable, Coroutine
from typing import Any

from pcbrouter.ai.exceptions import AIProviderError, AIRequestCancelled, AITimeoutError
from pcbrouter.ai.provider import AIProvider
from pcbrouter.ai.requests import AIRequest
from pcbrouter.ai.responses import AIResponse
from pcbrouter.ai.retry import RetryPolicy, call_with_retries

log = logging.getLogger(__name__)

StatusCallback = Callable[[str], None]


class AsyncRunner:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="ai-runner", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit[T](self, coro: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def shutdown(self) -> None:
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)


async def execute_request(
    provider: AIProvider,
    request: AIRequest,
    policy: RetryPolicy,
    status: StatusCallback | None = None,
) -> AIResponse:
    """One logical request: retries for transient errors, bounded overall deadline."""
    deadline = (
        request.timeout_s * (policy.max_retries + 1) + policy.max_delay_s * policy.max_retries
    )

    def on_retry(attempt: int, error: AIProviderError, delay: float) -> None:
        if status:
            status(f"{error.user_message} Retrying in {delay:.0f} s (attempt {attempt + 1})…")

    if status:
        status(f"Sending to {provider.display_name}…")
    try:
        async with asyncio.timeout(deadline):
            return await call_with_retries(
                lambda: provider.generate(request), policy, on_retry=on_retry
            )
    except TimeoutError as exc:
        raise AITimeoutError(f"no response within {deadline:.0f} s") from exc
    except asyncio.CancelledError:
        log.info("ai.request.cancelled request_id=%s", request.request_id)
        raise


def result_or_error(future: concurrent.futures.Future[AIResponse]) -> AIResponse:
    """Unwrap a finished future into a response or a normalised AI error."""
    if future.cancelled():
        raise AIRequestCancelled("request cancelled")
    exc = future.exception()
    if exc is None:
        return future.result()
    if isinstance(exc, AIProviderError):
        raise exc
    if isinstance(exc, asyncio.CancelledError | concurrent.futures.CancelledError):
        raise AIRequestCancelled("request cancelled")
    raise AIProviderError(f"unexpected error: {exc!r}") from exc
