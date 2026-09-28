"""Manual trace/via drawing: validated commits, undo/redo, canvas draw mode."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from pcbrouter.commands.route_commands import CommitManualCopperCommand
from pcbrouter.domain.geometry import Point
from pcbrouter.domain.units import mm_to_internal
from pcbrouter.routing.manual import build_manual_tracks, build_manual_via, new_run_id
from pcbrouter.routing.working_board import Provenance
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


def test_manual_commit_and_undo(window: MainWindow, fixture_path: Callable[[str], Path]) -> None:
    """Hand-drawn copper commits validated, then undo/redo restores state."""
    assert window.open_board(fixture_path("router_basic.kicad_pcb"))
    wait(window)
    wb = window.bus.context.project.working
    assert wb is not None
    before = wb.board.fingerprint
    run = new_run_id()
    tracks = build_manual_tracks(
        "B",
        [
            Point(mm_to_internal(20.0), mm_to_internal(10.0)),
            Point(mm_to_internal(23.0), mm_to_internal(10.0)),
        ],
        mm_to_internal(0.25),
        "F.Cu",
        run,
    )
    via = build_manual_via(
        "B",
        Point(mm_to_internal(21.5), mm_to_internal(10.0)),
        mm_to_internal(0.8),
        mm_to_internal(0.4),
        "F.Cu",
        "B.Cu",
        run,
    )
    result = window.bus.dispatch(CommitManualCopperCommand("B", tuple(tracks), (via,)))
    assert result.success, result.message
    wait(window)
    assert wb.board.fingerprint != before
    assert wb.provenance_of(tracks[0].id) is Provenance.USER_ACCEPTED
    assert any(t.net_name == "B" and t.id == tracks[0].id for t in wb.board.tracks)
    assert any(v.net_name == "B" and v.id == via.id for v in wb.board.vias)
    window.bus.context.history.undo()
    wait(window)
    assert all(t.id != tracks[0].id for t in wb.board.tracks)
    window.bus.context.history.redo()
    wait(window)
    assert any(t.id == tracks[0].id for t in wb.board.tracks)


def test_manual_commit_refuses_illegal_copper(
    window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    """A trace through another net's pad is refused, changing nothing."""
    assert window.open_board(fixture_path("router_basic.kicad_pcb"))
    wait(window)
    wb = window.bus.context.project.working
    assert wb is not None
    before = wb.board.fingerprint
    # straight through net A's pad at (5, 10)
    tracks = build_manual_tracks(
        "B",
        [
            Point(mm_to_internal(4.0), mm_to_internal(10.0)),
            Point(mm_to_internal(6.0), mm_to_internal(10.0)),
        ],
        mm_to_internal(0.25),
        "F.Cu",
        new_run_id(),
    )
    result = window.bus.dispatch(CommitManualCopperCommand("B", tuple(tracks), ()))
    assert not result.success
    assert wb.board.fingerprint == before


def test_draw_tool_click_finish_cycle(
    window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    """Controller: select net, click points, finish commits a track."""
    assert window.open_board(fixture_path("router_basic.kicad_pcb"))
    wait(window)
    wb = window.bus.context.project.working
    assert wb is not None
    window._on_net_selected("B")
    assert window.routing_ui.selected_net() == "B"
    n_before = len(wb.board.tracks)
    assert window.manual_draw.start()
    assert window.canvas.draw_mode
    window.manual_draw._on_click(20.0, 10.0)
    window.manual_draw._on_click(23.0, 10.0)
    assert window.manual_draw.finish()
    wait(window)
    assert len(wb.board.tracks) == n_before + 1
    assert wb.board.tracks[-1].net_name == "B"
    assert not window.canvas.draw_mode
    assert not window.manual_draw.active


def test_canvas_draw_mode_clicks_emit_signal(
    window: MainWindow, fixture_path: Callable[[str], Path], qtbot: QtBot
) -> None:
    """In draw mode left-clicks emit drawClicked instead of selecting."""
    from PySide6.QtCore import QPoint

    assert window.open_board(fixture_path("router_basic.kicad_pcb"))
    wait(window)
    seen: list[tuple[float, float]] = []
    window.canvas.drawClicked.connect(lambda x, y: seen.append((x, y)))
    selected: list[tuple[ItemKind, str]] = []
    window.canvas.objectSelected.connect(lambda k, i: selected.append((k, i)))
    window.canvas.set_draw_mode(True)
    try:
        viewport = window.canvas.viewport()
        qtbot.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=QPoint(60, 60))
        assert len(seen) == 1
        assert not selected
    finally:
        window.canvas.set_draw_mode(False)
