"""Stage 3 desktop integration: engine build, Internal Geometry Check, rules, tools."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from pcbrouter.drc.result import DRCStatus
from pcbrouter.routing.collision import ValidationStatus
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


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def open_ready(window: MainWindow, path: Path) -> None:
    assert window.open_board(path)
    assert window.engine_ui.wait_until_ready()
    assert window.engine_ui.engine is not None


def test_status_bar_and_engine_ready(
    window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    ui = window.engine_ui
    assert ui.lbl_routing.text() == "Routing: Unavailable until Stage 4" or "Routing" in (
        ui.lbl_routing.text()
    )
    assert not ui.act_run_check.isEnabled()
    open_ready(window, fixture_path("stage3_rules.kicad_pcb"))
    assert ui.lbl_geometry.text() == "Geometry: Ready"
    assert ui.lbl_rules.text().startswith("Rules: Loaded")
    assert ui.lbl_drc.text() == "Internal DRC: Not run"
    assert ui.act_run_check.isEnabled()
    stats = window.project_panel.geometry_stats()
    assert stats["Copper layers"] == "2" and "Nets unrouted" in stats
    assert stats["Internal DRC errors"] == "not run"


def test_internal_geometry_check_panel(
    window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    path = fixture_path("stage3_rules.kicad_pcb")
    before = _sha(path)
    open_ready(window, path)
    ui = window.engine_ui
    ui.run_geometry_check()
    assert ui.wait_until_ready()
    result = ui.drc_panel.result
    assert result is not None and result.status is DRCStatus.FAIL
    assert len(result.errors) == 6
    assert ui.lbl_drc.text() == "Internal DRC: Fail"
    assert "KiCad DRC" not in ui.lbl_drc.text()
    assert "Internal Geometry Check" in ui.drc_panel.summary.text()
    assert ui.overlays.has("drc")
    # filters
    ui.drc_panel.show_warnings.setChecked(False)
    ui.drc_panel.show_info.setChecked(False)
    assert len(ui.drc_panel.visible_violations()) == 6
    ui.drc_panel.net_filter.setText("VBAT")
    assert all(
        "VBAT" in (v.net_a or "") + (v.net_b or "") for v in ui.drc_panel.visible_violations()
    )
    ui.drc_panel.net_filter.setText("")
    # click-to-locate zooms in and highlights
    zoom = window.canvas.px_per_mm
    v = ui.drc_panel.select_row(0)
    assert v is not None and ui.overlays.has("drc_focus")
    assert window.canvas.px_per_mm >= zoom
    assert window.project_panel.geometry_stats()["Internal DRC errors"] == "6"
    assert _sha(path) == before


def test_rules_inspector_shows_sources(
    window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    open_ready(window, fixture_path("stage3_rules.kicad_pcb"))
    panel = window.engine_ui.rules_panel
    assert "Board Rules" in panel.html() and "Unsupported deterministic rule" in panel.html()
    assert panel.show_net("VBAT")
    html = panel.html()
    assert "Power" in html and "Net class" in html and "Clearance" in html


def test_validate_test_segment_and_via(
    window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    path = fixture_path("stage3_rules.kicad_pcb")
    before = _sha(path)
    open_ready(window, path)
    ui = window.engine_ui
    board = window.canvas.board
    assert board is not None
    n_tracks = len(board.tracks)
    dlg = ui.open_test_segment()
    assert dlg is not None
    dlg.set_net("SIG")
    dlg.layer_combo.setCurrentText("F.Cu")
    dlg.set_points((39.9, 1.0), (45.0, 1.0))  # leaves the board
    assert dlg.validate().status is ValidationStatus.INVALID
    assert ui.overlays.has("candidate")
    assert "NOT added to the board" in dlg.result_view.toPlainText()
    dlg.set_points((2.0, 2.0), (6.0, 2.0))
    res = dlg.validate()
    assert res.status in (ValidationStatus.VALID, ValidationStatus.VALID_WITH_WARNINGS), res
    dlg.width_spin.setValue(0.05)  # below the 0.15 mm minimum
    assert dlg.validate().status is ValidationStatus.INVALID
    via = ui.open_test_via()
    assert via is not None
    via.set_net("GND")
    via.pos_x.setValue(4.0)
    via.pos_y.setValue(26.0)  # keepout B forbids vias
    assert via.validate().status is ValidationStatus.INVALID
    dlg.close()
    assert not ui.overlays.has("candidate")
    assert len(board.tracks) == n_tracks  # nothing was added to the board
    assert _sha(path) == before


def test_envelope_debug_overlays_and_grid(
    window: MainWindow, fixture_path: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    open_ready(window, fixture_path("stage3_rules.kicad_pcb"))
    ui = window.engine_ui
    board = window.canvas.board
    assert board is not None
    track = board.tracks[0]
    window.canvas.select_object(ItemKind.TRACK, track.id)
    window.canvas.objectSelected.emit(ItemKind.TRACK, track.id)
    ui.act_envelope.setChecked(True)
    assert ui.overlays.has("envelope")
    for key, act in ui.debug_actions.items():
        act.setChecked(True)
        ui.wait_until_ready()
        assert ui.overlays.has(f"debug:{key}"), key
    assert ui.last_occupancy is not None and ui.last_congestion is not None
    ui.clear_overlays()
    assert not ui.overlays.names
    monkeypatch.setattr(window, "run_dialog", lambda dlg: 1)
    ui.open_routing_grid()
    ui.wait_until_ready()
    assert ui.overlays.has("debug:grid")
    # a board reload drops every overlay safely
    window.close_board()
    assert not ui.overlays.names and ui.engine is None


def test_overrides_exports_and_diagnostics(
    window: MainWindow,
    fixture_path: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_ready(window, fixture_path("stage3_rules.kicad_pcb"))
    ui = window.engine_ui
    from pcbrouter.rules.overrides import NetOverride, RuleOverrides

    ui.save_overrides(RuleOverrides(nets={"SIG": NetOverride(width=400_000)}))
    assert ui.wait_until_ready()
    assert ui.engine is not None
    assert ui.engine.resolver.resolve_trace_width("SIG").value == 400_000
    snap = tmp_path / "rules_snapshot.json"
    assert ui.write_rules_snapshot(snap)
    data = json.loads(snap.read_text())
    assert data["nets"]["SIG"]["preferred_width"]["mm"] == pytest.approx(0.4)
    summary = tmp_path / "geometry_summary.json"
    assert ui.write_geometry_summary(summary)
    gs = json.loads(summary.read_text())
    assert gs["counts"] and gs["objects"] and "points" not in json.dumps(gs)
    errors: list[str] = []
    monkeypatch.setattr(
        "pcbrouter.ui.geometry_controller.dialogs.show_error",
        lambda _p, title, *_a: errors.append(title),
    )
    assert not ui.write_rules_snapshot(tmp_path / "x.kicad_pcb")  # refused
    assert errors == ["Export refused"]
    diag = ui.diagnostics()
    for key in ("board_fingerprint", "geometry_engine_version", "rule_engine_version",
                "object_counts", "spatial_index", "last_drc_status"):  # fmt: skip
        assert key in diag
    monkeypatch.setattr(window, "run_dialog", lambda dlg: 0)
    ui.show_diagnostics()


def test_conservative_setting_propagates(
    window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    open_ready(window, fixture_path("stage3_unsupported.kicad_pcb"))
    ui = window.engine_ui
    assert ui.engine is not None and ui.engine.config.conservative
    window.settings.geometry.conservative_rules = False
    ui.apply_settings()
    assert ui.wait_until_ready()
    assert ui.engine is not None and not ui.engine.config.conservative
