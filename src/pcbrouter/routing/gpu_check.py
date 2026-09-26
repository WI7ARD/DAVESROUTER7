"""In-app GPU check: route some nets of the open board with the CPU A* and with the
GPU wavefront, validate every route with the exact engine, and compare timings.

Gated like every GPU job: without a device the check is SKIPPED (with the reason)
and nothing is scheduled on the GPU. Nothing is committed to the working board.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pcbrouter.compute.probe import SKIPPED, gpu_gate
from pcbrouter.routing.request import RouteRequest
from pcbrouter.routing.result import RouteStatus
from pcbrouter.routing.router import Router
from pcbrouter.routing.search.wavefront import wavefront_search

if TYPE_CHECKING:
    from pcbrouter.board_engine import BoardEngine
    from pcbrouter.compute.gpu_backend import GPUBackend
    from pcbrouter.compute.probe import GpuProbe

MAX_NETS = 8


@dataclass
class GpuCheckRow:
    net: str
    backend: str
    status: str
    legal: bool
    seconds: float
    error: str = ""


@dataclass
class GpuCheckResult:
    status: str  # "RAN" | SKIPPED | "ERROR"
    reason: str = ""
    device: str = ""
    rows: list[GpuCheckRow] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.status != "RAN":
            return f"GPU check {self.status}: {self.reason}"
        gpu = [r for r in self.rows if r.backend == "gpu"]
        if any(r.error for r in gpu):
            return "GPU problems found: some searches raised errors (they fall back to the CPU)"
        if any(r.status == RouteStatus.SUCCESS.value and not r.legal for r in gpu):
            return "GPU problems found: an illegal route was produced (it would be rejected)"
        cpu_t = sum(r.seconds for r in self.rows if r.backend == "cpu")
        gpu_t = sum(r.seconds for r in gpu)
        speed = f"CPU {cpu_t:.2f} s vs GPU {gpu_t:.2f} s"
        return f"GPU works on {self.device}: all GPU routes legal ({speed})"

    def text(self) -> str:
        lines = [self.verdict, ""]
        if self.rows:
            lines.append(f"{'net':<14}{'backend':<9}{'status':<16}{'legal':<7}{'time s':>8}")
            for r in self.rows:
                lines.append(
                    f"{r.net:<14}{r.backend:<9}{r.status:<16}{r.legal!s:<7}{r.seconds:>8.3f}"
                    + (f"  {r.error}" if r.error else "")
                )
        return "\n".join(lines)


def run_gpu_check(
    engine: BoardEngine,
    gpu: GPUBackend | Any,
    nets: list[str],
    *,
    probe: GpuProbe | None = None,
    cancel: threading.Event | None = None,
    base_request: RouteRequest | None = None,
) -> GpuCheckResult:
    gate = gpu_gate("in-app-gpu-check", probe)
    if not gate.available:
        return GpuCheckResult(SKIPPED, gate.reason)
    if gpu is None or not gpu.available:
        why = getattr(gpu, "last_error", None) or "GPU backend could not be initialised"
        return GpuCheckResult("ERROR", str(why))
    try:
        device = gpu.device_info().name
    except Exception:
        device = ", ".join(gate.devices) or "GPU"
    result = GpuCheckResult("RAN", device=device)

    def gpu_fn(problem: Any, **kw: Any) -> Any:
        kw.pop("record_explored", None)
        try:
            fits_ok, fits_why = gpu.fits(problem.grid.n, len(problem.grid.layers))
        except Exception:
            fits_ok, fits_why = False, "could not estimate GPU memory"
        if not fits_ok:
            raise MemoryError(f"GPU skipped, would not fit on device: {fits_why}")
        return wavefront_search(problem, xp=gpu.xp, **kw)

    for net in nets[:MAX_NETS]:
        for name, fn in (("cpu", None), ("gpu", gpu_fn)):
            if cancel is not None and cancel.is_set():
                return result
            req = RouteRequest(net, request_id=f"gpu-check-{name}-{net}", candidates=1)
            if base_request is not None:
                from dataclasses import replace

                req = replace(base_request, net=net, request_id=req.request_id, candidates=1)
            t0 = time.perf_counter()
            try:
                res = Router(engine, search_fn=fn, backend_name=name).route_net(req, cancel=cancel)
                legal = bool(res.best and engine.validator.validate_route(res.best.proposal).legal)
                row = GpuCheckRow(net, name, res.status.value, legal, time.perf_counter() - t0)
            except Exception as exc:
                row = GpuCheckRow(net, name, "ERROR", False, time.perf_counter() - t0, repr(exc))
            result.rows.append(row)
    return result
