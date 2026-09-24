"""The application must work without KiCad, CUDA, or any LLM SDK installed.

Each test runs in a fresh interpreter whose import system *refuses* those modules,
so the result does not depend on what happens to be installed on the test machine.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BLOCKED = [
    "pcbnew",
    "kicad",
    "kiutils",
    "cupy",
    "numba",
    "torch",
    "cuda",
    "pycuda",
    "openai",
    "anthropic",
    "keyring",
]

BLOCKER = textwrap.dedent(f"""
    import importlib.abc, sys
    BLOCKED = {BLOCKED!r}

    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ModuleNotFoundError(f"blocked for test: {{name}}", name=name)
            return None

    sys.meta_path.insert(0, Block())
    """)


def run_python(code: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(
        QT_QPA_PLATFORM="offscreen",
        PCBROUTER_CONFIG_DIR=str(tmp_path / "config"),
        PCBROUTER_DATA_DIR=str(tmp_path / "data"),
        PCBROUTER_LOG_DIR=str(tmp_path / "logs"),
        PYTHONPATH=os.pathsep.join([str(REPO / "src"), str(REPO)]),
    )
    return subprocess.run(
        [sys.executable, "-c", BLOCKER + textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )


def test_cli_works_without_optional_packages(
    tmp_path: Path, fixture_path: Callable[[str], Path]
) -> None:
    board = fixture_path("four_layer.kicad_pcb")
    proc = run_python(
        f"""
        import sys
        from pcbrouter.app.application import main
        code = main([{str(board)!r}, "--inspect", "--no-log-file"])
        leaked = [m for m in sys.modules if m.split(".")[0] in BLOCKED]
        assert not leaked, leaked
        sys.exit(code)
        """,
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["counts"]["footprints"] == 4


def test_gpu_detection_reports_missing_libraries(tmp_path: Path) -> None:
    proc = run_python(
        """
        from pcbrouter.compute import detect_gpu
        r = detect_gpu()
        assert r.installed_packages == (), r.installed_packages
        print(r.status.value)
        """,
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() in {
        "cuda_unavailable",
        "unsupported_gpu",
        "gpu_libraries_not_installed",
    }


@pytest.mark.gui
def test_gui_entry_point_starts_and_exits_cleanly(
    tmp_path: Path, fixture_path: Callable[[str], Path]
) -> None:
    """Runs the real ``main()`` GUI path (theme, services, window, background GPU
    detection, board open, event loop, shutdown) and quits it with a timer."""
    board = fixture_path("vias.kicad_pcb")
    proc = run_python(
        f"""
        import sys
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        app = QApplication(sys.argv[:1])
        QTimer.singleShot(1500, app.quit)
        from pcbrouter.app.application import main
        sys.exit(main([{str(board)!r}]))
        """,
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    log = (tmp_path / "logs" / "pcbrouter.log").read_text(encoding="utf-8")
    for needle in (
        "app.start",
        "app.environment",
        "app.gui qt=",
        "board.load.done",
        "canvas.build items=",
        "compute.selected backend=CPU",
        "compute.gpu_detected",
        "unchanged=True",
        "app.shutdown exit_code=0",
    ):
        assert needle in log, needle
    settings = json.loads((tmp_path / "config" / "settings.json").read_text(encoding="utf-8"))
    assert settings["recent_boards"] == [str(board.resolve())]
