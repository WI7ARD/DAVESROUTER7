"""Search-backend selection: CPU (reference), GPU, or AUTO — with safe fallback.

* **CPU** — A* (:mod:`pcbrouter.routing.search.astar`), the authoritative backend.
* **GPU** — array wavefront (:mod:`pcbrouter.routing.search.wavefront`) on CuPy
  (NVIDIA) or dpnp (Intel oneAPI) when the GPU backend initialised, the grid fits
  in device memory and the request has no via limit (the wavefront has no via
  dimension); otherwise that search runs on the CPU.
* **AUTO** — the GPU only for grids of at least the per-library threshold
  below (small problems are faster on the CPU once transfer and launch
  overheads count).

Any GPU error falls back to the CPU A* for that search and is logged; the job
continues. Whatever produced the path, the route then goes through the same
simplification and the same exact CPU validator.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Any

from pcbrouter.board_engine import BoardEngine
from pcbrouter.compute.probe import SKIPPED, gpu_gate
from pcbrouter.routing.router import Router
from pcbrouter.routing.search.astar import SearchOutcome, SearchProblem, search
from pcbrouter.routing.search.relax import relax_search
from pcbrouter.routing.search.wavefront import wavefront_search

if TYPE_CHECKING:
    from pcbrouter.compute.gpu_backend import GPUBackend
    from pcbrouter.compute.manager import ComputeManager

log = logging.getLogger(__name__)

#: AUTO thresholds in cells x layers, per GPU array library.
#: dpnp (Intel integrated) measured on i5-1235U + Iris Xe, R6 bench: the
#: wavefront never beats the CPU A* there (17x-236x slower from 16k to 240k
#: cells/layer, gap widening with size), so AUTO effectively stays on the CPU;
#: explicit GPU mode still works. CUDA is unmeasured on real NVIDIA hardware
#: here, so it keeps the conservative historical value.
AUTO_MIN_CELLS_CUDA = 2_000_000
AUTO_MIN_CELLS_ONEAPI = 50_000_000
#: legacy alias (CUDA default)
AUTO_MIN_CELLS = AUTO_MIN_CELLS_CUDA


def auto_min_cells(gpu: Any) -> int:
    """Per-library AUTO threshold for ``gpu`` (unknown libraries: CUDA value)."""
    module = getattr(getattr(gpu, "detection", None), "array_module", None)
    if module == "dpnp":
        return AUTO_MIN_CELLS_ONEAPI
    return AUTO_MIN_CELLS_CUDA


def device_search(gpu: Any) -> Callable[..., SearchOutcome]:
    """The search function that runs on ``gpu``: the fused integer relaxation
    kernel on an explicitly selected SYCL device (Intel), otherwise the array
    wavefront on the backend's array module (CuPy)."""
    device = getattr(gpu, "sycl_device", None)
    if device is not None:
        from pcbrouter.compute.sycl_relax import SyclRelax

        engine = SyclRelax(device)

        def run(problem: SearchProblem, **kw: Any) -> SearchOutcome:
            kw.pop("record_explored", None)
            kw.pop("heuristic_weight", None)
            return relax_search(problem, solver=engine.solve, **kw)

        return run

    def run_xp(problem: SearchProblem, **kw: Any) -> SearchOutcome:
        kw.pop("record_explored", None)
        return wavefront_search(problem, xp=gpu.xp, **kw)

    return run_xp


class SearchMode(Enum):
    CPU = "cpu"
    GPU = "gpu"
    AUTO = "auto"


