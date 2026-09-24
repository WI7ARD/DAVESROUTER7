"""Stage 6: wavefront search, hybrid backend selection, GPU fallback, same hard rules."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from pcbrouter.compute.detection import GpuDetectionResult, GpuDevice, GpuStatus
from pcbrouter.compute.gpu_backend import GPUBackend
from pcbrouter.compute.manager import ComputeManager
from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.routing.backend import AUTO_MIN_CELLS, HybridSearch, SearchMode, router_for
from pcbrouter.routing.request import RouteRequest
from pcbrouter.routing.result import RouteStatus
from pcbrouter.routing.router import Router
from pcbrouter.routing.search.wavefront import wavefront_search
from pcbrouter.routing.working_board import WorkingBoard

BOARDS = Path(__file__).parent.parent / "fixtures" / "boards"


def working(name: str) -> WorkingBoard:
    path = BOARDS / name
    return WorkingBoard(load_board(path).board, load_project_rules(path))


def numpy_wavefront(problem: Any, **kw: Any) -> Any:
    return wavefront_search(problem, xp=np, **kw)


@pytest.mark.parametrize("net", ["A", "B", "C", "E"])
def test_wavefront_routes_obey_the_same_hard_rules(net: str) -> None:
    """The array kernel (NumPy here; CuPy/dpnp on a GPU) feeds the same pipeline:
    geometry may differ from A*, every hard rule is identical."""
    wb = working("router_basic.kicad_pcb")
    engine = wb.engine
    cpu = Router(engine).route_net(RouteRequest(net, candidates=1))
    arr = Router(engine, search_fn=numpy_wavefront).route_net(RouteRequest(net, candidates=1))
    assert cpu.status is arr.status is RouteStatus.SUCCESS
    for res in (cpu, arr):
        prop = res.best.proposal
        assert engine.validator.validate_route(prop).legal
        for s in prop.segments:
            dx, dy = s.end.x - s.start.x, s.end.y - s.start.y
            assert dx == 0 or dy == 0 or abs(dx) == abs(dy)
    assert len(arr.best.proposal.vias) == len(cpu.best.proposal.vias)


class _BrokenXp:
    """An array module that fails like a GPU driver error."""

    float32 = np.float32

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError("CUDA_ERROR_LAUNCH_FAILED (simulated)")


class _FakeGpu:
    def __init__(self, xp: Any, free: bool = True) -> None:
        self.xp = xp
        self.available = True
        self._free = free

    def fits(self, cells: int, layers: int) -> tuple[bool, str]:
        return self._free, "fake"


def test_gpu_failure_falls_back_to_cpu_and_routes() -> None:
    wb = working("router_basic.kicad_pcb")
    hybrid = HybridSearch(SearchMode.GPU, _FakeGpu(_BrokenXp()))  # type: ignore[arg-type]
    res = Router(wb.engine, search_fn=hybrid, backend_name="hybrid-gpu").route_net(
        RouteRequest("B", candidates=1)
    )
    assert res.status is RouteStatus.SUCCESS
    assert hybrid.used["fallback"] >= 1 and hybrid.used["gpu"] == 0 and hybrid.used["cpu"] >= 1
    assert "fallback" in res.metrics.backend


def test_gpu_path_is_used_when_available() -> None:
    wb = working("router_basic.kicad_pcb")
    hybrid = HybridSearch(SearchMode.GPU, _FakeGpu(np))  # type: ignore[arg-type]
    res = Router(wb.engine, search_fn=hybrid).route_net(RouteRequest("C", candidates=1))
    assert res.status is RouteStatus.SUCCESS and hybrid.used["gpu"] >= 1


def test_selection_rules() -> None:
    wb = working("router_basic.kicad_pcb")
    auto = HybridSearch(SearchMode.AUTO, _FakeGpu(np))  # type: ignore[arg-type]
    Router(wb.engine, search_fn=auto).route_net(RouteRequest("C", candidates=1))
    assert auto.used["gpu"] == 0  # small grid stays on the CPU
    assert AUTO_MIN_CELLS > 0
    limited = HybridSearch(SearchMode.GPU, _FakeGpu(np))  # type: ignore[arg-type]
    Router(wb.engine, search_fn=limited).route_net(RouteRequest("A", candidates=1, max_vias=2))
    assert limited.used["gpu"] == 0  # via limits need the CPU A*
    no_mem = HybridSearch(SearchMode.GPU, _FakeGpu(np, free=False))  # type: ignore[arg-type]
    Router(wb.engine, search_fn=no_mem).route_net(RouteRequest("C", candidates=1))
    assert no_mem.used["gpu"] == 0  # VRAM guard


def test_app_works_without_cuda() -> None:
    mgr = ComputeManager(gpu_detection=GpuDetectionResult(GpuStatus.CUDA_UNAVAILABLE))
    wb = working("router_basic.kicad_pcb")
    router = router_for(wb.engine, mgr, SearchMode.AUTO)
    assert router.route_net(RouteRequest("C", candidates=1)).status is RouteStatus.SUCCESS
    iris = GPUBackend(
        GpuDetectionResult(
            GpuStatus.ONEAPI_LIBRARIES_NOT_INSTALLED,
            (GpuDevice("Intel(R) Iris(R) Xe Graphics", "Intel"),),
        )
    )
    assert not iris.available and "Intel" in iris.device_info().name
