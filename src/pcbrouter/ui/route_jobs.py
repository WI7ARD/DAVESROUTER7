"""GUI side of the routing worker: submit jobs, receive progress, cancel.

Routing (board preparation, grids, planning, A*/wavefront searches, rip-up,
optimisation, validation, export and KiCad DRC) runs in a separate **process**
(:mod:`pcbrouter.jobs.worker`). The Qt thread never routes and never waits: a
``QTimer`` drains the worker's queue with non-blocking reads, delivers at most
one (coalesced) progress update per tick, and runs watchdogs:

* worker crash (process exited)          → FAILED, with the exit code
* job timeout (``job.timeout_s``)         → cancel → TIMED_OUT
* cancel not honoured within the grace    → the worker process is stopped
* no heartbeat for ``HEARTBEAT_TIMEOUT_S`` → worker stopped, FAILED

Stopping the worker is always safe: jobs never touch the GUI's working board or
the source PCB (results are applied by the GUI after they arrive; exports are
atomic). A fresh worker is started for the next job.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import multiprocessing as mp
import pickle
import queue
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from pcbrouter.jobs.protocol import (
    Heartbeat,
    JobBase,
    JobDone,
    JobStatus,
    LogBatch,
    RouteProgress,
    WorkerReady,
    install_pickling,
)

log = logging.getLogger(__name__)
worker_log = logging.getLogger("pcbrouter.worker")

POLL_MS = 33  # ~30 Hz: drain the queue; progress reaches widgets at most this often
IDLE_POLL_MS = 250
POLL_BUDGET_S = 0.012  # never spend more than ~12 ms of a GUI tick on messages
HEARTBEAT_TIMEOUT_S = 20.0
CANCEL_GRACE_S = 5.0
START_TIMEOUT_S = 90.0


class JobState(StrEnum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    CANCELING = "CANCELING"


class WorkerHost:
    """The worker process and its queues (no Qt). ``spawn`` everywhere: identical
    behaviour on Windows, Linux and in the frozen application."""

    _shared: WorkerHost | None = None

    def __init__(self) -> None:
        install_pickling()
        ctx = mp.get_context("spawn")
        self.inbox: Any = ctx.Queue()
        self.outbox: Any = ctx.Queue()
        self.cancel_job: Any = ctx.Value("q", 0)
        self.paused: Any = ctx.Value("b", 0)
        from pcbrouter.jobs.worker import worker_main

        self.process: Any = ctx.Process(
            target=worker_main,
            args=(self.inbox, self.outbox, self.cancel_job, self.paused),
            name="pcbrouter-route-worker",
            daemon=True,
        )
        self.ready = False
        self.pid: int | None = None
        self.started_at = time.monotonic()
        self.process.start()
        log.info("route_worker.spawned pid=%s", self.process.pid)

    @classmethod
    def shared(cls) -> WorkerHost:
        """One worker per application process (reused across windows/jobs)."""
        if cls._shared is None or not cls._shared.alive():
            cls._shared = cls()
        return cls._shared

    @classmethod
    def discard_shared(cls, host: WorkerHost) -> None:
        if cls._shared is host:
            cls._shared = None

    def alive(self) -> bool:
        return bool(self.process.is_alive())

    @property
    def exitcode(self) -> int | None:
        code = self.process.exitcode
        return None if code is None else int(code)

    def submit(self, job_id: int, job: JobBase) -> None:
        self.inbox.put((job_id, job))  # pickled by the queue's feeder thread

    def poll(self, budget_s: float = POLL_BUDGET_S, max_items: int = 500) -> list[Any]:
        """Non-blocking: returns whatever is already in the pipe (bounded)."""
        out: list[Any] = []
        end = time.monotonic() + budget_s
        while len(out) < max_items and time.monotonic() < end:
            try:
                raw = self.outbox.get_nowait()
            except (queue.Empty, OSError, EOFError, ValueError):
                break
            try:
                out.append(pickle.loads(raw))
            except Exception as exc:  # a corrupt message is reported, not fatal
                log.error("route_worker.bad_message error=%r", exc)
        return out

    def stop(self) -> None:
        """Ask the worker to exit after its current job (non-blocking)."""
        with contextlib.suppress(OSError, ValueError):
            self.inbox.put(None)

    def kill(self) -> None:
        """Stop the process now (it holds no state the GUI needs)."""
        try:
            if self.process.is_alive():
                self.process.terminate()
        except (OSError, ValueError, AttributeError):
            pass
        for q in (self.inbox, self.outbox):
            try:
                q.cancel_join_thread()  # never block application exit on the pipe
                q.close()
            except (OSError, ValueError, AttributeError):
                pass
        WorkerHost.discard_shared(self)
        log.info("route_worker.stopped pid=%s", self.process.pid)


@dataclass
class _Active:
    job_id: int
    job: JobBase
    on_done: Callable[[JobDone], None]
    t0: float
    last_heartbeat: float
    last_progress_at: float
    progress: RouteProgress | None = None
    cancel_at: float | None = None
    cancel_reason: str = ""
    log_tail: list[str] = field(default_factory=list)


class RouteJobController(QObject):
    """Submit one job at a time; everything comes back through signals."""

    stateChanged = Signal(str)
    progress = Signal(object)  # RouteProgress (coalesced, ≤ ~30/s, typically ≤ 10/s)
    heartbeat = Signal(object)  # Heartbeat
    jobStarted = Signal(object)  # the JobBase
    finished = Signal(object)  # JobDone (after the job's own callback ran)

    _ids = itertools.count(1)

    def __init__(
        self, parent: QObject | None = None, host_factory: Callable[[], WorkerHost] | None = None
    ) -> None:
        super().__init__(parent)
        self._host_factory = host_factory or WorkerHost.shared
        self._host: WorkerHost | None = None
        self.state = JobState.IDLE
        self.active: _Active | None = None
        self.last_done: JobDone | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)

    # -------------------------------------------------------------- queries
    @property
    def busy(self) -> bool:
        return self.active is not None

    @property
    def elapsed_s(self) -> float:
        return 0.0 if self.active is None else time.monotonic() - self.active.t0

    @property
    def seconds_since_progress(self) -> float | None:
        if self.active is None:
            return None
        return time.monotonic() - self.active.last_progress_at

    @property
    def worker_pid(self) -> int | None:
        return None if self._host is None else self._host.process.pid

    # -------------------------------------------------------------- control
    def prewarm(self) -> None:
        """Start the worker ahead of the first job (imports take a moment)."""
        self._ensure_host()

    def restart_worker(self) -> None:
        """Stop the worker and start a new one (picks up a changed environment)."""
        if self.active is not None:
            return
        for host in (self._host, WorkerHost._shared):
            if host is not None:
                host.kill()
        self._host = None
        self._ensure_host()

    def submit(self, job: JobBase, on_done: Callable[[JobDone], None]) -> int | None:
        """Queue ``job``; returns its id, or None when another job is running."""
        if self.active is not None:
            log.info("route_job.refused reason=busy kind=%s", job.kind)
            return None
        host = self._ensure_host()
        job_id = next(self._ids)
        now = time.monotonic()
        self.active = _Active(job_id, job, on_done, now, now, now)
        host.paused.value = 0
        host.submit(job_id, job)
        log.info(
            "[route:%d] submitted %s mode=%s timeout=%s", job_id, job.kind, job.mode, job.timeout_s
        )
        self._set_state(JobState.STARTING)
        self.jobStarted.emit(job)
        self._timer.setInterval(POLL_MS)
        self._timer.start()
        return job_id

    def cancel(self, reason: str = "user") -> bool:
        """Request cooperative cancellation (idempotent while canceling)."""
        a = self.active
        if a is None or self.state is JobState.CANCELING or self._host is None:
            return False
        a.cancel_at = time.monotonic()
        a.cancel_reason = reason
        self._host.cancel_job.value = a.job_id
        log.info("[route:%d] cancel requested (%s)", a.job_id, reason)
        self._set_state(JobState.CANCELING)
        return True

    def pause(self) -> None:
        if self._host is not None and self.active is not None:
            self._host.paused.value = 1

    def resume(self) -> None:
        if self._host is not None:
            self._host.paused.value = 0

    def shutdown(self) -> None:
        """Window closing: stop a running job immediately (nothing is applied)."""
        self._timer.stop()
        if self.active is not None and self._host is not None:
            log.info("[route:%d] window closing: stopping the worker", self.active.job_id)
            self._host.kill()
            self._host = None
            self.active = None
            self._set_state(JobState.IDLE)

    # -------------------------------------------------------------- internals
    def _ensure_host(self) -> WorkerHost:
        if self._host is None or not self._host.alive():
            self._host = self._host_factory()
            self._timer.start()
        return self._host

    def _set_state(self, state: JobState) -> None:
        if state is not self.state:
            self.state = state
            self.stateChanged.emit(state.value)

    def _poll(self) -> None:
        host = self._host
        if host is None:
            self._timer.stop()
            return
        latest: RouteProgress | None = None
        a = self.active
        now = time.monotonic()
        for msg in host.poll():
            if isinstance(msg, RouteProgress):
                if a is not None and msg.job_id == a.job_id:
                    latest = msg
            elif isinstance(msg, Heartbeat):
                if a is not None:
                    a.last_heartbeat = now
                self.heartbeat.emit(msg)
            elif isinstance(msg, LogBatch):
                self._forward_logs(msg)
            elif isinstance(msg, WorkerReady):
                host.ready, host.pid = True, msg.pid
                log.info(
                    "route_worker.ready pid=%d python=%s after %.1fs",
                    msg.pid,
                    msg.python,
                    now - host.started_at,
                )
                if a is not None:
                    a.last_heartbeat = now
            elif isinstance(msg, JobDone) and a is not None and msg.job_id == a.job_id:
                self._finish(msg)
                a = None
        if latest is not None and self.active is not None:
            self.active.progress = latest
            self.active.last_progress_at = now
            if self.state is JobState.STARTING:
                self._set_state(JobState.RUNNING)
            self.progress.emit(latest)
        self._watchdog(host, now)
        if self.active is None:
            self._timer.setInterval(IDLE_POLL_MS)  # keep draining worker logs slowly

    def _watchdog(self, host: WorkerHost, now: float) -> None:
        a = self.active
        if a is None:
            if not host.alive():
                self._host = None
            return
        if not host.alive():
            code = host.exitcode
            self._host = None
            WorkerHost.discard_shared(host)
            tail = "\n".join(a.log_tail[-20:])
            self._finish(
                JobDone(
                    a.job_id,
                    JobStatus.FAILED.value,
                    error=f"The routing worker process stopped unexpectedly (exit code {code}). "
                    "Nothing was changed on the board.",
                    traceback=f"Last worker log lines:\n{tail}" if tail else "",
                    elapsed_s=now - a.t0,
                )
            )
            return
        if a.cancel_at is None and a.job.timeout_s is not None and now - a.t0 > a.job.timeout_s:
            self.cancel("timeout")
            return
        if a.cancel_at is not None and now - a.cancel_at > CANCEL_GRACE_S:
            self._stop_worker(
                host,
                JobStatus.TIMED_OUT if a.cancel_reason == "timeout" else JobStatus.CANCELED,
                f"the worker did not stop within {CANCEL_GRACE_S:.0f} s and was terminated",
            )
            return
        if not host.ready and now - host.started_at > START_TIMEOUT_S:
            self._stop_worker(host, JobStatus.FAILED, "the routing worker did not start")
            return
        if host.ready and now - a.last_heartbeat > HEARTBEAT_TIMEOUT_S:
            self._stop_worker(
                host,
                JobStatus.FAILED,
                f"the routing worker stopped responding (no heartbeat for "
                f"{HEARTBEAT_TIMEOUT_S:.0f} s) and was terminated",
            )

    def _stop_worker(self, host: WorkerHost, status: JobStatus, why: str) -> None:
        a = self.active
        host.kill()
        self._host = None
        if a is None:
            return
        log.warning("[route:%d] %s", a.job_id, why)
        self._finish(
            JobDone(
                a.job_id,
                status.value,
                error=why[0].upper() + why[1:] + ". Nothing was changed on the board.",
                elapsed_s=time.monotonic() - a.t0,
            )
        )

    def _forward_logs(self, batch: LogBatch) -> None:
        prefix = "[worker] " if batch.job_id is None else ""
        for line in batch.lines:
            worker_log.info("%s%s", prefix, line)
        if batch.suppressed:
            worker_log.info("… %d worker log line(s) suppressed (rate limit)", batch.suppressed)
        if self.active is not None:
            self.active.log_tail = (self.active.log_tail + batch.lines)[-50:]

    def _finish(self, done: JobDone) -> None:
        a = self.active
        if a is None:
            return
        self.active = None
        if a.cancel_reason == "timeout" and done.status == JobStatus.CANCELED.value:
            done.status = JobStatus.TIMED_OUT.value
            done.error = done.error or f"Timed out after {a.job.timeout_s:.0f} s"
        self.last_done = done
        timings = ", ".join(f"{k.lower()} {v:.2f}s" for k, v in done.phase_timings.items())
        backend = done.backend.text() if done.backend else "n/a"
        log.info(
            "[route:%d] %s in %.1fs · backend: %s · phases: %s · %s",
            done.job_id,
            done.status.lower(),
            done.elapsed_s,
            backend,
            timings or "n/a",
            done.diagnostics,
        )
        if done.traceback:
            log.error("[route:%d] worker error: %s\n%s", done.job_id, done.error, done.traceback)
        self._set_state(JobState.IDLE)
        try:
            a.on_done(done)
        except Exception:  # a result handler bug must not break the job controller
            log.exception("[route:%d] result handler failed", done.job_id)
        self.finished.emit(done)
