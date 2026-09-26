"""GUI integration tests (pytest-qt, offscreen). These drive the real widgets."""

from __future__ import annotations

import hashlib
import shutil
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QKeySequence, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QMessageBox
from pytestqt.qtbot import QtBot

from pcbrouter import __version__
from pcbrouter.app.application import build_services
from pcbrouter.domain.units import internal_to_mm
from pcbrouter.settings import SettingsStore
from pcbrouter.ui import dialogs
from pcbrouter.ui.main_window import MainWindow
from pcbrouter.ui.pcb_canvas import ItemKind, PcbCanvas
from pcbrouter.ui.settings_dialog import SettingsDialog
from tests.fixtures.synthetic import generate_board

pytestmark = pytest.mark.gui


def make_window(qtbot: QtBot, store: SettingsStore) -> MainWindow:
    settings = store.load()
    services = build_services(settings, detect_gpu_now=False)
    window = MainWindow(
        bus=services.bus, compute=services.compute, settings=settings, settings_store=store
    )
    qtbot.addWidget(window)
    window.resize(1400, 900)
    window.show()
    qtbot.waitExposed(window)
    return window


@pytest.fixture
def store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(tmp_path / "config" / "settings.json")


@pytest.fixture
def window(qtbot: QtBot, store: SettingsStore) -> Iterator[MainWindow]:
    w = make_window(qtbot, store)
    yield w
    w.close()


def scene_to_view(canvas: PcbCanvas, x_mm: float, y_mm: float) -> QPoint:
    return canvas.mapFromScene(QPointF(x_mm, y_mm))


def click(qtbot: QtBot, canvas: PcbCanvas, x_mm: float, y_mm: float) -> None:
    qtbot.mouseClick(
        canvas.viewport(), Qt.MouseButton.LeftButton, pos=scene_to_view(canvas, x_mm, y_mm)
    )


def visible(canvas: PcbCanvas, kind: ItemKind, obj_id: str) -> bool:
    items = canvas.records_for(kind, obj_id)
    assert items, f"no canvas item for {kind} {obj_id}"
    return all(i.isVisible() for i in items)


