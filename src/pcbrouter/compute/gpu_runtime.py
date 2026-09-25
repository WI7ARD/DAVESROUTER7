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
_DONE = False


def runtime_dirs() -> list[Path]:
    bases = []
    if getattr(sys, "frozen", False):
        bases.append(Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)))
    bases.append(Path(sys.prefix))
    out: list[Path] = []
    for base in bases:
        for sub in (Path("Library") / "bin", Path("dpctl"), Path("dpnp")):
            d = base / sub
            if d.is_dir() and d not in out:
                out.append(d)
    return out


def prepare_gpu_runtime() -> list[Path]:
    """Register GPU runtime DLL folders (Windows). Returns the folders added."""
    global _DONE
    if _DONE or sys.platform != "win32":
        _DONE = True
        return []
    _DONE = True
    added: list[Path] = []
    for d in runtime_dirs():
        try:
            os.add_dll_directory(str(d))
        except (OSError, AttributeError):
            continue
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
        added.append(d)
    if added:
        log.info("gpu.runtime_dirs %s", ", ".join(map(str, added)))
    return added
