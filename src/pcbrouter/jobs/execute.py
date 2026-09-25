"""Runs one job. Called inside the routing worker process (and directly by unit
tests). No Qt here: progress leaves through :class:`ProgressReporter`, which
coalesces the router's internal events into at most ~10 messages per second.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from pcbrouter.jobs.protocol import (
    AIPlanJob,
    BackendInfo,
    ExportJob,
    FakeJob,
    GpuCheckJob,
    JobBase,
    JobPhase,
    OptimizeJob,
    RouteBoardJob,
    RouteNetJob,
    RouteProgress,
    WorkingSnapshot,
)

log = logging.getLogger(__name__)

PROGRESS_INTERVAL_S = 0.1  # ≤ 10 progress messages per second reach the GUI
SIMULATE_GPU_ENV = "PCBROUTER_SIMULATE_GPU"  # tests/diagnostics only: "numpy"

Send = Callable[[object], None]


class JobCancelled(Exception):  # noqa: N818 - a stop signal, not an error
    """Raised by a job that stops early because cancellation was requested."""


class ProgressReporter:
    """Aggregates progress: the router may report thousands of events; the GUI gets
    the latest state at most every ``interval`` seconds (plus a final flush)."""

    def __init__(self, job_id: int, send: Send, interval: float = PROGRESS_INTERVAL_S) -> None:
        self.job_id = job_id
        self._send = send
        self.interval = interval
        self.t0 = time.monotonic()
        self.state = RouteProgress(job_id, JobPhase.PREPARING_BOARD.value)
        self._last_sent = 0.0
        self._dirty = False
        self._lock = threading.Lock()
        self._phase_started = self.t0
        self.timings: dict[str, float] = {}
        self.events = 0  # internal events received (diagnostics)
        self.sent = 0

    def update(self, **fields: Any) -> None:
        now = time.monotonic()
        with self._lock:
            self.events += 1
            phase = fields.get("phase")
            if phase is not None and phase != self.state.phase:
                self.timings[self.state.phase] = self.timings.get(self.state.phase, 0.0) + (
                    now - self._phase_started
                )
                self._phase_started = now
            for key, value in fields.items():
                setattr(self.state, key, value)
            self._dirty = True
            if now - self._last_sent >= self.interval:
                self._flush_locked(now)

    def flush(self) -> None:
        with self._lock:
            now = time.monotonic()
            self.timings[self.state.phase] = self.timings.get(self.state.phase, 0.0) + (
                now - self._phase_started
            )
            self._phase_started = now
            if self._dirty:
                self._flush_locked(now)

    def _flush_locked(self, now: float) -> None:
        self.state.elapsed_s = now - self.t0
        self._send(replace(self.state))
        self._last_sent = now
        self._dirty = False
        self.sent += 1


class JobContext:
    def __init__(
        self,
        job_id: int,
        send: Send,
        cancel: threading.Event | None = None,
        pause: threading.Event | None = None,
    ) -> None:
        self.job_id = job_id
        self.send = send
        self.cancel = cancel or threading.Event()
        #: set = running, clear = paused (board jobs honour it between nets)
        self.pause = pause
        self.reporter = ProgressReporter(job_id, send)
        self.backend: BackendInfo | None = None
        self._hybrid: Any = None

    # -------------------------------------------------------------- progress hooks
    def router_progress(self, top_level: bool) -> Callable[[dict[str, Any]], None]:
        """Router events: phases for a single-net job; a detail line inside a board job."""

        def hook(info: dict[str, Any]) -> None:
            fields: dict[str, Any] = {}
            for key in (
                "candidates_completed",
                "candidates_total",
                "connection",
                "connections_total",
                "grid",
            ):
                if key in info:
                    fields[key] = info[key]
            if top_level:
                if "phase" in info:
                    fields["phase"] = info["phase"]
                if "net" in info:
                    fields["current_net_name"] = info["net"]
            elif "phase" in info:
                fields["message"] = (
                    f"{info.get('net', '')}: {info['phase'].lower().replace('_', ' ')}"
                )
            self.reporter.update(**fields)

        return hook

    def board_progress(self, info: dict[str, Any]) -> None:
        m = info.get("metrics") or {}
        fields: dict[str, Any] = {"board_info": info}
        if info.get("phase"):
            fields["phase"] = info["phase"]
        if info.get("net") is not None:
            fields["current_net_name"] = info["net"]
        if info.get("index") is not None:
            fields["current_net"] = info["index"]
        if info.get("total_nets") is not None:
            fields["total_nets"] = info["total_nets"]
        if info.get("completed_nets") is not None:
            fields["completed_nets"] = info["completed_nets"]
            fields["routes_succeeded"] = info["completed_nets"]
        if info.get("pass_no") is not None:
            fields["current_pass"] = info["pass_no"]
        if info.get("total_passes") is not None:
            fields["total_passes"] = info["total_passes"]
        if "ripups" in m:
            fields["ripups"] = m["ripups"]
        if info.get("state") == "ripup":
            fields["message"] = f"rip-up for {info.get('net')} (try {info.get('ripup_try', 1)})"
        self.reporter.update(**fields)

    def phase(self, name: str, message: str = "") -> None:
        self.reporter.update(phase=name, message=message)

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise JobCancelled()

    # -------------------------------------------------------------- backend choice
    def router_factory(self, mode: str, top_level: bool) -> Callable[[Any], Any]:
        """Select the search backend *inside the worker* (GPU libraries and device
        contexts are initialised here, never in the GUI process) and report the
        actual choice; returns engine → Router."""
        from pcbrouter.compute.probe import gpu_gate
        from pcbrouter.routing.backend import HybridSearch, SearchMode
        from pcbrouter.routing.router import Router

        requested = mode.upper()
        hook = self.router_progress(top_level)
        search_fn: Any = None
        name = "cpu"
        if mode == "cpu":
            self.backend = BackendInfo("CPU", "CPU", "CPU selected in Settings")
        else:
            probe = _simulated_probe() or gpu_gate(f"route-job-{self.job_id}")
            gpu = _worker_gpu() if probe.available else None
            if not probe.available:
                self.backend = BackendInfo(
                    requested, "CPU fallback", f"GPU skipped: {probe.reason}"
                )
            elif gpu is None or not gpu.available:
                why = getattr(gpu, "last_error", None) or "GPU backend could not be initialised"
                self.backend = BackendInfo(requested, "CPU fallback", str(why))
            else:
                label = _gpu_label(gpu, probe)
                self.backend = BackendInfo(
                    requested,
                    label if mode == "gpu" else "AUTO (decided per search)",
                    "" if mode == "gpu" else "GPU for grids ≥ 2M cells, CPU below",
                )
                hybrid = HybridSearch(SearchMode(mode), gpu)
                info = self.backend

                def selected(backend: str, reason: str) -> None:
                    info.selected = label if backend == "gpu" else "CPU"
                    info.reason = reason
                    self.reporter.update(backend=replace(info))

                hybrid.on_select = selected
                self._hybrid = hybrid
                search_fn, name = hybrid, f"hybrid-{mode}"
        self.reporter.update(backend=replace(self.backend))
        log.info("[route:%s] backend %s", self.job_id, self.backend.text())

        def factory(engine: Any) -> Any:
            router = Router(engine, search_fn=search_fn, backend_name=name)
            router.progress = hook
            return router

        return factory

    def finish_backend(self) -> BackendInfo | None:
        if self.backend is not None and self._hybrid is not None:
            used = self._hybrid.used
            self.backend.searches_cpu = used.get("cpu", 0)
            self.backend.searches_gpu = used.get("gpu", 0)
            self.backend.gpu_fallbacks = used.get("fallback", 0)
        return self.backend


# ------------------------------------------------------------------ GPU in the worker
_GPU: Any = None


def _simulated_probe() -> Any:
    if os.environ.get(SIMULATE_GPU_ENV) != "numpy":
        return None
    from pcbrouter.compute.probe import GpuProbe

    return GpuProbe(True, "numpy-simulated", ("simulated GPU (NumPy on the CPU)",))


class _SimulatedGpu:
    """Diagnostics/tests: runs the GPU code path with NumPy. Always labelled
    'simulated' so it can never be mistaken for real GPU use."""

    def __init__(self) -> None:
        import numpy as np

        self.xp = np
        self.available = True
        self.initialized = True
        self.last_error = None

    def fits(self, cells: int, layers: int) -> tuple[bool, str]:
        return True, "simulated"

    def device_info(self) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(name="simulated GPU (NumPy on the CPU)")


def _worker_gpu() -> Any:
    """One GPU backend per worker process, initialised on first use (here, in the
    worker: the GUI process never imports CuPy/dpnp or touches the device)."""
    global _GPU
    if _GPU is None:
        if os.environ.get(SIMULATE_GPU_ENV) == "numpy":
            _GPU = _SimulatedGpu()
        else:
            from pcbrouter.compute.detection import detect_gpu
            from pcbrouter.compute.gpu_backend import GPUBackend

            _GPU = GPUBackend(detect_gpu())
    return _GPU


def _gpu_label(gpu: Any, probe: Any) -> str:
    try:
        device = gpu.device_info().name
    except Exception:
        device = ", ".join(probe.devices) or "GPU"
    return f"GPU ({probe.library}: {device})"


# ------------------------------------------------------------------ board cache
_CACHE: dict[str, Any] = {}


def working_from(snapshot: WorkingSnapshot, ctx: JobContext) -> Any:
    """The snapshot's WorkingBoard, reusing the last one (and its built geometry)
    when the GUI's board state has not changed since the previous job."""
    ctx.phase(JobPhase.PREPARING_BOARD.value)
    wb = _CACHE.get("wb") if _CACHE.get("key") == snapshot.key else None
    if wb is None:
        wb = snapshot.build()
        _CACHE.clear()
        _CACHE.update(key=snapshot.key, wb=wb)
    t0 = time.perf_counter()
    geo = wb.engine.geometry
    log.info(
        "[route:%s] board prepared: %d copper objects, %.2f s",
        ctx.job_id,
        len(geo.copper),
        time.perf_counter() - t0,
    )
    return wb


