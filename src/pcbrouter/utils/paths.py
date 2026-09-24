"""Platform-aware application directories (Windows 11 and Linux).

No path is hard-coded. Environment overrides exist so tests and portable installs
can redirect everything:

* ``PCBROUTER_CONFIG_DIR`` – settings
* ``PCBROUTER_DATA_DIR``   – workspaces / future snapshots
* ``PCBROUTER_LOG_DIR``    – rotating log files
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pcbrouter import APP_NAME, APP_SLUG


def _env_path(var: str) -> Path | None:
    value = os.environ.get(var)
    return Path(value).expanduser() if value else None


def _is_windows() -> bool:
    return sys.platform == "win32"


def config_dir() -> Path:
    override = _env_path("PCBROUTER_CONFIG_DIR")
    if override:
        return override
    if _is_windows():
        base = _env_path("APPDATA") or Path.home() / "AppData" / "Roaming"
        return base / APP_NAME
    base = _env_path("XDG_CONFIG_HOME") or Path.home() / ".config"
    return base / APP_SLUG


def data_dir() -> Path:
    override = _env_path("PCBROUTER_DATA_DIR")
    if override:
        return override
    if _is_windows():
        base = _env_path("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return base / APP_NAME
    base = _env_path("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return base / APP_SLUG


def log_dir() -> Path:
    override = _env_path("PCBROUTER_LOG_DIR")
    if override:
        return override
    if _is_windows():
        return data_dir() / "logs"
    base = _env_path("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return base / APP_SLUG / "logs"
