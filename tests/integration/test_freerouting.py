"""Freerouting engine integration, with stand-ins for KiCad's Python (fake pcbnew)
and for Freerouting (a script routing with our own router). Real-tool runs happen
in Windows CI (Freerouting jar) and on a KiCad machine; see docs/freerouting.md."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import ClassVar

import pytest

from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.routing.board_router import BoardRouter, BoardRouterSettings, BoardStatus
from pcbrouter.routing.freerouting import (
    FreeroutingError,
    FreeroutingTool,
    find_freerouting,
    freeroute,
    result_from_routed,
    run_freerouting,
)
from pcbrouter.routing.result import RouteStatus
from pcbrouter.routing.working_board import WorkingBoard

ROOT = Path(__file__).resolve().parents[2]
BOARDS = ROOT / "tests" / "fixtures" / "boards"
FAKE_FR = ROOT / "tests" / "support" / "fake_freerouting.py"
FAKE_PCBNEW = ROOT / "tests" / "support" / "fake_pcbnew"


@pytest.fixture
def fake_tools(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("PCBROUTER_KICAD_PYTHON", sys.executable)
    monkeypatch.setenv("PCBROUTER_FREEROUTING", str(FAKE_FR))
    path = os.pathsep.join([str(FAKE_PCBNEW), str(ROOT / "src"), os.environ.get("PYTHONPATH", "")])
    monkeypatch.setenv("PYTHONPATH", path)
    yield


def board_copy(tmp_path: Path, name: str = "router_basic") -> Path:
    for ext in (".kicad_pcb", ".kicad_pro"):
        (tmp_path / f"{name}{ext}").write_bytes((BOARDS / f"{name}{ext}").read_bytes())
    return tmp_path / f"{name}.kicad_pcb"


def test_tool_commands() -> None:
    dsn, ses = Path("a.dsn"), Path("a.ses")
    exe = FreeroutingTool("exe", Path("C:/fr/freerouting.exe")).command(dsn, ses, 50)
    assert exe[1:7] == ["-de", "a.dsn", "-do", "a.ses", "-mp", "50"]
    assert "--gui.enabled=false" in exe
    jar = FreeroutingTool("jar", Path("fr.jar"), "java").command(dsn, ses, 5)
    assert jar[:3] == ["java", "-jar", "fr.jar"]


def test_progress_parsing_and_cancel(
    tmp_path: Path, fake_tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = find_freerouting()
    assert tool is not None and tool.kind == "script"
    dsn = tmp_path / "b.dsn"
    dsn.write_text('{"board": "unused"}', encoding="utf-8")
    monkeypatch.setenv("FAKE_FR_SLEEP", "30")
    seen: list[tuple[int | None, int | None]] = []
    cancel = threading.Event()
    threading.Timer(1.5, cancel.set).start()
    t0 = time.monotonic()
    with pytest.raises(FreeroutingError, match="canceled"):
        run_freerouting(
            tool, dsn, tmp_path / "b.ses", 5, lambda p, u, _l: seen.append((p, u)), cancel
        )
    assert time.monotonic() - t0 < 10  # the process was killed, not waited for
    assert seen and seen[-1][0] and seen[-1][1] == 9
    monkeypatch.setenv("FAKE_FR_FAIL", "1")
    monkeypatch.setenv("FAKE_FR_SLEEP", "0")
    with pytest.raises(FreeroutingError, match="simulated Freerouting failure"):
        run_freerouting(tool, dsn, tmp_path / "c.ses", 5)


def test_result_diff_and_per_net_validation(tmp_path: Path) -> None:
    src = board_copy(tmp_path)
    wb = WorkingBoard(load_board(src).board, load_project_rules(src))
    routed = BoardRouter(wb, BoardRouterSettings()).run()
    # the "routed file": our result plus one illegal short on net B
    from dataclasses import replace

    from pcbrouter.domain.track import Track

    a = next(p for p in wb.board.pads if p.net_name == "A")
    b = next(p for p in wb.board.pads if p.net_name == "B")
    short = Track("short", a.position, b.position, 250_000, "F.Cu", "B")
    board = replace(routed.final_board, tracks=(*routed.final_board.tracks, short))
    res = result_from_routed(wb, board, 1.0)
    assert res.outcomes["B"].status is not RouteStatus.SUCCESS
    assert "validator" in res.outcomes["B"].message
    good = [n for n, o in res.outcomes.items() if o.status is RouteStatus.SUCCESS]
    assert len(good) >= 3 and "B" not in good
    assert not any(t.net_name == "B" for t in res.added_tracks)  # rejected net left out
    existing = {(t.start, t.end) for t in wb.board.tracks}
    assert not any((t.start, t.end) in existing for t in res.added_tracks)  # no duplicates
    assert all(len(t.id) == 36 for t in res.added_tracks)  # KiCad-style UUIDs


def test_full_pipeline_with_stand_in_tools(tmp_path: Path, fake_tools: None) -> None:
    src = board_copy(tmp_path)
    before = src.read_bytes()
    wb = WorkingBoard(load_board(src).board, load_project_rules(src))
    tool = find_freerouting()
    assert tool is not None
    phases: list[str] = []
    res = freeroute(wb, src, tool, 10, lambda ph, _i: phases.append(ph))
    assert res.status is BoardStatus.FULLY_ROUTED, res.summary()
    assert {"PREPARING_BOARD", "PLANNING", "ROUTING", "VALIDATING"} <= set(phases)
    assert src.read_bytes() == before  # the source file is never touched
    wb.commit_objects(res.added_tracks, res.added_vias, (), "accept")  # validated
    assert not wb.engine.run_drc().errors


def test_missing_kicad_python_is_explained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pcbrouter.kicad.specctra_bridge as bridge

    monkeypatch.setattr(bridge, "find_kicad_python", lambda: None)
    src = board_copy(tmp_path)
    wb = WorkingBoard(load_board(src).board, load_project_rules(src))
    with pytest.raises(FreeroutingError, match="KiCad's Python"):
        freeroute(wb, src, FreeroutingTool("script", FAKE_FR), 5)


@pytest.mark.gui
def test_route_with_freerouting_in_the_app(qtbot: object, tmp_path: Path, fake_tools: None) -> None:
    from pcbrouter.settings import SettingsStore
    from tests.integration.test_routing_ui import wait
    from tests.integration.test_ui import make_window

    w = make_window(qtbot, SettingsStore(tmp_path / "config" / "settings.json"))  # type: ignore[arg-type]
    w.route_jobs.restart_worker()  # the worker inherits the stand-in tool environment
    assert w.open_board(board_copy(tmp_path))
    wait(w)
    ui = w.routing_ui
    assert ui.act_route_freerouting.isEnabled() and ui.route_board_freerouting()
    wait(w, 120)
    done = w.route_jobs.last_done
    assert done.status == "COMPLETED", done.error + done.traceback
    assert done.backend.requested == "Freerouting"
    assert ui.last_board_result is not None
    assert ui.accept_board(None)
    assert w.bus.context.project.working.modified
    assert not w.bus.context.project.working.engine.run_drc().errors
    w.close()


# ------------------------------------------------------------ setup + CLI
def test_setup_check_reports_what_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from pcbrouter.routing import freerouting_setup as frs

    monkeypatch.setenv("PCBROUTER_KICAD_PYTHON", str(ROOT / "nope" / "python"))
    monkeypatch.setattr(frs, "find_kicad_python", lambda: None)
    monkeypatch.setattr(frs, "find_freerouting", lambda _c=None: None)
    state = frs.check_setup()
    assert not state.ready
    text = state.text()
    assert "KiCad 7-10 is needed" in text and "Freerouting is not installed" in text


def test_setup_check_ready_with_tools(fake_tools: None) -> None:
    from pcbrouter.routing import freerouting_setup as frs

    state = frs.check_setup()
    assert state.ready, state.text()
    assert "Ready." in state.text()


def test_java_version_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from pcbrouter.routing import freerouting_setup as frs

    def fake_run(out: str) -> Callable[..., subprocess.CompletedProcess[str]]:
        return lambda *a, **k: subprocess.CompletedProcess(a, 0, "", out)

    monkeypatch.setattr(frs.subprocess, "run", fake_run('openjdk version "21.0.4" 2024-07-16'))
    assert frs.java_major("java") == 21
    monkeypatch.setattr(frs.subprocess, "run", fake_run('java version "1.8.0_401"'))
    assert frs.java_major("java") == 8
    assert frs.java_major(None) is None


def test_download_jar_picks_the_release_jar(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import json

    from pcbrouter.routing import freerouting_setup as frs

    base = "https://github.com/freerouting/freerouting/releases/download/v2.1.0/"
    release = {
        "assets": [
            {"name": "freerouting-2.1.0-windows-x64.msi", "browser_download_url": base + "a.msi"},
            {
                "name": "freerouting-2.1.0.jar",
                "browser_download_url": base + "freerouting-2.1.0.jar",
            },
        ]
    }

    class Resp(io.BytesIO):
        headers: ClassVar[dict[str, str]] = {"Content-Length": "5"}

        def __enter__(self) -> Resp:
            return self

        def __exit__(self, *_a: object) -> None:
            self.close()

    urls: list[str] = []

    def fake_open(req: object, timeout: float = 0) -> Resp:
        url = getattr(req, "full_url", req)
        urls.append(str(url))
        return Resp(json.dumps(release).encode() if "api.github.com" in str(url) else b"JAR!!")

    monkeypatch.setattr(frs.urllib.request, "urlopen", fake_open)
    seen: list[float | None] = []
    path = frs.download_jar(lambda _s, f: seen.append(f))
    assert path.name == "freerouting-2.1.0.jar" and path.read_bytes() == b"JAR!!"
    assert path.parent == frs.jar_dir() and seen[-1] == 1.0
    assert urls[-1].endswith("freerouting-2.1.0.jar")
    release["assets"] = [{"name": "x.jar", "browser_download_url": "https://evil.example/x.jar"}]
    with pytest.raises(FreeroutingError, match=r"no freerouting \.jar"):
        frs.download_jar()


def test_selftest_and_cli(
    tmp_path: Path, fake_tools: None, capsys: pytest.CaptureFixture[str]
) -> None:
    from pcbrouter.app.application import main

    src = board_copy(tmp_path)
    before = src.read_bytes()
    assert main(["--setup-freerouting", str(src)]) == 0
    assert "Freerouting works" in capsys.readouterr().out
    out = tmp_path / "routed.kicad_pcb"
    assert main(["--freeroute", str(src), "--output", str(out), "--passes", "5"]) == 0
    printed = capsys.readouterr().out
    assert "ROUTER RESULT board" in printed and "Exported routed.kicad_pcb" in printed
    assert src.read_bytes() == before  # the source is never changed
    routed = load_board(out).board
    assert len(routed.tracks) > len(load_board(src).board.tracks)
    assert main(["--freeroute", str(src), "--output", str(src)]) == 1  # no overwrite


def test_cli_without_freerouting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                 capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    from pcbrouter.app.application import main
    from pcbrouter.routing import freerouting

    monkeypatch.setattr(freerouting, "find_freerouting", lambda _c=None: None)
    assert (
        main(["--freeroute", str(board_copy(tmp_path)), "--output", str(tmp_path / "o.kicad_pcb")])
        == 1
    )
    assert "--setup-freerouting" in capsys.readouterr().out


@pytest.mark.gui
def test_setup_dialog_buttons_answer(qtbot: object, tmp_path: Path, fake_tools: None,
                                     monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QMessageBox

    from pcbrouter.routing import freerouting_setup as frs
    from pcbrouter.settings import SettingsStore
    from pcbrouter.ui.freerouting_dialog import FreeroutingDialog
    from tests.integration.test_ui import make_window

    opened: list[str] = []
    monkeypatch.setattr(
        QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True
    )
    w = make_window(qtbot, SettingsStore(tmp_path / "config" / "settings.json"))  # type: ignore[arg-type]
    dlg = FreeroutingDialog(w)
    qtbot.waitUntil(lambda: dlg.state is not None, timeout=30_000)  # type: ignore[attr-defined]
    assert dlg.state is not None and dlg.state.ready
    out = dlg.output.toPlainText
    dlg.test_button.click()
    assert "Open a board first" in out()
    dlg.page_button.click()
    assert opened and "freerouting/releases" in opened[-1] and "installer" in out()
    dlg.kicad_button.click()
    assert "kicad.org" in opened[-1]
    assert not dlg.use_path(tmp_path / "missing.exe")
    jar = tmp_path / "freerouting-9.9.jar"
    jar.write_bytes(b"")
    assert dlg.use_path(jar) and w.settings.routing.freerouting_path == str(jar)
    qtbot.waitUntil(lambda: not dlg._jobs.is_running("check"), timeout=30_000)  # type: ignore[attr-defined]
    w.settings.routing.freerouting_path = None  # back to the stand-in tool
    dlg.passes.setValue(42)
    assert w.settings.routing.freerouting_passes == 42
    monkeypatch.setattr(frs, "java_install_command", lambda: None)
    dlg.java_button.click()
    assert "adoptium" in out()
    monkeypatch.setattr(frs, "java_install_command", lambda: ["winget", "install", "x"])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    dlg.java_button.click()
    assert "Java install canceled" in out()
    monkeypatch.setattr(
        frs, "download_jar", lambda *a, **k: (_ for _ in ()).throw(OSError("blocked"))
    )
    dlg.download_button.click()
    qtbot.waitUntil(lambda: "Download failed: blocked" in out(), timeout=10_000)  # type: ignore[attr-defined]
    # With a board open, Test exports it with KiCad and runs one Freerouting pass.
    assert w.open_board(board_copy(tmp_path))
    dlg.check()
    qtbot.waitUntil(lambda: not dlg._jobs.is_running("check"), timeout=30_000)  # type: ignore[attr-defined]
    dlg.test_button.click()
    qtbot.waitUntil(lambda: "Freerouting works" in out() or "Test failed" in out(), timeout=60_000)  # type: ignore[attr-defined]
    assert "Freerouting works" in out(), out()
    dlg.reject()
    w.close()


@pytest.mark.gui
def test_route_menu_opens_setup_when_missing(qtbot: object, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    from pcbrouter.routing import freerouting
    from pcbrouter.settings import SettingsStore
    from tests.integration.test_routing_ui import wait
    from tests.integration.test_ui import make_window

    monkeypatch.setattr(freerouting, "find_freerouting", lambda _c=None: None)
    monkeypatch.setattr("pcbrouter.routing.freerouting_setup.find_kicad_python", lambda: None)
    w = make_window(qtbot, SettingsStore(tmp_path / "config" / "settings.json"))  # type: ignore[arg-type]
    assert w.open_board(board_copy(tmp_path))
    wait(w)
    assert not w.routing_ui.route_board_freerouting()
    assert w._freerouting_dialog.isVisible()
    w._freerouting_dialog.reject()
    w.close()
