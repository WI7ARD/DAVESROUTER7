"""Utility commands used by support and by the Windows installer.

* ``--diagnostics`` prints a JSON report of the runtime: version, frozen/installed
  state, directories, Qt and optional-package availability, credential backend. The
  Windows build pipeline runs it on the *installed* executable to prove the bundle is
  complete (Qt, SDKs, keyring backends all importable).
* ``--forget-api-keys`` deletes the OS-keyring entries of every provider profile in
  the settings file. The uninstaller runs it when the user chooses to remove their
  data, so no API key is orphaned in Windows Credential Manager.

Neither command opens a board, starts the GUI, touches the network or writes logs.
Neither ever prints a key.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import platform
import sys
from collections.abc import Callable
from typing import Any

from pcbrouter import APP_NAME, STAGE, __version__
from pcbrouter.ai.credentials import CredentialService
from pcbrouter.settings.settings import SettingsStore
from pcbrouter.utils.paths import config_dir, data_dir, log_dir

#: Optional packages reported by ``--diagnostics`` (import name, distribution name).
OPTIONAL_PACKAGES: tuple[tuple[str, str], ...] = (
    ("openai", "openai"),
    ("anthropic", "anthropic"),
    ("keyring", "keyring"),
)

#: SDK client classes constructed (never used for a request) by ``--diagnostics``.
#: Construction builds the HTTP client and TLS context, which is where a frozen build
#: with a missing CA bundle or transport module fails.
SDK_CLIENTS: dict[str, str] = {"openai": "AsyncOpenAI", "anthropic": "AsyncAnthropic"}
#: Unroutable local address: a client is created, nothing is ever sent.
_OFFLINE_BASE_URL = "http://127.0.0.1:9/"
_PLACEHOLDER = "diagnostics-placeholder"


def _client_check(sdk: Any, class_name: str) -> str:
    try:
        client = getattr(sdk, class_name)(
            api_key=_PLACEHOLDER, base_url=_OFFLINE_BASE_URL, max_retries=0
        )
        asyncio.run(client.close())
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return "ok"


def _package_status(module: str, dist: str) -> dict[str, Any]:
    """Actually import the package: in a frozen build ``find_spec`` alone would not
    prove that the package's own dependencies were bundled."""
    try:
        sdk = importlib.import_module(module)
    except Exception as exc:  # ImportError, or a broken transitive dependency
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        version: str | None = importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        version = None
    status: dict[str, Any] = {"available": True, "version": version}
    if module in SDK_CLIENTS:
        status["client"] = _client_check(sdk, SDK_CLIENTS[module])
    return status


def _qt_status() -> dict[str, Any]:
    try:
        from PySide6 import __version__ as pyside_version
        from PySide6.QtCore import qVersion
        from PySide6.QtWidgets import QApplication  # noqa: F401  (proves QtWidgets loads)
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"available": True, "qt": qVersion(), "pyside": pyside_version}


def diagnostics(credentials: CredentialService | None = None) -> dict[str, Any]:
    creds = credentials if credentials is not None else CredentialService()
    return {
        "app": APP_NAME,
        "version": __version__,
        "stage": STAGE,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "paths": {
            "config": str(config_dir()),
            "data": str(data_dir()),
            "logs": str(log_dir()),
        },
        "qt": _qt_status(),
        "packages": {mod: _package_status(mod, dist) for mod, dist in OPTIONAL_PACKAGES},
        "credential_store": {
            "secure_available": creds.secure_available,
            "description": creds.describe_secure_backend(),
        },
    }


def forget_api_keys(
    store: SettingsStore | None = None,
    credentials: CredentialService | None = None,
    *,
    echo: Callable[[str], None] = print,
) -> int:
    """Delete the stored key of every configured provider profile. Returns an exit
    code: 0 when every lookup worked (including "nothing to delete"), 1 otherwise."""
    settings_store = store if store is not None else SettingsStore()
    creds = credentials if credentials is not None else CredentialService()
    if not settings_store.path.exists():
        echo("No settings file found; no provider profiles, nothing to remove.")
        return 0
    profiles = settings_store.load().ai.profiles
    if settings_store.last_load_problem:
        echo(f"Settings file could not be read ({settings_store.last_load_problem}).")
        return 1
    if not creds.secure_available:
        echo(f"{creds.describe_secure_backend()}; no stored keys can exist.")
        return 0
    removed = 0
    for profile in profiles:
        if creds.delete(profile.credential_ref):
            removed += 1
            echo(f"Removed stored API key for profile '{profile.name}'.")
    echo(f"{removed} stored API key(s) removed ({len(profiles)} profile(s) checked).")
    return 0