class HybridSearch:
    """A search function (same contract as ``astar.search``) that picks a backend
    per problem and falls back to the CPU on any GPU problem."""

    def __init__(self, mode: SearchMode, gpu: GPUBackend | None) -> None:
        self.mode = mode
        self.gpu = gpu
        self.used: dict[str, int] = {"cpu": 0, "gpu": 0, "fallback": 0}
        self.fallback_reasons: list[str] = []
        self._lock = threading.Lock()
        #: called as on_select(backend, reason) whenever the choice *changes* (so a
        #: UI can say "AUTO → CPU: problem too small"); never once per search
        self.on_select: Callable[[str, str], None] | None = None
        self.last_selection: tuple[str, str] | None = None
        self._device_fn: Callable[..., SearchOutcome] | None = None

    def _selected(self, backend: str, reason: str) -> None:
        sel = (backend, reason)
        if sel != self.last_selection:
            self.last_selection = sel
            if self.on_select is not None:
                self.on_select(backend, reason)

    def _gpu_reason(self, problem: SearchProblem) -> str | None:
        """None if the GPU should run this problem, else why not."""
        if self.mode is SearchMode.CPU:
            return "CPU selected"
        gpu = self.gpu
        if gpu is None or not gpu.available:
            return "GPU backend unavailable"
        if problem.max_vias is not None and problem.vias_enabled:
            return "via limit requires the CPU A*"
        cells = problem.grid.n
        layers = len(problem.grid.layers)
        if self.mode is SearchMode.AUTO and cells * layers < auto_min_cells(gpu):
            return f"problem too small for the GPU ({cells * layers:,} cells)"
        ok, why = gpu.fits(cells, layers)
        if not ok:
            return f"not enough GPU memory ({why})"
        return None

    def __call__(
        self,
        problem: SearchProblem,
        *,
        node_limit: int,
        time_limit_s: float,
        cancel: threading.Event | None = None,
        record_explored: bool = False,
        heuristic_weight: float = 1.0,
    ) -> SearchOutcome:
        # Weighted search is an A* concept: forwarded on the CPU path, ignored
        # by the wavefront (which has no heuristic).
        reason = self._gpu_reason(problem)
        if reason is None and self.gpu is not None:
            self._selected("gpu", f"{self.mode.value.upper()} mode, grid fits on the device")
            try:
                if self._device_fn is None:
                    self._device_fn = device_search(self.gpu)
                out = self._device_fn(
                    problem, node_limit=node_limit, time_limit_s=time_limit_s, cancel=cancel
                )
                with self._lock:
                    self.used["gpu"] += 1
                return out
            except Exception as exc:  # any device/library failure: CPU takes over
                log.warning("gpu.fallback reason=%r (search continues on the CPU)", exc)
                with self._lock:
                    self.used["fallback"] += 1
                    self.fallback_reasons.append(repr(exc))
                reason = f"GPU error, CPU fallback: {exc!r}"[:200]
        self._selected("cpu", reason or "CPU")
        with self._lock:
            self.used["cpu"] += 1
        return search(
            problem,
            node_limit=node_limit,
            time_limit_s=time_limit_s,
            cancel=cancel,
            record_explored=record_explored,
            heuristic_weight=heuristic_weight,
        )


def mode_from_settings(compute: ComputeManager | None, choice: Any = None) -> SearchMode:
    if choice is not None:
        value = getattr(choice, "value", choice)
        if value in ("cpu", "gpu", "auto"):
            return SearchMode(value)
    if compute is not None and compute.active is compute.gpu:
        return SearchMode.GPU
    return SearchMode.CPU


def router_for(
    engine: BoardEngine,
    compute: ComputeManager | None = None,
    mode: SearchMode | None = None,
) -> Router:
    """A router with the selected search backend (CPU unless a GPU is usable)."""
    mode = mode or mode_from_settings(compute)
    if mode is SearchMode.CPU or compute is None:
        return Router(engine, backend_name="cpu")
    # Hardware gate: GPU acceleration is only scheduled when a device exists.
    gate = gpu_gate("routing-acceleration")
    if not gate.available:
        return Router(engine, backend_name=f"cpu (GPU {SKIPPED}: {gate.reason})")
    hybrid = HybridSearch(mode, compute.gpu)
    return Router(engine, search_fn=hybrid, backend_name=f"hybrid-{mode.value}")
