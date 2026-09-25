"""Stage 8 workbench UI: view modes, locks (undoable), reroute section, inspector,
change preview, and a realistic end-to-end user flow."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from pcbrouter.routing.result import RouteStatus
from pcbrouter.settings import SettingsStore
from pcbrouter.ui.main_window import MainWindow
from pcbrouter.ui.pcb_canvas import ItemKind
from tests.integration.test_routing_ui import wait
from tests.integration.test_ui import make_window

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot: QtBot, tmp_path: Path) -> Iterator[MainWindow]:
    w = make_window(qtbot, SettingsStore(tmp_path / "config" / "settings.json"))
    yield w
    w.close()


def route_and_accept(window: MainWindow, net: str) -> None:
    window._on_net_selected(net)
    assert window.routing_ui.route_selected_net()
    wait(window)
    assert window.routing_ui.panel.result.status is RouteStatus.SUCCESS
    assert window.routing_ui.accept(window.routing_ui.panel.current)
    wait(window)


def select(window: MainWindow, kind: ItemKind, obj_id: str) -> None:
    window.canvas.select_object(kind, obj_id)
    window.canvas.objectSelected.emit(kind, obj_id)


def test_workbench_flow(window: MainWindow, fixture_path: Callable[[str], Path]) -> None:
    assert window.open_board(fixture_path("router_basic.kicad_pcb"))
    wait(window)
    wb = window.bus.context.project.working
    # route, with a change preview (diff incl. DRC before/after)
    window._on_net_selected("B")
    window.routing_ui.route_selected_net()
    wait(window)
    wait(window)
    window.workbench.jobs.wait(10_000)
    wait(window)
    assert any("segment" in line for line in window.workbench.last_diff_lines)
    assert "Change preview" in window.routing_ui.panel.text()
    assert window.routing_ui.accept(window.routing_ui.panel.current)
    wait(window)
    track = next(t for t in wb.board.tracks if t.net_name == "B")
    # view modes
    window.workbench.set_view_mode("original")
    assert not window.canvas.records_for(ItemKind.TRACK, track.id)[0].isVisible()
    window.workbench.set_view_mode("difference")
    rec = window.canvas.records_for(ItemKind.TRACK, track.id)[0]
    assert rec.isVisible() and rec.opacity() == 1.0
    window.workbench.set_view_mode("working")
    # route inspector
    select(window, ItemKind.TRACK, track.id)
    assert "Route (Stage 8)" in [
        window.inspector._tree.topLevelItem(i).text(0)
        for i in range(window.inspector._tree.topLevelItemCount())
    ]
    # reroute this section (Shift+R) -> preview -> accept replaces the section
    assert window.workbench.reroute_section()
    wait(window)
    assert window.routing_ui.pending_remove_ids
    assert window.routing_ui.accept(window.routing_ui.panel.current)
    wait(window)
    assert track.id not in {t.id for t in wb.board.tracks}
    # lock the net: routing it again is refused; undo the lock
    window._on_net_selected("B")
    window._selected = (ItemKind.NET, "B")
    assert window.workbench.toggle_lock()
    assert wb.is_locked("", "B") and window.engine_ui.overlays.has("locks")
    assert not window.routing_ui.route_net(window.routing_ui.request_for("B"))
    window.undo()
    assert not wb.is_locked("", "B")
    # continue with full-board routing; everything still undoable
    assert window.routing_ui.route_board()
    wait(window)
    assert window.routing_ui.accept_board(None)
    wait(window)
    window.engine_ui.run_geometry_check()
    wait(window)
    assert not window.engine_ui.drc_panel.result.errors
