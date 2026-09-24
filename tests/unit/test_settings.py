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
    assert s.ai.profiles == [] and s.ai.default_profile is None
    assert s.ai.show_privacy_preview is True and s.ai.debug_log_prompts is False
    assert s.routing.enabled is False and s.gpu.enabled is False


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
        json.dumps({"schema_version": 2, "ai": {"request_timeout_s": -1}}),
        json.dumps({"routing": {"enabled": True}}),  # future sections cannot be switched on
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


def _fields(schema: object) -> dict[str, set[str]]:
    """Every property name in the schema -> the JSON types it may hold."""
    found: dict[str, set[str]] = {}

    def types(prop: object) -> set[str]:
        if not isinstance(prop, dict):
            return set()
        out = {prop["type"]} if isinstance(prop.get("type"), str) else set()
        for sub in prop.get("anyOf", []):
            out |= types(sub)
        return out or {"object"}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                for name, prop in props.items():
                    found.setdefault(name.lower(), set()).update(types(prop))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return found


def test_settings_schema_has_no_secret_fields() -> None:
    fields = _fields(AppSettings.model_json_schema())
    assert "grid_spacing_mm" in fields  # sanity: the walk sees nested sections
    assert "credential_ref" in fields  # profiles store a keyring *reference* only
    secretish = ("key", "token", "secret", "password", "authorization", "bearer")
    for name, kinds in fields.items():
        if "string" in kinds and name != "credential_ref":
            assert not any(word in name for word in secretish), name
