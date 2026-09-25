"""Persistent application settings (JSON).

Format decision: JSON. The standard library can both read *and* write it (TOML
writing needs a third-party package), it round-trips Pydantic models losslessly,
and the file is small and machine-owned.

Robustness rules:

* A missing file yields defaults.
* A corrupt/invalid file is moved aside to ``settings.json.corrupt-<timestamp>``
  (never deleted) and defaults are used; the problem is logged.
* Writes are atomic (temp file + ``os.replace``) so a crash cannot truncate the file.

SECURITY: settings must never contain API keys or other secrets. Provider
credentials live in the OS keyring and are referenced here only by name
(``ProviderProfile.credential_ref``).
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

from pcbrouter.ai.context_builder import ContextLevel
from pcbrouter.ai.profiles import ProviderProfile
from pcbrouter.ai.requests import AIMode
from pcbrouter.utils.paths import config_dir

log = logging.getLogger(__name__)

SETTINGS_FILENAME = "settings.json"
SETTINGS_SCHEMA_VERSION = 2
MAX_RECENT_BOARDS = 10


class Theme(StrEnum):
    DARK = "dark"
    LIGHT = "light"


class ComputeBackendChoice(StrEnum):
    CPU = "cpu"  # A* on the CPU (reference; always available)
    GPU = "gpu"  # wavefront on the GPU when usable, else CPU fallback per search
    AUTO = "auto"  # GPU only for large grids


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)


class ViewerSettings(_Model):
    grid_visible: bool = True
    grid_spacing_mm: float = Field(default=1.0, gt=0.0, le=100.0)
    show_reference_labels: bool = True
    show_footprint_bodies: bool = True


class AnonymizationSettings(_Model):
    net_names: bool = False
    component_values: bool = False
    references: bool = False
    board_filename: bool = False


class AISettings(_Model):
    """AI provider profiles and engineering-assistant preferences.

    SECURITY: holds no secrets. Each profile stores only ``credential_ref``, the name
    of its OS-keyring entry.
    """

    profiles: list[ProviderProfile] = Field(default_factory=list)
    default_profile_id: str | None = None
    default_mode: AIMode = AIMode.ANALYZE
    context_level: ContextLevel = ContextLevel.STANDARD
    max_context_chars: int = Field(default=60_000, ge=2_000, le=400_000)
    max_context_nets: int = Field(default=200, ge=0, le=5_000)
    max_context_components: int = Field(default=150, ge=0, le=5_000)
    max_conversation_turns: int = Field(default=6, ge=0, le=50)
    request_timeout_s: float = Field(default=90.0, ge=5.0, le=600.0)
    max_retries: int = Field(default=2, ge=0, le=5)
    max_output_tokens: int = Field(default=8192, ge=256, le=64_000)
    show_privacy_preview: bool = True
    anonymization: AnonymizationSettings = Field(default_factory=AnonymizationSettings)
    save_conversation_history: bool = False
    #: Stage 7: what approved AI commands may do. Never a silent autonomous mode.
    autonomy_mode: Literal["advisory", "approval_required", "batch_approval"] = "approval_required"
    #: Log full prompts/context at DEBUG level. Off unless the user explicitly enables it.
    debug_log_prompts: bool = False

    @field_validator("profiles")
    @classmethod
    def _unique_profiles(cls, value: list[ProviderProfile]) -> list[ProviderProfile]:
        ids = [p.profile_id for p in value]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate provider profile ids")
        return value

    def profile(self, profile_id: str | None) -> ProviderProfile | None:
        return next((p for p in self.profiles if p.profile_id == profile_id), None)

    @property
    def default_profile(self) -> ProviderProfile | None:
        return self.profile(self.default_profile_id) or (
            self.profiles[0] if self.profiles else None
        )


class RoutingSettings(_Model):
    """Router preferences (Stages 4-5). Rule values never come from here."""

    enabled: bool = True
    candidates: int = Field(default=3, ge=1, le=5)
    time_limit_s: float = Field(default=30.0, ge=1.0, le=600.0)
    strategy: Literal[
        "critical_first", "most_constrained", "shortest_first", "fewest_escapes",
        "congestion_aware",
    ] = "critical_first"  # fmt: skip
    max_passes: int = Field(default=3, ge=1, le=5)
    allow_ripup: bool = True


class GeometrySettings(_Model):
    """Stage 3 geometry + rule engine preferences (no secrets, no board content)."""

    #: Unknown critical rules make route validation refuse (RULE_UNKNOWN). Default ON.
    #: OFF is an explicit expert choice: unknowns become warnings (never silent).
    conservative_rules: bool = True
    #: Default routing-grid resolution for View ▸ Routing Grid.
    grid_resolution_mm: float = Field(default=0.10, gt=0.0, le=5.0)
    #: Run the Internal Geometry Check automatically after a board opens.
    check_on_open: bool = False


class GPUSettings(_Model):
    """GPU preferences (Stage 6). The backend choice itself is
    ``AppSettings.default_compute_backend``."""

    enabled: bool = False  # legacy flag from earlier versions; not used for decisions


class ExportSettings(_Model):
    """Export and session safety (Stage 9)."""

    #: default False: the source board is never overwritten; exports go to a new file
    allow_overwrite_source: bool = False
    #: run KiCad's own DRC on the exported file when kicad-cli is installed
    run_kicad_drc: bool = False
    #: write a crash-recovery session after every working-board change
    autosave_recovery: bool = True


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
    ai: AISettings = Field(default_factory=AISettings)
    routing: RoutingSettings = Field(default_factory=RoutingSettings)
    geometry: GeometrySettings = Field(default_factory=GeometrySettings)
    gpu: GPUSettings = Field(default_factory=GPUSettings)
    export: ExportSettings = Field(default_factory=ExportSettings)

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


def _migrate(raw: dict[str, object]) -> dict[str, object]:
    """Upgrade older settings files. v1 (Stage 1) had an inert AI placeholder section."""
    version = raw.get("schema_version", 1)
    if isinstance(version, int) and version < 2:
        raw = {k: v for k, v in raw.items() if k != "ai"}
    raw["schema_version"] = SETTINGS_SCHEMA_VERSION
    return raw


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
            settings = AppSettings.model_validate(_migrate(raw))
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
