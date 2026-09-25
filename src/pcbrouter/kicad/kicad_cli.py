"""Optional KiCad command-line integration (Stage 9): real **KiCad DRC**.

Used only when ``kicad-cli`` (KiCad 7+) is installed; the application never needs
KiCad. Commands are argument lists (no shell, no string concatenation) with a
timeout. Results are labelled "KiCad DRC" — distinct from the application's own
"Internal Geometry Check".
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

LABEL = "KiCad DRC"
DRC_TIMEOUT_S = 300.0
_WINDOWS_CANDIDATES = (
    r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe",
    r"C:\Program Files\KiCad\8.0\bin\kicad-cli.exe",
    r"C:\Program Files\KiCad\7.0\bin\kicad-cli.exe",
)


@dataclass
class KiCadCli:
    path: Path
    version: str | None


@dataclass
class KiCadDRCResult:
    ran: bool
    passed: bool = False
    violations: int = 0
    unconnected: int = 0
    errors: int = 0
    warnings: int = 0
    kicad_version: str | None = None
    message: str = ""
    details: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.ran:
            return f"{LABEL}: not run — {self.message}"
        verdict = "passed" if self.passed else "FAILED"
        return (
            f"{LABEL} {verdict} (KiCad {self.kicad_version or '?'}): {self.errors} error(s), "
            f"{self.warnings} warning(s), {self.unconnected} unconnected"
        )


def find_kicad_cli() -> KiCadCli | None:
    exe = shutil.which("kicad-cli")
    if exe is None and sys.platform == "win32":
        exe = next((c for c in _WINDOWS_CANDIDATES if os.path.exists(c)), None)
    if exe is None:
        return None
    try:
        out = subprocess.run(
            [exe, "version"], capture_output=True, text=True, timeout=20, check=False
        )
        version = out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("kicad_cli.version_failed error=%r", exc)
        return None
    return KiCadCli(Path(exe), version)


def run_kicad_drc(
    board: Path, cli: KiCadCli | None = None, timeout_s: float = DRC_TIMEOUT_S
) -> KiCadDRCResult:
    cli = cli or find_kicad_cli()
    if cli is None:
        return KiCadDRCResult(False, message="kicad-cli not found (KiCad is optional)")
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "drc.json"
        args = [
            str(cli.path),
            "pcb",
            "drc",
            "--format",
            "json",
            "--severity-all",
            "--output",
            str(report),
            str(board),
        ]
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout_s, check=False
            )
        except subprocess.TimeoutExpired:
            return KiCadDRCResult(
                False,
                message=f"KiCad DRC timed out after {timeout_s:.0f} s",
                kicad_version=cli.version,
            )
        except OSError as exc:
            return KiCadDRCResult(
                False, message=f"could not start kicad-cli: {exc}", kicad_version=cli.version
            )
        if not report.exists():
            return KiCadDRCResult(
                False,
                kicad_version=cli.version,
                message=(proc.stderr or proc.stdout or "no report")[:300],
            )
        return parse_drc_report(json.loads(report.read_text(encoding="utf-8")), cli.version)


def parse_drc_report(data: dict[str, object], version: str | None = None) -> KiCadDRCResult:
    raw_violations = data.get("violations")
    unconnected = data.get("unconnected_items")
    violations = raw_violations if isinstance(raw_violations, list) else []
    items = [v for v in violations if isinstance(v, dict)]
    errors = sum(1 for v in items if v.get("severity") == "error")
    warnings = sum(1 for v in items if v.get("severity") == "warning")
    unc = len(unconnected) if isinstance(unconnected, list) else 0
    details = [f"{v.get('severity')}: {v.get('description')}" for v in items[:50]]
    return KiCadDRCResult(
        True,
        passed=errors == 0 and unc == 0,
        violations=len(items),
        unconnected=unc,
        errors=errors,
        warnings=warnings,
        kicad_version=version or str(data.get("kicad_version") or ""),
        details=details,
    )
