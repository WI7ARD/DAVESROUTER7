"""The routing worker process.

Started by the GUI with the ``spawn`` start method (the only one on Windows, and
the one used everywhere here, so development and the frozen build behave alike).
One persistent process runs one job at a time:

    inbox  (GUI → worker): (job_id, job) tuples; ``None`` = shut down
    outbox (worker → GUI): WorkerReady, RouteProgress, Heartbeat, LogBatch, JobDone
    cancel_job (shared int): the id of the job the GUI wants stopped
    paused     (shared int): 1 = pause board routing between nets

A monitor thread mirrors the shared values into per-job ``threading.Event``s that
the router checks (A* every 2048 expansions, the wavefront every sweep, board
routing between nets), sends a heartbeat every ``HEARTBEAT_S`` and flushes log
lines in batches. The main thread only runs jobs.
"""

from __future__ import annotations

import collections
import logging
import os
import pickle
import queue
import sys
import threading
import time
import traceback
from typing import Any

from pcbrouter.jobs.protocol import (
    Heartbeat,
    JobDone,
    JobStatus,
    LogBatch,
    WorkerReady,
    install_pickling,
)

HEARTBEAT_S = 0.5
LOG_FLUSH_S = 0.25
MAX_LOG_LINES_PER_BATCH = 200


class _LogCapture(logging.Handler):
    """Buffers formatted records; the monitor thread ships them in batches."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: collections.deque[str] = collections.deque()
        self.suppressed = 0
        self.lock_ = threading.Lock()
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:
            return
        with self.lock_:
            if len(self.lines) >= 5 * MAX_LOG_LINES_PER_BATCH:
                self.suppressed += 1
            else:
                self.lines.append(text)

    def take(self) -> tuple[list[str], int]:
        with self.lock_:
            n = min(len(self.lines), MAX_LOG_LINES_PER_BATCH)
            out = [self.lines.popleft() for _ in range(n)]
            suppressed, self.suppressed = self.suppressed, 0
            return out, suppressed


def memory_info() -> tuple[int | None, int | None]:
    """(current RSS, peak RSS) in bytes when the platform tells us; else None."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            handle = ctypes.windll.kernel32.GetCurrentProcess()  # type: ignore[attr-defined]
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(  # type: ignore[attr-defined]
                handle, ctypes.byref(pmc), pmc.cb
            )
            return (pmc.WorkingSetSize, pmc.PeakWorkingSetSize) if ok else (None, None)
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_bytes = peak if sys.platform == "darwin" else peak * 1024
        rss = None
        if os.path.exists("/proc/self/statm"):
            with open("/proc/self/statm", encoding="ascii") as fh:
                rss = int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        return rss, peak_bytes
    except Exception:
        return None, None


_CRASH_FILE: Any = None


def _enable_worker_crash_log() -> None:
    """A native crash in the worker (GPU driver, NumPy build…) is caught by the GUI
    as "worker stopped (exit code …)"; the stack goes to worker-crash.log."""
    global _CRASH_FILE
    import faulthandler

    try:
        from pcbrouter.utils.paths import log_dir

        path = log_dir() / "worker-crash.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        _CRASH_FILE = open(path, "a", encoding="utf-8")  # noqa: SIM115
        _CRASH_FILE.write(f"--- routing worker pid {os.getpid()} {time.ctime()} ---\n")
        _CRASH_FILE.flush()
        faulthandler.enable(file=_CRASH_FILE, all_threads=True)
    except OSError:
        pass


