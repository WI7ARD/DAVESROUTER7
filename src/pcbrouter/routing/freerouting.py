"""Freerouting as an external routing engine (run as a separate program).

Freerouting (https://github.com/freerouting/freerouting) is a mature open-source
autorouter under GPL-3.0. It is *run*, never copied into this MIT-licensed app,
and it is not bundled: the user installs it (Windows installer with its own
Java, or a ``.jar`` plus Java 21+). The flow, all inside the routing worker:

    working board ─(our writer)→ temp .kicad_pcb ─(KiCad Python)→ .dsn
      ─(Freerouting, headless)→ .ses ─(KiCad Python)→ routed temp .kicad_pcb
      ─(our loader)→ geometry diff → per-net validated commits on a fork
      → BoardRoutingResult (reviewed and accepted like our own router's)

Existing copper is locked before export, so it stays exactly as it is. Every
new object is checked by the exact validator; nets that fail are reported and
left out, never forced onto the board.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pcbrouter.domain.board import Board
from pcbrouter.domain.track import Track
from pcbrouter.domain.via import Via
from pcbrouter.routing.board_router import (
    BoardMetrics,
    BoardRouterSettings,
    BoardRoutingResult,
    BoardStatus,
    NetOutcome,
    make_plan,
)
from pcbrouter.routing.connectivity import NetStatus, net_connectivity
from pcbrouter.routing.result import FailureReason, RouteStatus
from pcbrouter.routing.working_board import CommitError, WorkingBoard, stable_id

log = logging.getLogger(__name__)

TOOL_ENV = "PCBROUTER_FREEROUTING"
RELEASES_URL = "https://github.com/freerouting/freerouting/releases"
MATCH_NM = 1_000  # SES coordinates are rounded: match copper within 1 µm
_PASS = re.compile(r"pass #(\d+)", re.IGNORECASE)
_UNROUTED = re.compile(r"(\d+) unrouted", re.IGNORECASE)


class FreeroutingError(RuntimeError):
    pass


@dataclass
class FreeroutingTool:
    kind: str  # "exe" | "jar" | "script" (tests)
    path: Path
    java: str | None = None

    def command(self, dsn: Path, ses: Path, passes: int) -> list[str]:
        args = ["-de", str(dsn), "-do", str(ses), "-mp", str(passes), "--gui.enabled=false"]
        if self.kind == "jar":
            return [self.java or "java", "-jar", str(self.path), *args]
        if self.kind == "script":
            return [sys.executable, str(self.path), *args]
        return [str(self.path), *args]

    def text(self) -> str:
        return f"{self.path}" + (f" (Java: {self.java})" if self.kind == "jar" else "")


def find_java() -> str | None:
    """``java`` on PATH, else ``%JAVA_HOME%\\bin\\java``, else a standard Windows
    install (Temurin / Microsoft / Oracle), else None."""
    exe = "java.exe" if sys.platform == "win32" else "java"
    found = shutil.which("java")
    if found:
        return found
    home = os.environ.get("JAVA_HOME")
    if home and (Path(home) / "bin" / exe).exists():
        return str(Path(home) / "bin" / exe)
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES"),):
            if not base:
                continue
            for vendor in ("Eclipse Adoptium", "Microsoft", "Java"):
                for cand in sorted((Path(base) / vendor).glob(f"*/bin/{exe}"), reverse=True):
                    return str(cand)
    return None


def _tool_for(path: Path) -> FreeroutingTool | None:
    if not path.exists():
        return None
    suffix = path.suffix.lower()
    if suffix == ".jar":
        return FreeroutingTool("jar", path, find_java())
    if suffix == ".py":
        return FreeroutingTool("script", path)
    return FreeroutingTool("exe", path)


def find_freerouting(configured: str | None = None) -> FreeroutingTool | None:
    """Configured path → env override → app data folder → standard installs → PATH."""
    candidates: list[Path] = []
    for raw in (configured, os.environ.get(TOOL_ENV)):
        if raw:
            candidates.append(Path(raw))
    from pcbrouter.utils.paths import data_dir

    candidates += sorted((data_dir() / "freerouting").glob("freerouting*.jar"), reverse=True)
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("LOCALAPPDATA")):
            if base:
                for sub in ("freerouting", "Freerouting", r"Programs\freerouting"):
                    candidates.append(Path(base) / sub / "freerouting.exe")
    found = shutil.which("freerouting")
    if found:
        candidates.append(Path(found))
    for c in candidates:
        tool = _tool_for(c)
        if tool is not None and (tool.kind != "jar" or tool.java):
            return tool
    return None


def run_freerouting(
    tool: FreeroutingTool,
    dsn: Path,
    ses: Path,
    passes: int,
    progress: Callable[[int | None, int | None, str], None] | None = None,
    cancel: threading.Event | None = None,
    timeout_s: float = 3600.0,
) -> list[str]:
    """Run Freerouting headless; stream progress; kill it on cancel/timeout."""
    cmd = tool.command(dsn, ses, passes)
    log.info("freerouting.start %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(dsn.parent),
        )
    except OSError as exc:
        raise FreeroutingError(f"could not start Freerouting: {exc}") from exc
    lines: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line.rstrip())
        lines.put(None)

    threading.Thread(target=reader, name="freerouting-output", daemon=True).start()
    tail: list[str] = []
    deadline = time.monotonic() + timeout_s
    current_pass: int | None = None
    done = False
    while not done:
        if cancel is not None and cancel.is_set():
            proc.kill()
            raise FreeroutingError("canceled")
        if time.monotonic() > deadline:
            proc.kill()
            raise FreeroutingError(f"Freerouting did not finish within {timeout_s:.0f} s")
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            continue
        if line is None:
            done = True
            break
        tail = [*tail, line][-40:]
        m = _PASS.search(line)
        if m:
            current_pass = int(m.group(1))
        u = _UNROUTED.search(line)
        if progress is not None and (m or u):
            progress(current_pass, int(u.group(1)) if u else None, line[-160:])
    code = proc.wait(timeout=30)
    if code != 0 or not ses.exists():
        raise FreeroutingError(
            f"Freerouting failed (exit code {code}); last output:\n" + "\n".join(tail[-12:])
        )
    return tail


# ------------------------------------------------------------------ result
def _q(v: int) -> int:
    return round(v / MATCH_NM)


def _track_key(t: Track) -> tuple[Any, ...]:
    a, b = (_q(t.start.x), _q(t.start.y)), (_q(t.end.x), _q(t.end.y))
    return (t.layer, t.net_name, _q(t.width), min(a, b), max(a, b))


def _via_key(v: Via) -> tuple[Any, ...]:
    return (v.net_name, _q(v.position.x), _q(v.position.y))


def result_from_routed(
    working: WorkingBoard, routed: Board, runtime_s: float
) -> BoardRoutingResult:
    """Diff the routed board against the working board and validate each net's new
    copper on a fork (per net, so one rejected net never blocks the others)."""
    base = working.board
    have_t = {_track_key(t) for t in base.tracks}
    have_v = {_via_key(v) for v in base.vias}
    seed = base.fingerprint[:16]
    new_t = [
        Track(
            stable_id("freerouting", seed, "t", i),
            t.start,
            t.end,
            t.width,
            t.layer,
            t.net_name,
            t.mid,
        )
        for i, t in enumerate(routed.tracks)
        if _track_key(t) not in have_t and t.net_name
    ]
    new_v = [
        Via(
            stable_id("freerouting", seed, "v", i),
            v.position,
            v.diameter,
            v.drill,
            v.net_name,
            v.start_layer,
            v.end_layer,
            v.via_type,
        )
        for i, v in enumerate(routed.vias)
        if _via_key(v) not in have_v and v.net_name
    ]
    missing = len(have_t - {_track_key(t) for t in routed.tracks})
    plan = make_plan(working, BoardRouterSettings())
    touched = {t.net_name for t in new_t if t.net_name} | {v.net_name for v in new_v if v.net_name}
    nets = sorted({t.net for t in plan.tasks} | touched)
    fork = working.fork()
    outcomes: dict[str, NetOutcome] = {}
    kept_t: list[Track] = []
    kept_v: list[Via] = []
    job_log = [f"Freerouting added {len(new_t)} segment(s) and {len(new_v)} via(s)"]
    if missing:
        job_log.append(f"note: {missing} existing segment(s) were not echoed back (kept as is)")
    for net in nets:
        nt = [t for t in new_t if t.net_name == net]
        nv = [v for v in new_v if v.net_name == net]
        o = NetOutcome(net, RouteStatus.NO_ROUTE)
        if nt or nv:
            try:
                fork.commit_objects(nt, nv, (), f"freerouting {net}")
                kept_t += nt
                kept_v += nv
                o.added_ids = [t.id for t in nt] + [v.id for v in nv]
                o.length_nm = sum(t.length for t in nt)
                o.vias = len(nv)
            except CommitError as exc:
                o.status, o.reason = RouteStatus.NO_ROUTE, FailureReason.VALIDATION
                o.message = f"rejected by the exact validator: {str(exc)[:200]}"
                job_log.append(f"{net}: {o.message}")
        status = net_connectivity(fork.engine.geometry, net).status
        if status in (NetStatus.FULLY_CONNECTED, NetStatus.NOT_APPLICABLE):
            o.status = RouteStatus.SUCCESS
        elif o.added_ids:
            o.status = RouteStatus.PARTIAL
        elif not o.message:
            o.message = "Freerouting left this net unrouted"
        outcomes[net] = o
    completed = sum(1 for o in outcomes.values() if o.status is RouteStatus.SUCCESS)
    metrics = BoardMetrics(
        nets_attempted=len(outcomes),
        nets_completed=completed,
        nets_failed=len(outcomes) - completed,
        total_length_nm=sum(t.length for t in kept_t),
        new_vias=len(kept_v),
        runtime_s=runtime_s,
    )
    if not outcomes:
        status_b = BoardStatus.NOTHING_TO_ROUTE
    elif completed == len(outcomes):
        status_b = BoardStatus.FULLY_ROUTED
    elif kept_t or kept_v:
        status_b = BoardStatus.PARTIALLY_ROUTED
    else:
        status_b = BoardStatus.FAILED
    plan.tasks = [t for t in plan.tasks if t.net in outcomes] or plan.tasks
    known = {t.net for t in plan.tasks}
    for net in outcomes:
        if net not in known:  # nets Freerouting touched that our plan did not list
            from pcbrouter.routing.board_router import RouteTask, TaskKind

            plan.tasks.append(RouteTask(net, TaskKind.SIGNAL))
    return BoardRoutingResult(
        status_b,
        base,
        fork.board,
        plan,
        outcomes,
        metrics,
        tuple(kept_t),
        tuple(kept_v),
        (),
        job_log,
    )


def freeroute(
    working: WorkingBoard,
    source_path: Path,
    tool: FreeroutingTool,
    passes: int,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
    cancel: threading.Event | None = None,
) -> BoardRoutingResult:
    """The whole pipeline (worker job and ``--freeroute`` CLI)."""
    from pcbrouter.kicad.loader import load_board
    from pcbrouter.kicad.specctra_bridge import export_dsn, find_kicad_python, import_ses
    from pcbrouter.kicad.writer import ExportError, compose

    def tell(phase: str, **info: Any) -> None:
        if progress is not None:
            progress(phase, info)

    t0 = time.perf_counter()
    tell("PREPARING_BOARD", message="looking for KiCad's Python (pcbnew)")
    python = find_kicad_python()
    if python is None:
        raise FreeroutingError(
            "KiCad's Python (pcbnew module) was not found. Install KiCad 7-10, or see "
            "Tools ▸ Set Up Freerouting."
        )
    with tempfile.TemporaryDirectory(prefix="pcbrouter-fr-") as tmp:
        d = Path(tmp)
        src_text = source_path.read_text(encoding="utf-8")
        src_ids = {t.id for t in working.source.tracks} | {v.id for v in working.source.vias}
        codes = {n.name: n.code for n in working.source.nets if n.code is not None}
        board_file = d / "working.kicad_pcb"
        added_t = [t for t in working.board.tracks if t.id not in src_ids]
        added_v = [v for v in working.board.vias if v.id not in src_ids]
        if not added_t and not added_v:
            shutil.copy2(source_path, board_file)  # nothing accepted yet: the file as is
        else:
            try:
                text = compose(src_text, added_t, added_v, codes)
            except ExportError as exc:
                raise FreeroutingError(
                    f"could not prepare the board for Freerouting: {exc}"
                ) from exc
            board_file.write_text(text, encoding="utf-8")
        pro = source_path.with_suffix(".kicad_pro")
        if pro.exists():  # KiCad reads net classes/rules from the project file
            shutil.copy2(pro, d / "working.kicad_pro")
        dsn, ses, out = d / "working.dsn", d / "working.ses", d / "routed.kicad_pcb"
        tell("PLANNING", message=f"KiCad {python.version}: exporting Specctra DSN")
        export_dsn(python, board_file, dsn)

        def fr_progress(pass_no: int | None, unrouted: int | None, line: str) -> None:
            tell("ROUTING", current_pass=pass_no, unrouted=unrouted, message=line)

        tell("ROUTING", message=f"Freerouting: {tool.text()}")
        run_freerouting(tool, dsn, ses, passes, fr_progress, cancel)
        tell("VALIDATING", message="importing the session and validating every net")
        import_ses(python, board_file, ses, out)
        routed = load_board(out).board
    return result_from_routed(working, routed, time.perf_counter() - t0)