# ------------------------------------------------------------------ dispatcher
def run_job(job: JobBase, ctx: JobContext) -> Any:
    if isinstance(job, FakeJob):
        return _fake(job, ctx)
    if isinstance(job, RouteNetJob):
        return _route_net(job, ctx)
    if isinstance(job, RouteBoardJob):
        return _route_board(job, ctx)
    if isinstance(job, AIPlanJob):
        return _ai_plan(job, ctx)
    if isinstance(job, OptimizeJob):
        return _optimize(job, ctx)
    if isinstance(job, GpuCheckJob):
        return _gpu_check(job, ctx)
    if isinstance(job, ExportJob):
        return _export(job, ctx)
    raise TypeError(f"unknown job type {type(job).__name__}")


def _route_net(job: RouteNetJob, ctx: JobContext) -> Any:
    wb = working_from(job.snapshot, ctx)
    ctx.check_cancel()
    if job.remove_ids:  # local reroute: route around a copy without that section
        wb = wb.fork()
        wb.commit_objects((), (), job.remove_ids, "reroute section (preview)", validate=False)
    factory = ctx.router_factory(job.mode, top_level=True)
    router = factory(wb.engine)
    router.record_explored = job.record_explored
    ctx.reporter.update(
        phase=JobPhase.ROUTING.value,
        current_net_name=job.request.net,
        total_nets=1,
        completed_nets=0,
    )
    result = router.route_net(job.request, cancel=ctx.cancel)
    ctx.reporter.update(
        phase=JobPhase.VALIDATING.value,
        completed_nets=1,
        routes_succeeded=1 if result.best is not None else 0,
        routes_failed=0 if result.best is not None else 1,
    )
    return result