def worker_main(
    inbox: Any, outbox: Any, cancel_job: Any, paused: Any, in_process: bool = False
) -> None:
    """Process entry point (must stay importable at module level for ``spawn``).

    ``in_process=True``: fallback mode on a thread of the GUI process (used only
    when a worker process cannot be started): the app's logging and crash log are
    left alone; log records already reach the app's own handlers."""
    install_pickling()
    capture = _LogCapture()
    if not in_process:
        _enable_worker_crash_log()
        root = logging.getLogger()
        root.handlers[:] = [capture]
        root.setLevel(logging.INFO)
    log = logging.getLogger("pcbrouter.worker")

    current: dict[str, Any] = {"job": None, "cancel": threading.Event(), "run": threading.Event()}
    current["run"].set()
    current_lock = threading.Lock()
    stop = threading.Event()

    def send(msg: object) -> None:
        # pickled here, not in the queue's feeder thread, so an unpicklable
        # result raises in this thread and is reported instead of vanishing
        outbox.put(pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL))

    def monitor() -> None:
        last_beat = last_log = 0.0
        while not stop.wait(0.02):
            with current_lock:
                job = current["job"]
                cancel_ev = current["cancel"]
                run_ev = current["run"]
                cancel_val = cancel_job.value
                paused_val = paused.value
            if job is not None and cancel_val == job:
                cancel_ev.set()
                run_ev.set()  # a paused job must wake up to see the cancel
            elif job is not None:
                (run_ev.clear if paused_val else run_ev.set)()
            now = time.monotonic()
            if now - last_log >= LOG_FLUSH_S:
                last_log = now
                lines, suppressed = capture.take()
                if lines or suppressed:
                    send(LogBatch(job, lines, suppressed))
            if now - last_beat >= HEARTBEAT_S:
                last_beat = now
                rss, peak = memory_info()
                send(Heartbeat(job, os.getpid(), rss, peak))

    threading.Thread(target=monitor, name="route-worker-monitor", daemon=True).start()
    if in_process:  # nothing to forward: the records went to the app's handlers
        capture.setLevel(logging.CRITICAL + 1)
    send(WorkerReady(os.getpid(), sys.version.split()[0]))
    log.info("routing worker started pid=%d", os.getpid())

    from pcbrouter.jobs.execute import JobCancelled, JobContext, run_job

    while not stop.is_set():
        try:
            msg = inbox.get(timeout=0.5)
        except queue.Empty:
            continue
        except (OSError, EOFError, ValueError):
            break
        if msg is None:
            break
        job_id, job = msg
        cancel, run = threading.Event(), threading.Event()
        run.set()
        with current_lock:
            current.update(cancel=cancel, run=run, job=job_id)
        ctx = JobContext(job_id, send, cancel, run)
        t0 = time.monotonic()
        log.info("[route:%d] worker started %s (%s)", job_id, job.kind, job.title)
        done = JobDone(job_id, JobStatus.COMPLETED.value)
        try:
            done.value = run_job(job, ctx)
            if cancel.is_set():
                done.status = JobStatus.CANCELED.value
        except JobCancelled:
            done.status = JobStatus.CANCELED.value
        except BaseException as exc:  # reported with the full traceback, never hidden
            done.status = JobStatus.FAILED.value
            done.error = f"{type(exc).__name__}: {exc}"
            done.traceback = traceback.format_exc()
            log.error("[route:%d] failed: %s", job_id, done.error)
        ctx.reporter.flush()
        done.elapsed_s = time.monotonic() - t0
        done.phase_timings = dict(ctx.reporter.timings)
        done.backend = ctx.finish_backend()
        rss, peak = memory_info()
        done.diagnostics = {
            "progress_events": ctx.reporter.events,
            "progress_messages": ctx.reporter.sent,
            "rss_bytes": rss,
            "peak_rss_bytes": peak,
            "worker_pid": os.getpid(),
        }
        log.info("[route:%d] %s in %.1fs", job_id, done.status.lower(), done.elapsed_s)
        with current_lock:
            current["job"] = None
        lines, suppressed = capture.take()
        if lines or suppressed:
            send(LogBatch(job_id, lines, suppressed))
        try:
            send(done)
        except Exception as exc:  # e.g. an unpicklable result: report it instead
            send(
                JobDone(
                    job_id,
                    JobStatus.FAILED.value,
                    error=f"result not transferable: {exc!r}",
                    traceback=traceback.format_exc(),
                )
            )
    stop.set()
