"""Background jobs for heavy deterministic work (Stage 3 spec §60).

A job is a plain Python callable run on :class:`QThreadPool`. It must not touch
widgets: the result is delivered back on the GUI thread through a queued Qt
signal, where the caller decides whether it is still relevant (e.g. the board may
have been closed meanwhile — see ``GeometryController``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

log = logging.getLogger(__name__)


class _Signals(QObject):
    finished = Signal(str, object, float)  # (job name, result, seconds)
    failed = Signal(str, str, str)  # (job name, message, detail)


class BackgroundJob(QRunnable):
    def __init__(self, name: str, fn: Callable[[], Any]) -> None:
        super().__init__()
        self.name = name
        self.fn = fn
        self.signals = _Signals()
        self.setAutoDelete(False)  # the runner owns it until the result is delivered

    def run(self) -> None:  # worker thread
        t0 = time.perf_counter()
        try:
            result = self.fn()
        except Exception as exc:  # reported to the GUI, never swallowed
            log.exception("job.failed name=%s", self.name)
            self.signals.failed.emit(self.name, f"{self.name} failed: {exc}", repr(exc))
            return
        self.signals.finished.emit(self.name, result, time.perf_counter() - t0)


_Done = Callable[[Any, float], None]
_Error = Callable[[str, str], None]


class JobRunner(QObject):
    """Runs one job per *name* at a time and tracks what is in flight.

    Results arrive through slots of this GUI-thread object, so Qt queues them onto
    the GUI thread (callbacks never run on a worker thread).
    """

    busyChanged = Signal(bool)

    def __init__(self, parent: QObject | None = None, pool: QThreadPool | None = None) -> None:
        super().__init__(parent)
        self.pool = pool or QThreadPool.globalInstance()
        self._running: dict[str, tuple[BackgroundJob, _Done, _Error]] = {}

    def is_running(self, name: str | None = None) -> bool:
        return bool(self._running) if name is None else name in self._running

    def start(self, name: str, fn: Callable[[], Any], on_done: _Done, on_error: _Error) -> bool:
        """Start ``fn`` unless a job with the same name is running. Returns started."""
        if name in self._running:
            return False
        job = BackgroundJob(name, fn)
        job.signals.finished.connect(self._on_finished)
        job.signals.failed.connect(self._on_failed)
        was_busy = bool(self._running)
        self._running[name] = (job, on_done, on_error)
        if not was_busy:
            self.busyChanged.emit(True)
        self.pool.start(job)
        return True

    def wait(self, msecs: int = 30_000) -> bool:
        """Block until all pool jobs finish (tests and shutdown only)."""
        return self.pool.waitForDone(msecs)

    @Slot(str, object, float)
    def _on_finished(self, name: str, result: object, secs: float) -> None:
        entry = self._pop(name)
        if entry is not None:
            entry[1](result, secs)

    @Slot(str, str, str)
    def _on_failed(self, name: str, message: str, detail: str) -> None:
        entry = self._pop(name)
        if entry is not None:
            entry[2](message, detail)

    def _pop(self, name: str) -> tuple[BackgroundJob, _Done, _Error] | None:
        entry = self._running.pop(name, None)
        if not self._running:
            self.busyChanged.emit(False)
        return entry