def test_open_board_populates_every_panel(
    window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    assert window.open_board(fixture_path("four_layer.kicad_pcb"))
    stats = window.canvas.render_stats
    assert stats.by_kind["pad"] == 10 and stats.by_kind["track"] == 4
    assert stats.by_kind["via"] == 3 and stats.by_kind["component"] == 4
    assert stats.by_kind["outline"] == 1
    assert window.nets_panel.model.rowCount() == 5
    assert all(window.layers_panel.is_checked(n) for n in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    assert "four_layer.kicad_pcb" in window.lbl_board.text()
    assert "4 comp" in window.lbl_counts.text() and "3 via" in window.lbl_counts.text()
    assert window.canvas.active_layer == "F.Cu"
    top = window.project_panel.tree.topLevelItem(0)
    assert top is not None and top.text(0) == "four_layer.kicad_pcb"
    assert window.act_close.isEnabled()


def test_zoom_and_pan(
    qtbot: QtBot, window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    window.open_board(fixture_path("traces.kicad_pcb"))
    c = window.canvas
    fit = c.px_per_mm
    assert fit > 0
    c.zoom_by(2.0)
    assert c.px_per_mm == pytest.approx(fit * 2)
    wheel = QWheelEvent(
        QPointF(200, 200),
        c.mapToGlobal(QPointF(200, 200)),
        QPoint(0, 0),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    before = c.px_per_mm
    c.wheelEvent(wheel)
    assert c.px_per_mm > before
    window.act_fit.trigger()
    assert c.px_per_mm == pytest.approx(fit)

    c.zoom_by(8.0)
    h0, v0 = c.horizontalScrollBar().value(), c.verticalScrollBar().value()
    mid = Qt.MouseButton.MiddleButton
    none = Qt.KeyboardModifier.NoModifier
    c.mousePressEvent(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonPress, QPointF(300, 300), QPointF(300, 300), mid, mid, none
        )
    )
    c.mouseMoveEvent(
        QMouseEvent(
            QMouseEvent.Type.MouseMove,
            QPointF(250, 260),
            QPointF(250, 260),
            Qt.MouseButton.NoButton,
            mid,
            none,
        )
    )
    c.mouseReleaseEvent(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonRelease,
            QPointF(250, 260),
            QPointF(250, 260),
            mid,
            Qt.MouseButton.NoButton,
            none,
        )
    )
    assert (c.horizontalScrollBar().value(), c.verticalScrollBar().value()) == (h0 + 50, v0 + 40)


def test_space_left_drag_pans(
    qtbot: QtBot, window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    window.open_board(fixture_path("traces.kicad_pcb"))
    c = window.canvas
    c.zoom_by(8.0)
    c.setFocus()
    qtbot.keyPress(c, Qt.Key.Key_Space)
    h0 = c.horizontalScrollBar().value()
    left, none = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    c.mousePressEvent(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPointF(300, 300),
            QPointF(300, 300),
            left,
            left,
            none,
        )
    )
    c.mouseMoveEvent(
        QMouseEvent(
            QMouseEvent.Type.MouseMove,
            QPointF(270, 300),
            QPointF(270, 300),
            Qt.MouseButton.NoButton,
            left,
            none,
        )
    )
    c.mouseReleaseEvent(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonRelease,
            QPointF(270, 300),
            QPointF(270, 300),
            left,
            Qt.MouseButton.NoButton,
            none,
        )
    )
    qtbot.keyRelease(c, Qt.Key.Key_Space)
    assert c.horizontalScrollBar().value() == h0 + 30
    assert window.inspector.title == "Nothing selected"  # panning did not select


def test_layer_visibility_toggle(window: MainWindow, fixture_path: Callable[[str], Path]) -> None:
    window.open_board(fixture_path("four_layer.kicad_pcb"))
    board = window.canvas.board
    assert board is not None
    front_track = next(t for t in board.tracks if t.layer == "F.Cu")
    tht_pad = board.index.components_by_ref["J1"].footprint.pads[0]
    window.layers_panel.set_checked("F.Cu", False)
    assert not visible(window.canvas, ItemKind.TRACK, front_track.id)
    assert visible(window.canvas, ItemKind.PAD, tht_pad.id)  # still on other layers
    for layer in ("In1.Cu", "In2.Cu", "B.Cu"):
        window.layers_panel.set_checked(layer, False)
    assert not visible(window.canvas, ItemKind.PAD, tht_pad.id)
    window.layers_panel.set_checked("F.Cu", True)
    assert visible(window.canvas, ItemKind.TRACK, front_track.id)
    window.layers_panel.set_checked("Edge.Cuts", False)
    assert not visible(window.canvas, ItemKind.OUTLINE, "board-outline")


def test_active_layer_selection(
    qtbot: QtBot, window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    window.open_board(fixture_path("four_layer.kicad_pcb"))
    item = window.layers_panel._items["In2.Cu"]
    window.layers_panel.tree.itemClicked.emit(item, 0)
    assert window.canvas.active_layer == "In2.Cu"
    assert window.lbl_layer.text() == "Layer In2.Cu"


def test_nets_highlight_isolate_hide(
    window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    window.open_board(fixture_path("four_layer.kicad_pcb"))
    c, np_ = window.canvas, window.nets_panel
    board = c.board
    assert board is not None
    data_track = board.index.tracks_by_net["/DATA"][0]
    gnd_track = board.index.tracks_by_net["GND"][0]

    np_.search.setText("dat")
    assert np_.proxy.rowCount() == 1
    np_.search.clear()

    np_.select_net("/DATA")
    assert c.highlighted_net == "/DATA"
    assert window.inspector.title == "Net /DATA"
    assert window.inspector.row_values()["Vias"] == "1"
    gnd_item = c.records_for(ItemKind.TRACK, gnd_track.id)[0]
    assert gnd_item.opacity() < 0.5  # dimmed

    np_.btn_isolate.click()
    assert c.isolated_net == "/DATA"
    assert visible(c, ItemKind.TRACK, data_track.id)
    assert not visible(c, ItemKind.TRACK, gnd_track.id)
    row = np_.model.row_of("/DATA")
    assert np_.model.index(row, 6).data() == "isolated"
    np_.btn_clear_iso.click()
    assert c.isolated_net is None and visible(c, ItemKind.TRACK, gnd_track.id)

    np_.select_net("GND")
    np_.btn_hide.click()
    assert not visible(c, ItemKind.TRACK, gnd_track.id)
    assert np_.model.index(np_.model.row_of("GND"), 6).data() == "hidden"
    np_.btn_show_all.click()
    assert visible(c, ItemKind.TRACK, gnd_track.id)


def test_click_to_inspect_pad_track_via_component(
    qtbot: QtBot, window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    window.open_board(fixture_path("four_layer.kicad_pcb"))
    c, insp = window.canvas, window.inspector
    c.zoom_by(3.0)
    c.centerOn(145, 120)

    click(qtbot, c, 140.0, 125.08)  # J1 pad 3 (THT)
    assert insp.title == "Pad J1.3"
    rows = insp.row_values()
    assert rows["Net"] == "/DATA" and rows["Drill"] == "1 mm" and rows["Type"] == "thru_hole"
    assert rows["Position"] == "(140.0000, 125.0800) mm"

    click(qtbot, c, 144.0, 125.08)  # In2.Cu track between J1.3 and the via
    assert insp.title == "Track"
    rows = insp.row_values()
    assert rows["Layer"] == "In2.Cu" and rows["Width"] == "0.2 mm" and rows["Length"] == "8 mm"

    click(qtbot, c, 148.0, 125.08)  # via (vias win over tracks)
    assert insp.title == "Via"
    rows = insp.row_values()
    assert rows["Diameter"] == "0.6 mm" and rows["Drill"] == "0.3 mm"
    assert (rows["Start layer"], rows["End layer"]) == ("F.Cu", "B.Cu")

    click(qtbot, c, 160.0, 125.0)  # U1 body centre (no pad there)
    assert insp.title == "Component U1"
    rows = insp.row_values()
    assert rows["Value"] == "SENSOR" and rows["Rotation"] == "45°" and rows["Side"] == "Front"

    click(qtbot, c, 168.0, 108.0)  # empty board area
    assert insp.title == "Nothing selected"


def test_unknown_values_are_shown_as_unknown(
    window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    window.open_board(fixture_path("four_layer.kicad_pcb"))
    board = window.canvas.board
    assert board is not None
    u1 = board.index.components_by_ref["U1"]
    window.inspector.show_object(board, ItemKind.COMPONENT, u1.id)
    trapezoid = u1.footprint.pads[3]
    window.inspector.show_object(board, ItemKind.PAD, trapezoid.id)
    assert "drawn as rectangle" in window.inspector.row_values()["Shape"]
    window.inspector.show_rows("t", [("Thing", None)])
    assert window.inspector.row_values()["Thing"] == "unknown"


def test_hover_reports_coordinates_and_object(
    qtbot: QtBot, window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    window.open_board(fixture_path("vias.kicad_pcb"))
    c = window.canvas
    pos = scene_to_view(c, 113.0, 100.0)
    qtbot.mouseMove(c.viewport(), pos + QPoint(25, 25))  # ensure the next move is a real move
    with qtbot.waitSignal(c.cursorMoved) as blocker:
        qtbot.mouseMove(c.viewport(), pos)
    x, y = blocker.args
    assert x == pytest.approx(113.0, abs=0.5) and y == pytest.approx(100.0, abs=0.5)
    assert "X" in window.lbl_cursor.text() and "mm" in window.lbl_cursor.text()
    assert c._hover_key is not None and c._hover_key[0] is ItemKind.VIA
    assert "Via" in c.describe(*c._hover_key)


def test_component_list_selects_on_canvas(
    window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    window.open_board(fixture_path("two_components.kicad_pcb"))
    board = window.canvas.board
    assert board is not None
    window.project_panel.componentActivated.emit(board.index.components_by_ref["C1"].id)
    assert window.inspector.title == "Component C1"
    window.project_panel.filter.setText("R")
    root = window.project_panel._components_root
    assert root is not None
    hidden = [root.child(i).isHidden() for i in range(root.childCount())]  # type: ignore[union-attr]
    assert hidden.count(False) == 1


def test_close_board_is_read_only(
    window: MainWindow, tmp_path: Path, fixture_path: Callable[[str], Path]
) -> None:
    path = tmp_path / "gui.kicad_pcb"
    shutil.copy2(fixture_path("four_layer.kicad_pcb"), path)
    before = hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns
    window.open_board(path)
    window.nets_panel.select_net("GND")
    window.nets_panel.btn_isolate.click()
    window.canvas.zoom_by(4.0)
    window.act_close.trigger()
    assert window.canvas.board is None and window.canvas.render_stats.item_count == 0
    assert window.nets_panel.model.rowCount() == 0
    assert "verified unchanged" in window.statusBar().currentMessage()
    assert window.lbl_board.text() == "No board"
    after = hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns
    assert after == before


def test_open_errors_show_readable_dialog(
    window: MainWindow,
    tmp_path: Path,
    fixture_path: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        dialogs,
        "show_error",
        lambda _p, title, msg, details=None: shown.append((title, msg)),
    )
    assert not window.open_board(fixture_path("malformed_unbalanced.kicad_pcb"))
    assert not window.open_board(tmp_path / "missing.kicad_pcb")
    wrong = tmp_path / "x.kicad_pro"
    wrong.write_text("{}")
    assert not window.open_board(wrong)
    assert [t for t, _ in shown] == ["Could not open board"] * 3
    assert "line" in shown[0][1] and "not found" in shown[1][1] and "project file" in shown[2][1]
    assert window.canvas.board is None


def test_failed_open_keeps_current_board(
    window: MainWindow, fixture_path: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dialogs, "show_error", lambda *a, **k: None)
    window.open_board(fixture_path("traces.kicad_pcb"))
    window.open_board(fixture_path("malformed_not_a_board.kicad_pcb"))
    assert window.canvas.board is not None
    assert "traces.kicad_pcb" in window.lbl_board.text()


def test_placeholders_say_available_in_a_later_stage(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "information", lambda _p, title, text, *a: messages.append(f"{title}\n{text}")
    )
    # Stages 4/5 made routing real: no routing placeholder remains.
    for act in (window.act_route_net, window.act_route_board):
        assert "later stage" not in act.text()
        assert not act.isEnabled()  # disabled (not faked) until a board is open
    assert not messages
    # Stage 2 activated AI configuration: it is no longer a placeholder.
    assert "later stage" not in window.act_ai.text()


def test_shortcuts(window: MainWindow) -> None:
    expected = {
        window.act_open: "Ctrl+O",
        window.act_close: "Ctrl+W",
        window.act_fit: "F",
        window.act_grid: "G",
        window.act_settings: "Ctrl+,",
    }
    for act, seq in expected.items():
        assert act.shortcut() == QKeySequence(seq), act.text()
    save = QKeySequence("Ctrl+S")
    all_shortcuts = [s for a in window.findChildren(type(window.act_open)) for s in a.shortcuts()]
    assert save not in all_shortcuts  # saving is intentionally disabled in Stage 1


def test_grid_toggle(window: MainWindow) -> None:
    assert window.canvas.grid_visible
    window.act_grid.trigger()
    assert not window.canvas.grid_visible and not window.settings.viewer.grid_visible
    window.act_grid.trigger()
    assert window.canvas.grid_visible


def test_settings_persist_across_restart(
    qtbot: QtBot, store: SettingsStore, fixture_path: Callable[[str], Path]
) -> None:
    first = make_window(qtbot, store)
    first.open_board(fixture_path("vias.kicad_pcb"))
    first.act_grid.trigger()  # grid off
    first.docks["log"].hide()
    first.settings.viewer.grid_spacing_mm = 0.5
    first.close()
    assert store.path.exists()

    reloaded = store.load()
    assert reloaded.viewer.grid_visible is False
    assert reloaded.viewer.grid_spacing_mm == 0.5
    assert reloaded.window.panel_visibility["log"] is False
    assert reloaded.recent_boards[0].endswith("vias.kicad_pcb")
    assert reloaded.last_open_directory == str(fixture_path("vias.kicad_pcb").resolve().parent)
    assert reloaded.window.geometry_b64

    second = make_window(qtbot, store)
    assert not second.canvas.grid_visible and not second.act_grid.isChecked()
    assert second.docks["log"].isHidden()
    recent = [a.text() for a in second.recent_menu.actions()]
    assert any("vias.kicad_pcb" in t for t in recent)
    second.close()


def test_missing_recent_board_is_removed(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dialogs, "show_error", lambda *a, **k: None)
    ghost = str(tmp_path / "gone.kicad_pcb")
    window.settings.add_recent_board(ghost)
    window._rebuild_recent_menu()
    window._open_recent(ghost)
    assert ghost not in window.settings.recent_boards


def test_settings_dialog(qtbot: QtBot, window: MainWindow) -> None:
    dlg = SettingsDialog(window.settings, window.compute, window)
    qtbot.addWidget(dlg)
    tabs = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
    assert tabs == [
        "General",
        "Viewer",
        "Compute",
        "AI Providers",
        "Routing",
        "GPU",
        "Geometry",
        "Export",
    ]
    dlg.grid_spacing.setValue(2.54)
    dlg.grid_visible.setChecked(False)
    result = dlg.result_settings()
    assert result.viewer.grid_spacing_mm == pytest.approx(2.54)
    assert window.settings.viewer.grid_spacing_mm == 1.0  # original untouched until accepted
    model = dlg.backend_combo.model()
    assert not model.item(1).isEnabled()  # type: ignore[attr-defined]
    # Stage 2: the AI tab hosts the real provider configuration.
    assert dlg.ai_widget.list.count() == 0
    assert "Add a provider profile" in dlg.ai_widget.kind_label.text()


def test_about_and_compute_info_text(window: MainWindow) -> None:
    about = dialogs.about_text()
    assert __version__ in about and "source board file is never modified" in about
    assert "not KiCad DRC" in about and "working copy" in about
    info = dialogs.compute_info_text(window.compute)
    assert "Active compute backend: CPU" in info
    assert "Threads:" in info and "GPU candidate" in info and "Array library:" in info


def test_gpu_detection_updates_status_bar(window: MainWindow) -> None:
    from pcbrouter.compute import GpuDetectionResult, GpuDevice, GpuStatus

    assert "detecting" in window.lbl_backend.text()
    window.on_gpu_detected(
        GpuDetectionResult(
            GpuStatus.GPU_LIBRARIES_NOT_INSTALLED, (GpuDevice("RTX 4070", "NVIDIA"),)
        )
    )
    assert "CUDA device, no GPU libs" in window.lbl_backend.text()
    assert "RTX 4070" in dialogs.compute_info_text(window.compute)


def test_log_panel_shows_records(window: MainWindow) -> None:
    import logging

    from pcbrouter.app_logging import add_handler, remove_handler

    add_handler(window.log_panel.handler)
    try:
        logging.getLogger("pcbrouter.test").warning("hello api_key=abcdef123456")
    finally:
        remove_handler(window.log_panel.handler)
    text = window.log_panel.text.toPlainText()
    assert "hello" in text and "abcdef123456" not in text


def test_large_board_stays_interactive(qtbot: QtBot, window: MainWindow, tmp_path: Path) -> None:
    text, counts = generate_board(40, 40)
    path = tmp_path / "large.kicad_pcb"
    path.write_text(text, encoding="utf-8")
    assert window.open_board(path)
    stats = window.canvas.render_stats
    assert stats.by_kind["pad"] == counts.pads and stats.by_kind["via"] == counts.vias
    assert stats.by_kind["track"] == counts.tracks
    c = window.canvas
    c.zoom_by(10.0)
    start = time.perf_counter()
    for i in range(200):
        c._pick(QPointF(100 + i % 50, 100 + i // 4))
    per_pick_ms = (time.perf_counter() - start) / 200 * 1e3
    assert per_pick_ms < 20  # generous: hover must never rebuild or scan the scene
    start = time.perf_counter()
    window.layers_panel.set_checked("B.Cu", False)
    window.nets_panel.select_net("N100")
    assert time.perf_counter() - start < 2.0
    board = c.board
    assert board is not None
    track = board.index.tracks_by_net["N100"][0]
    x = internal_to_mm((track.start.x + track.end.x) // 2)
    assert x > 0


def test_display_overlays_follow_settings(
    qtbot: QtBot, store: SettingsStore, fixture_path: Callable[[str], Path]
) -> None:
    settings = store.load()
    settings.viewer.show_footprint_bodies = False
    store.save(settings)
    w = make_window(qtbot, store)
    w.open_board(fixture_path("two_components.kicad_pcb"))
    board = w.canvas.board
    assert board is not None
    r1 = board.index.components_by_ref["R1"]
    assert not visible(w.canvas, ItemKind.COMPONENT, r1.id)
    assert not w.layers_panel.is_overlay_checked("footprints")
    assert w.layers_panel.is_overlay_checked("labels")
    w.close()


def test_dimmed_objects_remain_clickable(
    qtbot: QtBot, window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    window.open_board(fixture_path("vias.kicad_pcb"))
    window.nets_panel.select_net("GND")
    assert window.canvas.highlighted_net == "GND"
    click(qtbot, window.canvas, 113.0, 100.0)  # a /SIG via, dimmed by the GND highlight
    assert window.inspector.title == "Via"
    assert window.canvas.highlighted_net is None


def test_gpu_mode_without_device_shows_skipped(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pcbrouter.compute import probe
    from pcbrouter.settings.settings import ComputeBackendChoice

    window.gpu_probe = probe.GpuProbe(False, None, (), "no CUDA or oneAPI GPU device found")
    window.settings.default_compute_backend = ComputeBackendChoice.AUTO
    window._update_backend_label()
    assert "GPU: SKIPPED" in window.lbl_backend.text()
    assert "no CUDA or oneAPI GPU device found" in window.lbl_backend.toolTip()


@pytest.mark.parametrize("choice", ["cpu", "gpu", "auto"])
def test_window_starts_with_every_saved_backend(qtbot: QtBot, tmp_path: Path, choice: str) -> None:
    """Regression: a saved GPU/AUTO backend crashed the window at startup."""
    from pcbrouter.settings.settings import ComputeBackendChoice

    store = SettingsStore(tmp_path / "config" / "settings.json")
    settings = store.load()
    settings.default_compute_backend = ComputeBackendChoice(choice)
    store.save(settings)
    w = make_window(qtbot, store)
    assert "GPU:" in w.lbl_backend.text()
    assert w.open_board(
        Path(__file__).parents[1] / "fixtures" / "boards" / "router_basic.kicad_pcb"
    )
    w.close()
