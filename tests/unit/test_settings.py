from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from pcbrouter.settings import (
    MAX_RECENT_BOARDS,
    AppSettings,
    ComputeBackendChoice,
    SettingsStore,
    Theme,
)
from pcbrouter.utils.paths import config_dir


def test_defaults() -> None:
    s = AppSettings()
    assert s.theme is Theme.DARK
    assert s.viewer.grid_visible is True
    assert s.viewer.grid_spacing_mm == 1.0
    assert s.default_compute_backend is ComputeBackendChoice.CPU
    assert s.recent_boards == []
    assert s.ai.enabled is False and s.routing.enabled is False and s.gpu.enabled is False


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    assert store.load() == AppSettings()
    assert store.last_load_problem is None


def test_roundtrip(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "nested" / "settings.json")
    s = AppSettings()
    s.theme = Theme.LIGHT
    s.last_open_directory = str(tmp_path)
    s.add_recent_board(tmp_path / "a.kicad_pcb")
    s.viewer.grid_visible = False
    s.viewer.grid_spacing_mm = 0.635
    s.window.geometry_b64 = "AAEC"
    s.window.panel_visibility = {"log": False, "nets": True}
    store.save(s)
    loaded = SettingsStore(store.path).load()
    assert loaded == s
    assert not list(store.path.parent.glob(".settings-*.tmp"))  # atomic write cleaned up


def test_default_location_uses_config_dir() -> None:
    assert SettingsStore().path == config_dir() / "settings.json"


@pytest.mark.parametrize(
    "content",
    [
        "{ not json",
        "[1, 2, 3]",
        json.dumps({"viewer": {"grid_spacing_mm": -5}}),
        json.dumps({"theme": "neon"}),
        json.dumps({"ai": {"enabled": True}}),  # future sections cannot be switched on
    ],
)
def test_corrupt_file_is_quarantined(tmp_path: Path, content: str) -> None:
    path = tmp_path / "settings.json"
    path.write_text(content, encoding="utf-8")
    store = SettingsStore(path)
    assert store.load() == AppSettings()
    assert store.last_load_problem
    backups = list(tmp_path.glob("settings.json.corrupt-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == content
    assert not path.exists()


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"theme": "light", "from_the_future": 1}), encoding="utf-8")
    assert SettingsStore(path).load().theme is Theme.LIGHT


def test_recent_boards_order_dedupe_and_limit(tmp_path: Path) -> None:
    s = AppSettings()
    for i in range(MAX_RECENT_BOARDS + 5):
        s.add_recent_board(tmp_path / f"b{i}.kicad_pcb")
    assert len(s.recent_boards) == MAX_RECENT_BOARDS
    s.add_recent_board(tmp_path / "b7.kicad_pcb")
    assert s.recent_boards[0].endswith("b7.kicad_pcb")
    assert len(set(s.recent_boards)) == len(s.recent_boards)
    s.remove_recent_board(tmp_path / "b7.kicad_pcb")
    assert not any(p.endswith("b7.kicad_pcb") for p in s.recent_boards)


def test_validation_on_assignment() -> None:
    s = AppSettings()
    with pytest.raises(ValidationError):
        s.viewer.grid_spacing_mm = 0


def _field_names(schema: object) -> set[str]:
    names: set[str] = set()
    if isinstance(schema, dict):
        props = schema.get("properties")
        if isinstance(props, dict):
            names.update(k.lower() for k in props)
        for value in schema.values():
            names |= _field_names(value)
    elif isinstance(schema, list):
        for value in schema:
            names |= _field_names(value)
    return names


def test_settings_schema_has_no_secret_fields() -> None:
    names = _field_names(AppSettings.model_json_schema())
    assert "grid_spacing_mm" in names  # sanity: the walk sees nested sections
    for name in names:
        for forbidden in ("key", "password", "secret", "token", "credential"):
            assert forbidden not in name, name
