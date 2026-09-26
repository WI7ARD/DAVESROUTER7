"""What Freerouting routing needs on this computer, and helpers to get it.

Used by Tools ▸ Set Up Freerouting… and ``pcbrouter --setup-freerouting``.
Commands run here are fixed argument lists (no shell): KiCad's Python with our
own helper script, Freerouting with file paths, ``java -version`` and, after the
user confirms, ``winget install`` of one fixed Java package. The only download
is Freerouting's own release ``.jar`` from its GitHub releases.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pcbrouter.kicad.specctra_bridge import KiCadPython, export_dsn, find_kicad_python
from pcbrouter.routing.freerouting import (
    RELEASES_URL,
    FreeroutingError,
    FreeroutingTool,
    find_freerouting,
    find_java,
    run_freerouting,
)
from pcbrouter.utils.paths import data_dir

log = logging.getLogger(__name__)

LATEST_API = "https://api.github.com/repos/freerouting/freerouting/releases/latest"
JAVA_PACKAGE = "EclipseAdoptium.Temurin.21.JRE"
MIN_JAVA = 21
_JAR = re.compile(r"^freerouting-[\d.]+\.jar$", re.IGNORECASE)
_JAVA_VERSION = re.compile(r'version "(\d+)(?:\.(\d+))?')


@dataclass
class FreeroutingSetup:
    kicad: KiCadPython | None
    tool: FreeroutingTool | None
    java: str | None
    java_major: int | None
    jars: list[Path] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.kicad is not None and self.tool is not None

    def steps(self) -> list[str]:
        out: list[str] = []
        if self.kicad is None:
            out.append(
                "KiCad 7-10 is needed (its Python converts the board to Freerouting's "
                "format and back). Install it from kicad.org, then click 'Check again'."
            )
        else:
            out.append(f"KiCad Python found: {self.kicad.path} (KiCad {self.kicad.version}).")
        if self.tool is not None:
            out.append(f"Freerouting found: {self.tool.text()}.")
        elif self.jars:
            out.append(
                f"Freerouting .jar found ({self.jars[0].name}) but Java {MIN_JAVA}+ is "
                "missing: click 'Install Java'."
            )
        else:
            out.append(
                "Freerouting is not installed. Easiest: 'Open download page' and run the "
                "Windows installer (it includes Java). Or 'Download .jar' (needs Java "
                f"{MIN_JAVA}+), or 'Choose file…' if you already have it."
            )
        if self.java_major is not None and self.java_major < MIN_JAVA:
            out.append(f"Java {self.java_major} is too old for the .jar: Java {MIN_JAVA}+.")
        if self.ready:
            out.append(
                "Ready. Open a board, then Router ▸ Route Board with Freerouting… "
                "('Test' routes the open board once without changing anything)."
            )
        return out

    def text(self) -> str:
        return "\n".join(f"• {s}" for s in self.steps())


def jar_dir() -> Path:
    return data_dir() / "freerouting"


def java_major(java: str | None) -> int | None:
    if not java:
        return None
    try:
        proc = subprocess.run(
            [java, "-version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    m = _JAVA_VERSION.search(proc.stderr + proc.stdout)
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2) or 0) if major == 1 else major  # "1.8.0" means Java 8


def check_setup(configured: str | None = None) -> FreeroutingSetup:
    java = find_java()
    return FreeroutingSetup(
        find_kicad_python(),
        find_freerouting(configured),
        java,
        java_major(java),
        sorted(jar_dir().glob("freerouting*.jar"), reverse=True),
    )


def latest_jar_url(timeout: float = 30) -> tuple[str, str]:
    """(file name, download URL) of the newest release's plain ``freerouting-X.jar``."""
    req = urllib.request.Request(LATEST_API, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        release = json.loads(resp.read().decode("utf-8"))
    for asset in release.get("assets", []):
        name = str(asset.get("name", ""))
        if _JAR.match(name):
            return name, str(asset["browser_download_url"])
    raise FreeroutingError(f"no freerouting .jar in the latest release; see {RELEASES_URL}")


def download_jar(
    progress: Callable[[str, float | None], None] | None = None,
    cancel: threading.Event | None = None,
    timeout: float = 60,
) -> Path:
    """Download the latest release jar into the app data folder (atomic rename)."""
    name, url = latest_jar_url(timeout)
    if not url.startswith("https://github.com/freerouting/freerouting/releases/download/"):
        raise FreeroutingError(f"unexpected download location: {url}")
    dest = jar_dir() / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part")
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with part.open("wb") as fh:
            while chunk := resp.read(1 << 16):
                if cancel is not None and cancel.is_set():
                    fh.close()
                    part.unlink(missing_ok=True)
                    raise FreeroutingError("canceled")
                fh.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(f"{name}: {done / 1e6:.1f} MB", done / total if total else None)
    part.replace(dest)
    log.info("freerouting.downloaded path=%s bytes=%d", dest, done)
    return dest


def java_install_command() -> list[str] | None:
    winget = shutil.which("winget")
    if winget is None:
        return None
    return [winget, "install", "--exact", "--id", JAVA_PACKAGE, "--accept-package-agreements",
            "--accept-source-agreements"]  # fmt: skip


def selftest(
    board: Path,
    tool: FreeroutingTool,
    kicad: KiCadPython,
    progress: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
    timeout_s: float = 600,
) -> str:
    """Export BOARD (a copy) with KiCad and run one Freerouting pass on it.

    Proves both tools work on this computer; nothing is imported or saved."""
    say = progress or (lambda _m: None)
    with tempfile.TemporaryDirectory(prefix="pcbrouter-frtest-") as tmp:
        d = Path(tmp)
        copy = d / "test.kicad_pcb"
        shutil.copy2(board, copy)
        pro = board.with_suffix(".kicad_pro")
        if pro.exists():
            shutil.copy2(pro, d / "test.kicad_pro")
        say(f"KiCad {kicad.version}: exporting {board.name} to Specctra DSN…")
        dsn, ses = d / "test.dsn", d / "test.ses"
        export_dsn(kicad, copy, dsn)
        say("Running one Freerouting pass…")
        tail = run_freerouting(
            tool, dsn, ses, 1, lambda _p, _u, line: say(line), cancel, timeout_s=timeout_s
        )
        last = next((ln for ln in reversed(tail) if ln.strip()), "")
    return f"Freerouting works on this computer ({last[:160]})." if last else "Freerouting works."
