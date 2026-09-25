"""Stage 4 routing in the desktop UI: route, preview, accept, undo, redo."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from pcbrouter.routing.result import RouteStatus
from pcbrouter.settings import SettingsStore
from pcbrouter.ui.main_window import MainWindow
from pcbrouter.ui.pcb_canvas import ItemKind
from tests.integration.test_ui import make_window

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot: QtBot, tmp_path: Path) -> Iterator[MainWindow]:
    w = make_window(qtbot, SettingsStore(tmp_path / "config" / "settings.json"))
    yield w
    w.close()


def wait(w: MainWindow) -> None:
    from PySide6.QtCore import QCoreApplication

    for _ in range(600):
        QCoreApplication.processEvents()
        if not w.routing_ui.jobs.is_running() and not w.engine_ui.jobs.is_running():
            break
        w.routing_ui.jobs.wait(50)
    QCoreApplication.processEvents()


def test_route_preview_accept_undo(window: MainWindow, fixture_path: Callable[[str], Path]) -> None:
    path = fixture_path("router_basic.kicad_pcb")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert window.open_board(path)
    wait(window)
    ui = window.routing_ui
    assert window.engine_ui.lbl_routing.text() == "Routing: Ready (CPU)"
    assert not ui.route_selected_net()  # nothing selected yet
    window._on_net_selected("B")
    assert ui.route_selected_net()
    wait(window)
    result = ui.panel.result
    assert result is not None and result.status is RouteStatus.SUCCESS
    assert window.engine_ui.overlays.has("route_preview")
    assert "ROUTER RESULT" in ui.panel.text() and "Length" in ui.panel.text()
    first = ui.panel.current
    ui.panel.next_alternative()
    assert ui.panel.current is not first
    n_canvas = len(window.canvas._records)
    assert ui.accept(ui.panel.current)
    wait(window)
    working = window.bus.context.project.working
    assert working.modified and len(working.board.tracks) > len(working.source.tracks)
    assert len(window.canvas._records) > n_canvas
    new_track = working.board.tracks[-1]
    assert window.canvas.records_for(ItemKind.TRACK, new_track.id)
    assert window.canvas.is_generated(new_track.id)
    assert not window.engine_ui.overlays.has("route_preview")
    assert window.act_undo.isEnabled()
    window.undo()
    wait(window)
    assert not working.modified
    assert len(window.canvas._records) == n_canvas
    window.redo()
    wait(window)
    assert working.modified
    # the Internal Geometry Check runs on the working board and passes
    window.engine_ui.run_geometry_check()
    wait(window)
    drc = window.engine_ui.drc_panel.result
    assert drc is not None and not drc.errors
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_reject_changes_nothing(window: MainWindow, fixture_path: Callable[[str], Path]) -> None:
    assert window.open_board(fixture_path("router_basic.kicad_pcb"))
    wait(window)
    window._on_net_selected("C")
    window.routing_ui.route_selected_net()
    wait(window)
    window.routing_ui.reject()
    assert not window.bus.context.project.working.modified
    assert not window.engine_ui.overlays.has("route_preview")


def test_route_board_review_accept_subset_and_undo(
    window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    from pcbrouter.routing.board_router import BoardStatus
    from pcbrouter.routing.optimize import OptimizeGoal

    path = fixture_path("router_basic.kicad_pcb")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert window.open_board(path)
    wait(window)
    ui = window.routing_ui
    assert window.act_route_board.isEnabled()
    assert ui.route_board()
    wait(window)
    result = ui.board_panel.result
    assert result is not None and result.status is BoardStatus.FULLY_ROUTED, result
    assert window.engine_ui.overlays.has("board_preview")
    assert ui.board_panel.table.rowCount() == len(result.plan.tasks)
    working = window.bus.context.project.working
    assert not working.modified  # nothing before Accept
    ui.board_panel.set_checked({"B", "C"})
    assert ui.accept_board(ui.board_panel.checked_nets())
    wait(window)
    nets = {t.net_name for t in working.board.tracks} - {t.net_name for t in working.source.tracks}
    assert nets == {"B", "C"}
    # tweak a routed net (deterministic optimiser; one undoable step or "kept")
    window._on_net_selected("B")
    ui.optimize_selected(OptimizeGoal.MERGE_COLLINEAR)
    wait(window)
    while working.modified:
        window.undo()
        wait(window)
    assert working.board is working.source
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


class _SimulatedGpu:
    """Stands in for an initialised GPU backend: NumPy plays the role of dpnp/CuPy,
    so the app's real GPU code path (wavefront on ``xp``) runs end to end."""

    def __init__(self) -> None:
        import numpy as np

        from pcbrouter.compute import GpuDetectionResult, GpuDevice, GpuStatus

        self.xp = np
        self.available = True
        self.initialized = True
        self.last_error = None
        self.detection = GpuDetectionResult(
            GpuStatus.ONEAPI_DEVICE_DETECTED, (GpuDevice("Intel(R) Iris(R) Xe Graphics", "Intel"),)
        )

    def fits(self, cells: int, layers: int) -> tuple[bool, str]:
        return True, "simulated"

    def device_info(self) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(name="Intel(R) Iris(R) Xe Graphics (simulated)")


