"""Stage 2 UX blockers: typing-safe shortcuts, undoable constraints, dialog singletons."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from pcbrouter.domain.geometry import BoundingBox
from pcbrouter.domain.units import mm_to_internal
from pcbrouter.settings import SettingsStore
from pcbrouter.ui.main_window import MainWindow
from pcbrouter.ui.shortcuts import typing_focus
from tests.integration.test_routing_ui import wait
from tests.integration.test_ui import make_window

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot: QtBot, tmp_path: Path) -> Iterator[MainWindow]:
    w = make_window(qtbot, SettingsStore(tmp_path / "config" / "settings.json"))
    yield w
    w.close()


def test_typing_focus_detects_text_entry(window: MainWindow) -> None:
    window.nets_panel.search.setFocus()
    assert typing_focus()
    window.canvas.setFocus()
    assert not typing_focus()


def test_single_letter_actions_yield_while_typing(
    window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    assert window.open_board(fixture_path("router_basic.kicad_pcb"))
    wait(window)
    window._on_net_selected("B")
    window.nets_panel.search.setFocus()
    assert typing_focus()
    assert window.routing_ui.route_selected_net() is False
    window.manual_draw._on_toggled(True)
    assert not window.manual_draw.active
    assert window.workbench.toggle_lock() is False
    assert window.workbench.reroute_section() is False
    zoom = window.canvas.px_per_mm
    window.act_fit.trigger()
    assert window.canvas.px_per_mm == zoom
    window.canvas.setFocus()
    assert window.routing_ui.route_selected_net() is True
    window.routing_ui.cancel()


def test_grid_toggle_keeps_state_while_typing(window: MainWindow) -> None:
    assert window.act_grid.isChecked()
    window.nets_panel.search.setFocus()
    window.act_grid.trigger()
    assert window.act_grid.isChecked(), "blocked toggle must not desync the checkbox"
    assert window.canvas.grid_visible


def test_corridor_edits_are_undoable(
    window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    assert window.open_board(fixture_path("router_basic.kicad_pcb"))
    wait(window)
    wb = window.bus.context.project.working
    assert wb is not None
    history = window.bus.context.history
    box = BoundingBox(
        mm_to_internal(1.0), mm_to_internal(1.0), mm_to_internal(2.0), mm_to_internal(2.0)
    )
    assert window.workbench.add_corridor(box=box, avoid=True) is True
    assert len(wb.corridors) == 1 and history.can_undo
    history.undo()
    window.workbench.on_working_changed()
    assert wb.corridors == []
    history.redo()
    window.workbench.on_working_changed()
    assert len(wb.corridors) == 1
    window.workbench.clear_corridors()
    assert wb.corridors == []
    history.undo()
    assert len(wb.corridors) == 1
    assert any(e.kind == "constraints" for e in history.entries())


def test_setup_dialogs_are_singletons(window: MainWindow) -> None:
    window.open_gpu_setup()
    first = window._gpu_setup_dialog
    window.open_gpu_setup()
    assert window._gpu_setup_dialog is first
    window.open_ollama_setup()
    ollama = window._ollama_dialog
    window.open_ollama_setup()
    assert window._ollama_dialog is ollama
    first.close()


def test_dialogs_scroll_and_undo_labels(
    window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    from PySide6.QtWidgets import QScrollArea

    from pcbrouter.ui.settings_dialog import SettingsDialog
    from pcbrouter.ui.workbench import NetConstraintsDialog

    assert "never touches geometry" not in window.act_undo.toolTip()
    assert "Ctrl+Y" in [s.toString() for s in window.act_redo.shortcuts()]
    dlg = SettingsDialog(window.settings, window.compute, window)
    for i in range(dlg.tabs.count()):
        if dlg.tabs.tabText(i) == "AI Providers":
            continue
        assert isinstance(dlg.tabs.widget(i), QScrollArea), dlg.tabs.tabText(i)
    dlg.close()
    assert window.open_board(fixture_path("router_basic.kicad_pcb"))
    wait(window)
    wb = window.bus.context.project.working
    assert wb is not None
    constraints = NetConstraintsDialog(wb, "B", window)
    assert constraints.findChild(QScrollArea) is not None
    constraints.close()
