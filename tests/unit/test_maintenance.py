"""--diagnostics and --forget-api-keys (used by support and the Windows uninstaller)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pcbrouter
from pcbrouter.ai.credentials import CredentialService, SessionCredentialStore
from pcbrouter.ai.profiles import ProviderKind, ProviderProfile
from pcbrouter.app import maintenance
from pcbrouter.app.application import main
from pcbrouter.settings.settings import AppSettings, SettingsStore
from tests.support.mock_provider import FAKE_KEY


class SecureMemoryStore(SessionCredentialStore):
    """Stands in for an OS keychain."""

    secure = True
    name = "test keychain"


def _profiles_saved(path: Path, *ids: str) -> SettingsStore:
    store = SettingsStore(path)
    settings = AppSettings()
    settings.ai.profiles = [
        ProviderProfile(profile_id=i, name=f"Profile {i}", kind=ProviderKind.OPENAI) for i in ids
    ]
    store.save(settings)
    return store


def test_forget_api_keys_deletes_every_profile_key(tmp_path: Path) -> None:
    store = _profiles_saved(tmp_path / "settings.json", "one", "two", "three")
    creds = CredentialService(secure=SecureMemoryStore())
    profiles = store.load().ai.profiles
    creds.save(profiles[0].credential_ref, FAKE_KEY)
    creds.save(profiles[1].credential_ref, FAKE_KEY)
    out: list[str] = []

    assert maintenance.forget_api_keys(store, creds, echo=out.append) == 0

    assert all(creds.get(p.credential_ref) is None for p in profiles)
    assert out[-1] == "2 stored API key(s) removed (3 profile(s) checked)."
    assert not any(FAKE_KEY in line for line in out)
    assert store.path.exists()  # settings are the uninstaller's job, not this command's


def test_forget_api_keys_without_settings_or_secure_store(tmp_path: Path) -> None:
    out: list[str] = []
    missing = SettingsStore(tmp_path / "none.json")
    assert maintenance.forget_api_keys(missing, CredentialService(), echo=out.append) == 0
    assert "nothing to remove" in out[0]

    store = _profiles_saved(tmp_path / "settings.json", "one")
    insecure = CredentialService(secure=SessionCredentialStore())
    assert maintenance.forget_api_keys(store, insecure, echo=out.append) == 0
    assert "no stored keys can exist" in out[-1]


def test_forget_api_keys_reports_unreadable_settings(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    out: list[str] = []
    code = maintenance.forget_api_keys(
        SettingsStore(path), CredentialService(secure=SecureMemoryStore()), echo=out.append
    )
    assert code == 1
    assert "could not be read" in out[0]


def test_diagnostics_report(monkeypatch: pytest.MonkeyPatch) -> None:
    report = maintenance.diagnostics(CredentialService(secure=SecureMemoryStore()))
    assert report["version"] == pcbrouter.__version__
    assert report["frozen"] is False
    assert report["qt"]["available"] is True
    assert set(report["packages"]) == {"openai", "anthropic", "keyring"}
    for sdk in ("openai", "anthropic"):
        assert report["packages"][sdk]["client"] == "ok"  # built offline, never used
    assert report["credential_store"] == {"secure_available": True, "description": "test keychain"}
    json.dumps(report)


def test_diagnostics_reports_missing_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(maintenance, "OPTIONAL_PACKAGES", (("no_such_pkg_xyz", "none"),))
    report = maintenance.diagnostics(CredentialService(secure=SecureMemoryStore()))
    status = report["packages"]["no_such_pkg_xyz"]
    assert status["available"] is False and "ModuleNotFoundError" in status["error"]


def test_cli_flags_write_no_log_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PCBROUTER_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("PCBROUTER_LOG_DIR", str(tmp_path / "logs"))
    assert main(["--diagnostics"]) == 0
    assert json.loads(capsys.readouterr().out)["app"] == "AI PCB Router"
    assert main(["--forget-api-keys"]) == 0
    assert "nothing to remove" in capsys.readouterr().out
    assert not (tmp_path / "logs").exists()


def test_version_flag_survives_missing_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    # The windowed Windows executable has no console: sys.stdout is None.
    monkeypatch.setattr("sys.stdout", None)
    assert main(["--version"]) == 0
