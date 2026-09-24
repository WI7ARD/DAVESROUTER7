"""Persistent application settings (JSON).

Format decision: JSON. The standard library can both read *and* write it (TOML
writing needs a third-party package), it round-trips Pydantic models losslessly,
and the file is small and machine-owned.

Robustness rules:

* A missing file yields defaults.
* A corrupt/invalid file is moved aside to ``settings.json.corrupt-<timestamp>``
  (never deleted) and defaults are used; the problem is logged.
* Writes are atomic (temp file + ``os.replace``) so a crash cannot truncate the file.

SECURITY: settings must never contain API keys or other secrets. Future provider
credentials go through the OS keyring and are referenced here only by name.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from pcbrouter.utils.paths import config_dir

log = logging.getLogger(__name__)

SETTINGS_FILENAME = "settings.json"
SETTINGS_SCHEMA_VERSION = 1
MAX_RECENT_BOARDS = 10


class Theme(StrEnum):
    DARK = "dark"
    LIGHT = "light"


class ComputeBackendChoice(StrEnum):
    CPU = "cpu"
    GPU = "gpu"  # selectable only once a GPU backend exists (later stage)


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)


class ViewerSettings(_Model):
    grid_visible: bool = True
    grid_spacing_mm: float = Field(default=1.0, gt=0.0, le=100.0)
    show_reference_labels: bool = True
    show_footprint_bodies: bool = True


class AIProviderSettings(_Model):
    """Placeholder section. Inactive in Stage 1. Holds no secrets, ever."""

    enabled: Literal[False] = False
    note: str = "AI providers are available in a later stage."


class RoutingSettings(_Model):
    """Placeholder section. Inactive in Stage 1."""

    enabled: Literal[False] = False
    note: str = "Autorouting is available in a later stage."


class GPUSettings(_Model):
    """Placeholder section. Inactive in Stage 1."""

    enabled: Literal[False] = False
    note: str = "GPU acceleration is available in a later stage."


class WindowSettings(_Model):
    geometry_b64: str | None = None  # QMainWindow.saveGeometry(), base64
    state_b64: str | None = None  # QMainWindow.saveState(), base64
    panel_visibility: dict[str, bool] = Field(default_factory=dict)


class AppSettings(_Model):
    schema_version: int = SETTINGS_SCHEMA_VERSION
    theme: Theme = Theme.DARK
    last_open_directory: str | None = None
    recent_boards: list[str] = Field(default_factory=list)
    default_compute_backend: ComputeBackendChoice = ComputeBackendChoice.CPU
    viewer: ViewerSettings = Field(default_factory=ViewerSettings)
    window: WindowSettings = Field(default_factory=WindowSettings)
    ai: AIProviderSettings = Field(default_factory=AIProviderSettings)
    routing: RoutingSettings = Field(default_factory=RoutingSettings)
    gpu: GPUSettings = Field(default_factory=GPUSettings)

    @field_validator("recent_boards")
    @classmethod
    def _dedupe_recent(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for item in value:
            if item and item not in seen:
                seen.append(item)
        return seen[:MAX_RECENT_BOARDS]

    def add_recent_board(self, path: Path | str) -> None:
        text = str(Path(path))
        self.recent_boards = [text, *[p for p in self.recent_boards if p != text]]

    def remove_recent_board(self, path: Path | str) -> None:
        text = str(Path(path))
        self.recent_boards = [p for p in self.recent_boards if p != text]


class SettingsStore:
    """Loads and saves :class:`AppSettings` at a fixed path."""

    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else config_dir() / SETTINGS_FILENAME
        self.last_load_problem: str | None = None

    def load(self) -> AppSettings:
        self.last_load_problem = None
        if not self.path.exists():
            log.info("settings.load path=%s status=missing using=defaults", self.path)
            return AppSettings()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("top-level JSON value is not an object")
            settings = AppSettings.model_validate(raw)
        except (OSError, ValueError, ValidationError) as exc:
            self.last_load_problem = f"{type(exc).__name__}: {exc}"
            backup = self._quarantine()
            log.warning(
                "settings.load path=%s status=invalid backup=%s error=%s",
                self.path, backup, self.last_load_problem,
            )  # fmt: skip
            return AppSettings()
        log.info("settings.load path=%s status=ok", self.path)
        return settings

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = settings.model_dump_json(indent=2)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".settings-", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        log.info("settings.save path=%s", self.path)

    def _quarantine(self) -> Path | None:
        backup = self.path.with_name(f"{self.path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            os.replace(self.path, backup)
            return backup
        except OSError:
            return None
