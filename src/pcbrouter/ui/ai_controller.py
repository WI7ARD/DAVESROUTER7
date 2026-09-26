"""Qt bridge between the AI service (background asyncio loop) and the widgets.

* Network calls never run on the GUI thread; futures complete on the runner thread
  and results are delivered back through a queued Qt signal.
* One engineering request at a time (double-clicks cannot submit twice).
* Cancel really cancels the asyncio task (aborting the HTTP request). The UI is
  released immediately, the conversation is left untouched, and a late result for
  a request that is no longer active is ignored — so no duplicate message appears.
"""

from __future__ import annotations

import concurrent.futures
import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Signal

from pcbrouter.ai.exceptions import AIProviderError, AIRequestCancelled
from pcbrouter.ai.models import AIModelInfo, ConnectionResult, ConnectionStatus
from pcbrouter.ai.profiles import ProviderProfile
from pcbrouter.ai.requests import AIMode
from pcbrouter.ai.runner import result_or_error
from pcbrouter.ai.service import AIService
from pcbrouter.ai.session import AISession, Interaction, PreparedRequest

log = logging.getLogger(__name__)


class _Bridge(QObject):
    done = Signal(object)  # (callback, future) delivered on the GUI thread
    status = Signal(str, str)  # (request_id, text)


class AIRequestController(QObject):
    statusChanged = Signal(str)
    busyChanged = Signal(bool)
    interactionFinished = Signal(object)  # Interaction
    connectionTested = Signal(str, object)  # (profile_id, ConnectionResult)
    modelsListed = Signal(str, object, object)  # (profile_id, list[AIModelInfo] | None, error)

    def __init__(self, service: AIService, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self._bridge = _Bridge()
        self._bridge.done.connect(self._deliver)
        self._bridge.status.connect(self._on_status)
        self._active: (
            tuple[PreparedRequest, ProviderProfile, AISession, concurrent.futures.Future[Any]]
            | None
        ) = None

    @property
    def busy(self) -> bool:
        return self._active is not None

    @property
    def active_request_id(self) -> str | None:
        return self._active[0].request.request_id if self._active else None

    # ------------------------------------------------------------------ plumbing
    def _watch(
        self,
        future: concurrent.futures.Future[Any],
        callback: Callable[[concurrent.futures.Future[Any]], None],
    ) -> None:
        future.add_done_callback(lambda f: self._bridge.done.emit((callback, f)))

    def _deliver(self, payload: object) -> None:
        assert isinstance(payload, tuple) and len(payload) == 2
        callback: Callable[[concurrent.futures.Future[Any]], None] = payload[0]
        callback(payload[1])

    def _on_status(self, request_id: str, text: str) -> None:
        if request_id == self.active_request_id:
            self.statusChanged.emit(text)

    # ------------------------------------------------------------------ requests
    def send(
        self,
        prompt: str,
        mode: AIMode,
        profile: ProviderProfile,
        *,
        model: str,
        selected_nets: tuple[str, ...] = (),
        selected_components: tuple[str, ...] = (),
        prepared: PreparedRequest | None = None,
    ) -> PreparedRequest:
        """Submit one request. Raises ``RuntimeError`` if one is already running."""
        if self._active is not None:
            raise RuntimeError("an AI request is already running")
        session = self.service.session
        if session is None:
            raise RuntimeError("no board is open")
        self.statusChanged.emit("Building board context…")
        if prepared is None:
            prepared = session.prepare(
                prompt,
                mode,
                model=model,
                provider_name=profile.name,
                selected_nets=selected_nets,
                selected_components=selected_components,
            )
        rid = prepared.request.request_id
        future = self.service.submit(
            prepared,
            profile,
            status=lambda text: self._bridge.status.emit(rid, text),
            max_retries=session.config.max_retries,
        )
        self._active = (prepared, profile, session, future)
        self.busyChanged.emit(True)
        self.statusChanged.emit(f"Sending to {profile.name}… waiting for response")
        self._watch(future, lambda f: self._finished(rid, f))
        return prepared

    def cancel(self) -> bool:
        if self._active is None:
            return False
        prepared, profile, session, future = self._active
        self._active = None
        future.cancel()
        error = AIRequestCancelled("cancelled by the user")
        inter = session.record_failure(prepared, error)
        self.service.record_usage(prepared, profile, None, None)
        log.info("ai.request.cancelled_by_user request_id=%s", prepared.request.request_id)
        self.busyChanged.emit(False)
        self.statusChanged.emit("Cancelled.")
        self.interactionFinished.emit(inter)
        return True

    def _finished(self, request_id: str, future: concurrent.futures.Future[Any]) -> None:
        if self._active is None or self._active[0].request.request_id != request_id:
            log.info("ai.request.late_result_ignored request_id=%s", request_id)
            return
        prepared, profile, session, _ = self._active
        self._active = None
        try:
            response = result_or_error(future)
        except AIProviderError as exc:
            self.service.record_usage(prepared, profile, None, exc)
            inter = session.record_failure(prepared, exc)
            self.statusChanged.emit(f"Failed: {exc.user_message}")
        else:
            self.statusChanged.emit("Validating…")
            self.service.record_usage(prepared, profile, response)
            inter = session.accept_response(prepared, response)
            self.statusChanged.emit(
                "Complete."
                if inter.kind and inter.kind.value != "error"
                else "Complete (with errors)."
            )
        self.busyChanged.emit(False)
        self.interactionFinished.emit(inter)

    # ------------------------------------------------------------------ provider ops
    def test_connection(self, profile: ProviderProfile) -> None:
        def done(f: concurrent.futures.Future[Any]) -> None:
            try:
                result: ConnectionResult = f.result()
            except Exception as exc:  # defensive: test_connection normally never raises
                result = ConnectionResult(ConnectionStatus.ERROR, str(exc))
            self.service.remember_status(profile, result)
            self.connectionTested.emit(profile.profile_id, result)

        self._watch(self.service.test_connection(profile), done)

    def list_models(self, profile: ProviderProfile) -> None:
        def done(f: concurrent.futures.Future[Any]) -> None:
            try:
                models: list[AIModelInfo] = f.result()
            except AIProviderError as exc:
                self.modelsListed.emit(profile.profile_id, None, exc.user_message)
                return
            except Exception as exc:
                self.modelsListed.emit(profile.profile_id, None, str(exc))
                return
            self.service.model_cache[profile.profile_id] = models
            self.modelsListed.emit(profile.profile_id, models, None)

        self._watch(self.service.list_models(profile), done)


def interaction_is_error(inter: Interaction) -> bool:
    return inter.kind is not None and inter.kind.value in ("error", "cancelled", "stale")
