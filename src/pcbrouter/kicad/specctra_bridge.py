"""Specctra (DSN/SES) conversion through KiCad's own Python (``pcbnew`` module).

KiCad is the reference implementation of its file format, so board → DSN and
SES → board are done by KiCad itself, not re-implemented here. Commands are
argument lists with timeouts; only file paths are passed.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from pcbrouter.kicad.kicad_cli import find_kicad_cli

log = logging.getLogger(__name__)

SCRIPT = Path(__file__).resolve().parent / "resources" / "kicad_specctra.py"
PYTHON_ENV = "PCBROUTER_KICAD_PYTHON"  # override (tests, unusual installs)
CHECK_TIMEOUT_S = 60.0
CONVERT_TIMEOUT_S = 600.0


class SpecctraError(RuntimeError):
    pass


@dataclass
class KiCadPython:
    path: Path
    version: str


def _candidates() -> list[str]:
    out: list[str] = []
    if os.environ.get(PYTHON_ENV):
        out.append(os.environ[PYTHON_ENV])
    cli = find_kicad_cli()
    if cli is not None:
        exe = "python.exe" if sys.platform == "win32" else "python3"
        out.append(str(cli.path.parent / exe))
    if sys.platform != "win32":
        for name in ("python3", "python"):  # Linux/macOS: KiCad uses the system Python
            found = shutil.which(name)
            if found:
                out.append(found)
    return list(dict.fromkeys(out))


def find_kicad_python() -> KiCadPython | None:
    """A Python that can ``import pcbnew`` (KiCad's scripting module), or None."""
    for exe in _candidates():
        if not Path(exe).exists():
            continue
        try:
            proc = subprocess.run(
                [exe, "-c", "import pcbnew; print(pcbnew.Version())"],
                capture_output=True,
                text=True,
                timeout=CHECK_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.info("kicad_python.unusable path=%s error=%r", exe, exc)
            continue
        if proc.returncode == 0:
            return KiCadPython(Path(exe), proc.stdout.strip() or "?")
    return None


def _run(python: KiCadPython, args: list[str], timeout_s: float) -> None:
    if not SCRIPT.exists():
        raise SpecctraError(f"helper script missing: {SCRIPT}")
    try:
        proc = subprocess.run(
            [str(python.path), str(SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SpecctraError(f"KiCad conversion timed out after {timeout_s:.0f} s") from exc
    except OSError as exc:
        raise SpecctraError(f"could not start KiCad's Python: {exc}") from exc
    if proc.returncode != 0 or "OK" not in proc.stdout:
        detail = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise SpecctraError(f"KiCad conversion failed: {detail or proc.returncode}")


def export_dsn(python: KiCadPython, board: Path, dsn: Path) -> None:
    _run(python, ["export", str(board), str(dsn)], CONVERT_TIMEOUT_S)


def import_ses(python: KiCadPython, board: Path, ses: Path, out: Path) -> None:
    _run(python, ["import", str(board), str(ses), str(out)], CONVERT_TIMEOUT_S)
