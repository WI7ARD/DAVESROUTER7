"""Make the GPU runtimes loadable before CuPy/dpnp/dpctl are imported (worker only).

On Windows, dpnp/dpctl need the Intel SYCL/oneMKL DLLs from ``<env>\\Library\\bin``.
dpctl registers that folder only inside a virtual environment; it is missing
for the frozen (installed) app, where the build bundles it under
``_internal\\Library\\bin``, and for a system-wide Python. This adds the folder
to the DLL search path (idempotent; no-op elsewhere).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)
_ADDED: set[Path] = set()


def _user_base() -> Path | None:
    """Per-user install base (``%APPDATA%\\Python``): pip writes the Intel
    runtime DLLs to ``<userbase>\\Library\\bin`` for system-wide Pythons."""
    try:
        import site

        base = site.getuserbase()
    except Exception:
        return None
    return Path(base) if base else None


def runtime_dirs() -> list[Path]:
    bases = []
    if getattr(sys, "frozen", False):
        bases.append(Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)))
    bases.append(Path(sys.prefix))
    user = _user_base()
    if user is not None:
        bases.append(user)
    out: list[Path] = []
    for base in bases:
        for sub in (Path("Library") / "bin", Path("dpctl"), Path("dpnp")):
            d = base / sub
            if d.is_dir() and d not in out:
                out.append(d)
    return out


def prepare_gpu_runtime() -> list[Path]:
    """Register GPU runtime DLL folders (Windows). Returns the folders added."""
    if sys.platform != "win32":
        return []
    # No one-shot latch: rescan every call and add only new folders, so a
    # library installed mid-process (--setup-gpu) is picked up without restart.
    added: list[Path] = []
    for d in runtime_dirs():
        if d in _ADDED:
            continue
        try:
            os.add_dll_directory(str(d))
        except (OSError, AttributeError):
            continue
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
        _ADDED.add(d)
        added.append(d)
    if added:
        log.info("gpu.runtime_dirs %s", ", ".join(map(str, added)))
    return added
