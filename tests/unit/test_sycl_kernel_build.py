"""The fused routing kernel on Intel GPUs whose preferred backend cannot build it.

Reported on an Iris Xe: the GPU is selected through Level Zero, and dpctl builds
OpenCL C source only for OpenCL queues, so ``create_program_from_source`` raised
SyclProgramCompilationError and the in-app GPU check crashed. Now the kernel is
built on the same GPU's OpenCL device. If no backend can build it, the error is
clear, the GPU check reports it instead of crashing, and routing stays on the CPU
(the device is not retried for every search). Fake dpctl: no GPU needed.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from pcbrouter.compute import sycl_relax
from pcbrouter.compute.sycl_relax import KernelBuildError, SyclRelax
from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.routing import backend as backend_mod
from pcbrouter.routing.backend import HybridSearch, SearchMode
from pcbrouter.routing.request import RouteRequest
from pcbrouter.routing.result import RouteStatus
from pcbrouter.routing.router import Router
from pcbrouter.routing.working_board import WorkingBoard

BOARDS = Path(__file__).parent.parent / "fixtures" / "boards"


class SyclProgramCompilationError(Exception):
    pass


class Dev:
    def __init__(self, backend: str, name: str = "Intel(R) Iris(R) Xe Graphics") -> None:
        self.name = name
        self.backend = f"backend_type.{backend}"
        self.filter_string = f"{backend}:gpu:0"


def install(monkeypatch: pytest.MonkeyPatch, buildable: set[str], opencl: list[Dev]) -> list[str]:
    built: list[str] = []
    dpctl = types.ModuleType("dpctl")
    dpctl.SyclQueue = lambda dev, property=None: types.SimpleNamespace(sycl_device=dev)  # type: ignore[attr-defined]
    dpctl.get_devices = lambda backend=None, device_type=None: list(opencl)  # type: ignore[attr-defined]
    program = types.ModuleType("dpctl.program")

    def create(queue: Any, src: str) -> Any:
        flt = queue.sycl_device.filter_string
        built.append(flt)
        if flt.split(":")[0] not in buildable:
            raise SyclProgramCompilationError()
        return types.SimpleNamespace(get_sycl_kernel=lambda name: f"kernel@{flt}")

    program.create_program_from_source = create  # type: ignore[attr-defined]
    dpctl.program = program  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dpctl", dpctl)
    monkeypatch.setitem(sys.modules, "dpctl.program", program)
    monkeypatch.setattr(sycl_relax, "_PROGRAMS", {})
    return built


def test_level_zero_device_builds_on_the_same_gpus_opencl_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = Dev("opencl", name="Some other GPU")
    twin = Dev("opencl")
    built = install(monkeypatch, {"opencl"}, [other, twin])
    eng = SyclRelax(Dev("level_zero"))
    assert built == ["level_zero:gpu:0", "opencl:gpu:0"]
    assert eng.device is twin and eng.backend == "opencl"  # same name wins
    assert eng.kernel == "kernel@opencl:gpu:0"


def test_an_opencl_device_is_used_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    built = install(monkeypatch, {"opencl"}, [])
    dev = Dev("opencl")
    assert SyclRelax(dev).device is dev and built == ["opencl:gpu:0"]


def test_no_buildable_backend_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, set(), [Dev("opencl")])
    with pytest.raises(KernelBuildError) as info:
        SyclRelax(Dev("level_zero"))
    text = str(info.value)
    assert "level_zero:gpu:0" in text and "opencl:gpu:0" in text
    assert "SyclProgramCompilationError" in text and "routing stays on the CPU" in text


class _Gpu:
    available = True
    detection = types.SimpleNamespace(array_module="dpnp")

    def __init__(self) -> None:
        self.sycl_device = Dev("level_zero")

    def fits(self, cells: int, layers: int) -> tuple[bool, str]:
        return True, "fake"


def test_routing_falls_back_once_and_stays_on_the_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    built = install(monkeypatch, set(), [])
    calls = {"n": 0}
    real = backend_mod.device_search

    def counting(gpu: Any) -> Any:
        calls["n"] += 1
        return real(gpu)

    monkeypatch.setattr(backend_mod, "device_search", counting)
    path = BOARDS / "router_basic.kicad_pcb"
    wb = WorkingBoard(load_board(path).board, load_project_rules(path))
    hybrid = HybridSearch(SearchMode.GPU, _Gpu())  # type: ignore[arg-type]
    router = Router(wb.engine, search_fn=hybrid, backend_name="hybrid-gpu")
    for net in ("B", "C"):
        assert router.route_net(RouteRequest(net, candidates=1)).status is RouteStatus.SUCCESS
    assert calls["n"] == 1 and built == ["level_zero:gpu:0"]  # one build attempt, not per search
    assert hybrid.used["gpu"] == 0 and hybrid.used["cpu"] >= 2
    assert hybrid.last_selection is not None
    assert "GPU kernel unavailable" in hybrid.last_selection[1]


def test_gpu_check_reports_instead_of_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    from pcbrouter.routing import gpu_check

    install(monkeypatch, set(), [])
    monkeypatch.setattr(
        gpu_check,
        "gpu_gate",
        lambda job, probe=None: types.SimpleNamespace(available=True, devices=["Iris Xe"]),
    )
    gpu = _Gpu()
    gpu.device_info = lambda: types.SimpleNamespace(name="Intel Iris Xe")  # type: ignore[attr-defined]
    path = BOARDS / "router_basic.kicad_pcb"
    wb = WorkingBoard(load_board(path).board, load_project_rules(path))
    res = gpu_check.run_gpu_check(wb.engine, gpu, ["B"])
    assert res.status == "ERROR" and not res.rows
    assert "could not build the routing kernel" in res.reason and "CPU" in res.verdict
