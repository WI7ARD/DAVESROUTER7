"""Search-backend selection: CPU (reference), GPU, or AUTO — with safe fallback.

* **CPU** — A* (:mod:`pcbrouter.routing.search.astar`), the authoritative backend.
* **GPU** — array wavefront (:mod:`pcbrouter.routing.search.wavefront`) on CuPy
  (NVIDIA) or dpnp (Intel oneAPI) when the GPU backend initialised, the grid fits
  in device memory and the request has no via limit (the wavefront has no via
  dimension); otherwise that search runs on the CPU.
* **AUTO** — the GPU only for grids of at least ``AUTO_MIN_CELLS`` cells (small
  problems are faster on the CPU once transfer and launch overheads count).

Any GPU error falls back to the CPU A* for that search and is logged; the job
continues. Whatever produced the path, the route then goes through the same
simplification and the same exact CPU validator.
"""

from __future__ import annotations

import logging
import threading
from enum import Enum
from typing import TYPE_CHECKING, Any

from pcbrouter.board_engine import BoardEngine
from pcbrouter.compute.probe import SKIPPED, gpu_gate
from pcbrouter.routing.router import Router
from pcbrouter.routing.search.astar import SearchOutcome, SearchProblem, search
from pcbrouter.routing.search.wavefront import wavefront_search

if TYPE_CHECKING:
    from pcbrouter.compute.gpu_backend import GPUBackend
    from pcbrouter.compute.manager import ComputeManager

log = logging.getLogger(__name__)

AUTO_MIN_CELLS = 2_000_000


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
        if self.mode is SearchMode.AUTO and cells * layers < AUTO_MIN_CELLS:
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
    ) -> SearchOutcome:
        reason = self._gpu_reason(problem)
        if reason is None and self.gpu is not None:
            try:
                out = wavefront_search(
                    problem,
                    node_limit=node_limit,
                    time_limit_s=time_limit_s,
                    cancel=cancel,
                    xp=self.gpu.xp,
                )
                with self._lock:
                    self.used["gpu"] += 1
                return out
            except Exception as exc:  # any device/library failure: CPU takes over
                log.warning("gpu.fallback reason=%r (search continues on the CPU)", exc)
                with self._lock:
                    self.used["fallback"] += 1
                    self.fallback_reasons.append(repr(exc))
        with self._lock:
            self.used["cpu"] += 1
        return search(
            problem,
            node_limit=node_limit,
            time_limit_s=time_limit_s,
            cancel=cancel,
            record_explored=record_explored,
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