def test_gpu_selected_in_app_routes_on_the_gpu(
    window: MainWindow, fixture_path: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from pcbrouter.compute import probe
    from pcbrouter.settings.settings import ComputeBackendChoice

    present = probe.GpuProbe(True, "dpnp", ("Intel(R) Iris(R) Xe Graphics",))
    monkeypatch.setattr(probe, "probe_gpu", lambda: present)
    window.compute.gpu = _SimulatedGpu()  # type: ignore[assignment]
    window.settings.default_compute_backend = ComputeBackendChoice.GPU
    assert window.open_board(fixture_path("router_basic.kicad_pcb"))
    wait(window)
    assert window.engine_ui.lbl_routing.text() == "Routing: Ready (GPU)"
    window._update_backend_label()
    assert "GPU: ready (dpnp)" in window.lbl_backend.text()
    ui = window.routing_ui
    window._on_net_selected("C")
    assert ui.route_selected_net()
    wait(window)
    result = ui.panel.result
    assert result is not None and result.status is RouteStatus.SUCCESS
    assert result.metrics.backend.startswith("hybrid-gpu")
    assert "gpu 0," not in result.metrics.backend  # the GPU search actually ran
    # Tools ▸ Test GPU on This Board: CPU vs GPU, every route validated
    assert ui.act_gpu_check.isEnabled() and ui.test_gpu()
    wait(window)
    check = ui.last_gpu_check
    assert check is not None and check.status == "RAN", check.verdict
    assert {r.backend for r in check.rows} == {"cpu", "gpu"}
    assert check.verdict.startswith("GPU works on"), check.text()


def test_gpu_selected_without_device_is_skipped_in_app(
    window: MainWindow, fixture_path: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from pcbrouter.compute import probe
    from pcbrouter.settings.settings import ComputeBackendChoice

    absent = probe.GpuProbe(False, None, (), "no CUDA or oneAPI GPU device found")
    monkeypatch.setattr(probe, "probe_gpu", lambda: absent)
    window.settings.default_compute_backend = ComputeBackendChoice.GPU
    assert window.open_board(fixture_path("router_basic.kicad_pcb"))
    wait(window)
    assert "GPU skipped" in window.engine_ui.lbl_routing.text()
    ui = window.routing_ui
    window._on_net_selected("C")
    assert ui.route_selected_net()
    wait(window)
    result = ui.panel.result
    assert result is not None and result.status is RouteStatus.SUCCESS  # CPU did the work
    assert "SKIPPED" in result.metrics.backend
    assert ui.test_gpu()
    wait(window)
    assert ui.last_gpu_check.status == "SKIPPED"
