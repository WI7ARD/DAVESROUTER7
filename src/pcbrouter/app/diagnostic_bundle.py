"""Diagnostic bundle export (Stage 9.17) — only on explicit user request.

Contains: versions, OS/CPU/GPU detection, compute/KiCad availability, engine and
routing diagnostics, routing settings, the reproducibility tuple (board
fingerprint, app version, settings, seed, last request ids) and the tail of the
log file, passed through the secret redactor. It never contains the PCB file,
board text, API keys, Authorization headers or provider profiles' credentials.
"""

from __future__ import annotations

import json
import platform
import time
import zipfile
from pathlib import Path
from typing import Any

from pcbrouter import __version__
from pcbrouter.app_logging.setup import LOG_FILENAME, redact_secrets
from pcbrouter.utils.paths import log_dir

MAX_LOG_LINES = 2000


def _log_tail(directory: Path | None = None) -> str:
    path = (directory or log_dir()) / LOG_FILENAME
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-MAX_LOG_LINES:]
    except OSError:
        return "(no log file)"
    return redact_secrets("\n".join(lines))


def build_bundle(out: Path, info: dict[str, Any], log_directory: Path | None = None) -> Path:
    if out.suffix.lower() != ".zip":
        out = out.with_suffix(".zip")
    manifest = {
        "format": "pcbrouter-diagnostics/1",
        "app_version": __version__,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu": platform.processor() or platform.machine(),
        "contents": ["diagnostics.json", "log_tail.txt"],
        "excluded": ["PCB/board files", "API keys", "Authorization headers", "prompts"],
    }
    text = redact_secrets(json.dumps({**manifest, **info}, indent=2, default=str))
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("diagnostics.json", text)
        z.writestr("log_tail.txt", _log_tail(log_directory))
    return out