def _board_control(ctx: JobContext) -> Any:
    from pcbrouter.routing.board_router import BoardRoutingControl

    control = BoardRoutingControl()
    control.cancel_event = ctx.cancel  # one event: every net and every search sees it
    if ctx.pause is not None:
        control._running = ctx.pause
    return control


def _route_board(job: RouteBoardJob, ctx: JobContext) -> Any:
    from pcbrouter.routing.board_router import BoardRouter, make_plan

    wb = working_from(job.snapshot, ctx)
    ctx.phase(JobPhase.PLANNING.value)
    plan = make_plan(wb, job.settings)
    log.info(
        "[route:%s] routing %d net(s), up to %d pass(es)",
        ctx.job_id,
        len(plan.tasks),
        job.settings.max_passes,
    )
    ctx.reporter.update(
        total_nets=len(plan.tasks), completed_nets=0, total_passes=job.settings.max_passes
    )
    factory = ctx.router_factory(job.mode, top_level=False)
    router = BoardRouter(wb, job.settings, router_factory=factory)
    return router.run(plan, _board_control(ctx), ctx.board_progress)


def _ai_plan(job: AIPlanJob, ctx: JobContext) -> Any:
    from pcbrouter.ai.route_bridge import execute_plan

    wb = working_from(job.snapshot, ctx)
    factory = ctx.router_factory(job.mode, top_level=job.plan.kind == "route_nets")

    def progress(info: dict[str, Any]) -> None:
        if "metrics" in info:
            ctx.board_progress(info)
        else:
            ctx.reporter.update(
                phase=info.get("phase", JobPhase.ROUTING.value),
                current_net_name=info.get("net"),
                current_net=info.get("index"),
                total_nets=info.get("total_nets"),
                completed_nets=info.get("completed_nets"),
            )

    if job.plan.kind == "optimize":
        ctx.phase(JobPhase.OPTIMIZING.value)
    return execute_plan(job.plan, wb, None, ctx.cancel, router_factory=factory, progress=progress)


