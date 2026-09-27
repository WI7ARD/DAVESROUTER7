"""Child-process launch without flashing a console window over the GUI.

On Windows every console program (java.exe, KiCad's python.exe, nvidia-smi,
helper CLIs) opens a visible terminal unless told otherwise. Pass
``**hidden_console_kwargs()`` to ``subprocess.Popen``/``run`` for any child
the GUI can start. No-op on other platforms.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any


def hidden_console_kwargs() -> dict[str, Any]:
    """Popen/run kwargs that keep the child windowless (Windows only)."""
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "startupinfo": startupinfo,
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "stdin": subprocess.DEVNULL,
    }
