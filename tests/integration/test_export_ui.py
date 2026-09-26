"""Stage 9 UI: export (DRC gate), overwrite policy, sessions, crash recovery, bundle."""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from pcbrouter.project.session_store import recovery_path
from pcbrouter.settings import SettingsStore
from pcbrouter.ui.main_window import MainWindow
from tests.integration.test_routing_ui import wait
from tests.integration.test_ui import make_window

pytestmark = pytest.mark.gui
BOARDS = Path(__file__).parent.parent / "fixtures" / "boards"


@pytest.fixture
def window(qtbot: QtBot, tmp_path: Path) -> Iterator[MainWindow]:
    w = make_window(qtbot, SettingsStore(tmp_path / "config" / "settings.json"))
    w.export_ui.inform = lambda title, text: None
    yield w
    w.close()


def board_copy(tmp_path: Path) -> Path:
    for ext in (".kicad_pcb", ".kicad_pro"):
        (tmp_path / f"router_basic{ext}").write_bytes((BOARDS / f"router_basic{ext}").read_bytes())
    return tmp_path / "router_basic.kicad_pcb"


def route_and_accept(w: MainWindow, net: str) -> None:
    w._on_net_selected(net)
    assert w.routing_ui.route_selected_net()
    wait(w)
    assert w.routing_ui.accept(w.routing_ui.panel.current)
    wait(w)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_export_menu_gate_and_source_safety(window: MainWindow, tmp_path: Path) -> None:
    src = board_copy(tmp_path)
    before = sha(src)
    ui = window.export_ui
    assert not ui.act_export.isEnabled()
    assert window.open_board(src)
    wait(window)
    assert ui.act_export.isEnabled() and ui.act_export.shortcut().toString() == "Ctrl+E"
    assert not ui.act_overwrite.isEnabled() and window.bus.read_only
    route_and_accept(window, "A")
    out = tmp_path / "router_basic_routed.kicad_pcb"
    ui.save_path = lambda *_: out
    assert ui.export_routed()  # runs in the routing worker
    wait(window)
    assert out.exists() and ui.last_export.ok and sha(src) == before
    # exporting onto the source is refused from the export action
    assert not ui.export_routed(src) and sha(src) == before
    # overwrite is disabled by default policy
    assert not ui.overwrite_source() and sha(src) == before
    # DRC gate: an injected short needs an explicit "export unverified" answer
    from pcbrouter.domain.track import Track
    from pcbrouter.routing.working_board import Provenance

    wb = window.bus.context.project.working
    a = next(p for p in wb.board.pads if p.net_name == "A")
    b = next(p for p in wb.board.pads if p.net_name == "B")
    wb.commit_objects(
        [Track("short", a.position, b.position, 250_000, "F.Cu", "A")],
        [],
        (),
        "inject",
        Provenance.USER_ACCEPTED,
        validate=False,
    )
    bad = tmp_path / "bad.kicad_pcb"
    ui.ask = lambda *_: False
    assert ui.export_routed(bad)
    wait(window)
    assert not bad.exists() and ui.last_export.status.value == "EXPORT_BLOCKED_DRC"
    ui.ask = lambda *_: True
    assert ui.export_routed(bad)
    wait(window)  # blocked → confirmed → second (unverified) export job
    assert bad.exists()
    assert "UNVERIFIED" in ui.last_export.verification


def test_overwrite_when_allowed_makes_backup(window: MainWindow, tmp_path: Path) -> None:
    src = board_copy(tmp_path)
    original = src.read_bytes()
    window.settings.export.allow_overwrite_source = True
    window.export_ui.apply_settings()
    assert not window.bus.read_only
    assert window.open_board(src)
    wait(window)
    route_and_accept(window, "A")
    window.export_ui.ask = lambda *_: True
    assert window.export_ui.overwrite_source()
    wait(window)
    report = window.export_ui.last_export
    assert report.backup is not None and report.backup.read_bytes() == original
    assert src.read_bytes() != original


def test_session_save_load_and_crash_recovery(window: MainWindow, tmp_path: Path) -> None:
    src = board_copy(tmp_path)
    ui = window.export_ui
    assert window.open_board(src)
    wait(window)
    route_and_accept(window, "A")
    project = window.bus.context.project
    fp = project.working.fingerprint
    rec = recovery_path(project.session.workspace.root)
    ui.autosave_now()
    assert rec.exists()
    assert "api_key" not in rec.read_text(encoding="utf-8").lower()
    saved = tmp_path / "s.pcbrouter-session.json"
    assert ui.save_session(saved) and saved.exists()
    # simulate a crash: the recovery file stays; reopen the same file → offered
    window.bus.context.project.close_board()
    assert rec.exists()
    ui.ask = lambda *_: True
    assert window.open_board(src)
    wait(window)
    assert project.working.fingerprint == fp  # recovered, validated
    assert window.bus.context.history.can_undo  # recovery is undoable
    window.bus.context.history.undo()
    assert not project.working.modified
    assert ui.load_session(saved) and project.working.fingerprint == fp
    # a clean close with unexported work keeps the recovery file; after export it goes
    window.close_board()
    assert rec.exists()
    ui.ask = lambda *_: False  # declining deletes it
    assert window.open_board(src)
    wait(window)
    assert not rec.exists()
    route_and_accept(window, "A")
    ui.save_path = lambda *_: tmp_path / "exp.kicad_pcb"
    assert ui.export_routed()
    wait(window)
    window.close_board()
    assert not rec.exists()


def test_recovery_for_changed_source_is_not_offered(window: MainWindow, tmp_path: Path) -> None:
    src = board_copy(tmp_path)
    assert window.open_board(src)
    wait(window)
    route_and_accept(window, "A")
    rec = recovery_path(window.bus.context.project.session.workspace.root)
    window.export_ui.autosave_now()
    window.bus.context.project.close_board()
    src.write_bytes(src.read_bytes() + b"\n")  # edited in KiCad meanwhile
    asked: list[str] = []
    window.export_ui.ask = lambda title, _t: asked.append(title) or True
    assert window.open_board(src)
    wait(window)
    assert not asked and not window.bus.context.project.working.modified
    assert rec.exists()  # kept (not deleted) but never applied to a different file


def test_diagnostic_bundle_has_no_board_or_keys(window: MainWindow, tmp_path: Path) -> None:
    src = board_copy(tmp_path)
    assert window.open_board(src)
    wait(window)
    out = window.export_ui.export_bundle(tmp_path / "diag.zip")
    assert out is not None
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        text = z.read("diagnostics.json").decode("utf-8")
    assert names == ["diagnostics.json", "log_tail.txt"]
    data = json.loads(text)
    assert data["reproducibility"]["source_sha256"] == sha(src)
    assert "kicad_pcb (version" not in text and "sk-" not in text
    assert "recent_boards" not in data["settings"]