def _optimize(job: OptimizeJob, ctx: JobContext) -> Any:
    from pcbrouter.commands.route_commands import plan_optimization

    wb = working_from(job.snapshot, ctx)
    ctx.reporter.update(
        phase=JobPhase.OPTIMIZING.value, current_net_name=job.net, total_nets=1, completed_nets=0
    )
    ctx.backend = BackendInfo(job.mode.upper(), "CPU", "optimisation uses the CPU router")
    return plan_optimization(wb, job.net, job.goal, control=_board_control(ctx))


def _gpu_check(job: GpuCheckJob, ctx: JobContext) -> Any:
    from pcbrouter.compute.probe import gpu_gate
    from pcbrouter.routing.gpu_check import run_gpu_check

    wb = working_from(job.snapshot, ctx)
    probe = _simulated_probe() or gpu_gate(f"gpu-check-{ctx.job_id}")
    gpu = _worker_gpu() if probe.available else None
    ctx.reporter.update(
        phase=JobPhase.ROUTING.value,
        total_nets=len(job.nets[:8]),
        completed_nets=0,
        message="CPU A* vs GPU wavefront",
    )
    result = run_gpu_check(
        wb.engine, gpu, job.nets, probe=probe, cancel=ctx.cancel, base_request=job.base_request
    )
    ctx.backend = BackendInfo("GPU", result.device or "none", result.reason or result.verdict)
    return result


def _export(job: ExportJob, ctx: JobContext) -> Any:
    from pcbrouter.commands.export_commands import perform_export

    wb = working_from(job.snapshot, ctx)
    return perform_export(
        wb,
        job.source_path,
        job.source_sha256,
        job.out_path,
        backup_dir=job.backup_dir,
        allow_unverified=job.allow_unverified,
        run_kicad_drc=job.run_kicad_drc,
        overwrite_source=job.overwrite_source,
        phase=lambda name: ctx.phase(name),
        cancel=ctx.cancel,
    )


def _fake(job: FakeJob, ctx: JobContext) -> Any:
    """Synthetic load for tests and ``--worker-selftest``."""
    if job.outcome == "flood":
        for i in range(job.flood):
            ctx.reporter.update(
                phase=JobPhase.ROUTING.value,
                completed_nets=i,
                total_nets=job.flood,
                current_net_name=f"N{i}",
            )
            log.info("[route:%s] flood line %d", ctx.job_id, i)
        return {"events": ctx.reporter.events, "sent": ctx.reporter.sent}
    total = max(1, int(job.duration_s * job.rate_hz))
    ctx.reporter.update(phase=JobPhase.ROUTING.value, total_nets=total, completed_nets=0)
    t0 = time.monotonic()
    for i in range(total):
        if job.outcome == "crash" and i >= total // 2:
            os._exit(3)  # simulate a hard crash (e.g. a GPU driver fault)
        if job.outcome == "crash_gpu" and job.mode != "cpu" and i >= total // 2:
            os._exit(3)  # a fault only on the GPU path: the CPU retry succeeds
        if job.outcome == "fail" and i >= total // 2:
            raise RuntimeError("synthetic routing failure")
        if job.outcome != "hang":
            ctx.check_cancel()
        # busy-wait on purpose: a CPU-bound Python loop like the real router
        end = t0 + (i + 1) / job.rate_hz
        while time.monotonic() < end:
            pass
        ctx.reporter.update(completed_nets=i + 1, current_net_name=f"N{i + 1}")
    if job.outcome == "hang":
        while True:  # ignores cancellation: only the GUI's hard stop ends it
            time.sleep(0.05)
    return {"steps": total, "events": ctx.reporter.events, "sent": ctx.reporter.sent}
