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
