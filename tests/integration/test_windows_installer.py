"""Windows installer: compile with makensis, then run the real installer under Wine.

* ``test_installer_compiles`` needs ``makensis`` (``apt install nsis``); skipped otherwise.
* ``test_installer_lifecycle_under_wine`` also needs 64-bit Wine and is opt-in
  (``PCBROUTER_TEST_WINE=1``) because the first Wine start takes a while.

The payload is not the real PyInstaller bundle (that can only be built on Windows; see
.github/workflows/windows-installer.yml). The executables are tiny stubs, compiled
with makensis, that log their command line, so the test can check that the
uninstaller calls ``pcbrouter.exe --forget-api-keys`` only when asked to.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
STUB_NSI = ROOT / "tests" / "support" / "stub_exe.nsi"
UNINSTALL_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\AI PCB Router"
APP_KEY = r"HKCU\Software\AI PCB Router"
OPEN_WITH_KEY = r"HKCU\Software\Classes\.kicad_pcb\OpenWithProgids"
BOARD_EXT_KEY = r"HKCU\Software\Classes\.kicad_pcb"
PROGID_KEY = r"HKCU\Software\Classes\AIPCBRouter.kicad_pcb"

MAKENSIS = shutil.which("makensis")
needs_makensis = pytest.mark.skipif(MAKENSIS is None, reason="makensis (NSIS) not installed")


def _load_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_installer", ROOT / "packaging" / "windows" / "build_installer.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(module)
    return module


builder = _load_builder()


def _stub_payload(folder: Path) -> Path:
    assert MAKENSIS is not None
    payload = folder / "payload"
    (payload / "_internal" / "sub").mkdir(parents=True)
    cli = payload / "pcbrouter.exe"
    subprocess.run(
        [MAKENSIS, "-V2", "-WX", f"-DOUTFILE={cli}", str(STUB_NSI)], check=True
    )  # fmt: skip
    shutil.copy(cli, payload / "AI PCB Router.exe")
    (payload / "_internal" / "lib.dll").write_bytes(b"stub library")
    (payload / "_internal" / "sub" / "data file.txt").write_text("stub data")
    return payload


def _build(payload: Path, out: Path, version: str) -> Path:
    code = builder.main(
        [
            "--payload", str(payload),
            "--output", str(out),
            "--work", str(out / "work"),
            "--version-override", version,
        ]
    )  # fmt: skip
    assert code == 0
    return out / str(builder.installer_filename(version))


@needs_makensis
def test_installer_compiles(tmp_path: Path) -> None:
    setup = _build(_stub_payload(tmp_path), tmp_path / "out", "0.2.0-stage2")
    assert setup.is_file()
    assert setup.read_bytes()[:2] == b"MZ"  # a Windows executable


# ---------------------------------------------------------------------------- Wine


def _wine_binary() -> str | None:
    return shutil.which("wine64") or next(
        (p for p in ("/usr/lib/wine/wine64", "/usr/lib/wine/wine") if Path(p).is_file()), None
    )


WINE = _wine_binary()
wine_opt_in = pytest.mark.skipif(
    MAKENSIS is None or WINE is None or os.environ.get("PCBROUTER_TEST_WINE") != "1",
    reason="needs makensis + 64-bit Wine and PCBROUTER_TEST_WINE=1",
)


class WineBox:
    """A throwaway Wine prefix with helpers to run programs and read the registry."""

    def __init__(self, root: Path) -> None:
        assert WINE is not None
        self.wine = WINE
        self.wineserver = str(Path(WINE).with_name("wineserver"))
        home = root / "home"
        home.mkdir()
        self.env = dict(
            os.environ, WINEPREFIX=str(root / "prefix"), WINEDEBUG="-all", HOME=str(home)
        )

    def run(self, *args: str, timeout: float = 300) -> int:
        result = subprocess.run(
            [self.wine, *args], env=self.env, capture_output=True, timeout=timeout, check=False
        )
        return result.returncode

    def wait_idle(self) -> None:
        """Uninstall.exe re-launches itself from %TEMP%; wait for every process."""
        subprocess.run([self.wineserver, "-w"], env=self.env, timeout=120, check=False)

    def reg_value(self, key: str, name: str | None) -> str | None:
        args = [self.wine, "reg", "query", key] + (["/v", name] if name else ["/ve"])
        result = subprocess.run(
            args, env=self.env, capture_output=True, text=True, timeout=60, check=False
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 2)
            label = name if name else "(Default)"
            if parts and parts[0] == label and len(parts) >= 2:
                return parts[2] if len(parts) == 3 else ""
        return None

    def key_exists(self, key: str) -> bool:
        result = subprocess.run(
            [self.wine, "reg", "query", key], env=self.env, capture_output=True, check=False
        )  # fmt: skip
        return result.returncode == 0

    def reg_add(self, key: str, name: str | None, data: str) -> None:
        args = [self.wine, "reg", "add", key] + (["/v", name] if name else ["/ve"])
        subprocess.run([*args, "/d", data, "/f"], env=self.env, capture_output=True, check=True)

    @property
    def user_dir(self) -> Path:
        users = Path(self.env["WINEPREFIX"]) / "drive_c" / "users"
        candidates = [p for p in users.iterdir() if p.name.lower() != "public"]
        assert len(candidates) == 1, candidates
        return candidates[0]


@pytest.fixture
def wine_box(tmp_path: Path) -> Iterator[WineBox]:
    box = WineBox(tmp_path)
    yield box
    subprocess.run([box.wineserver, "-k"], env=box.env, check=False)


@wine_opt_in
def test_installer_lifecycle_under_wine(tmp_path: Path, wine_box: WineBox) -> None:
    payload = _stub_payload(tmp_path)
    setup_v1 = _build(payload, tmp_path / "v1", "0.2.0-stage2")
    setup_v2 = _build(payload, tmp_path / "v2", "0.2.1-test")
    box = wine_box

    # --- 1. fresh silent install (defaults: all components) -------------------------
    assert box.run(str(setup_v1), "/S") == 0
    user = box.user_dir
    local, roaming = user / "AppData" / "Local", user / "AppData" / "Roaming"
    instdir = local / "Programs" / "AI PCB Router"
    installed = {p.relative_to(instdir).as_posix() for p in instdir.rglob("*") if p.is_file()}
    assert installed == {
        "AI PCB Router.exe",
        "pcbrouter.exe",
        "Uninstall.exe",
        "_internal/lib.dll",
        "_internal/sub/data file.txt",
    }
    assert box.reg_value(UNINSTALL_KEY, "DisplayVersion") == "0.2.0-stage2"
    assert box.reg_value(UNINSTALL_KEY, "Publisher") == "Davi"
    assert "Uninstall.exe" in (box.reg_value(UNINSTALL_KEY, "QuietUninstallString") or "")
    assert box.reg_value(OPEN_WITH_KEY, "AIPCBRouter.kicad_pcb") is not None
    command = box.reg_value(PROGID_KEY + r"\shell\open\command", None) or ""
    assert command.endswith('AI PCB Router.exe" "%1"')
    start_menu = roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    assert (start_menu / "AI PCB Router.lnk").is_file()
    assert (user / "Desktop" / "AI PCB Router.lnk").is_file()

    # Things Setup does not own: KiCad's association, user data, a user's own file.
    box.reg_add(BOARD_EXT_KEY, None, "KiCad.pcb")
    box.reg_add(OPEN_WITH_KEY, "KiCad.pcb", "")
    settings = roaming / "AI PCB Router" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{}")
    log_file = local / "AI PCB Router" / "logs" / "pcbrouter.log"
    log_file.parent.mkdir(parents=True)
    log_file.write_text("log")
    (instdir / "my notes.txt").write_text("user file")
    calls = local / "pcbrouter-stub-calls.log"

    # --- 2. upgrade in place: old version removed by its own uninstaller ----------
    assert box.run(str(setup_v2), "/S") == 0
    box.wait_idle()
    assert box.reg_value(UNINSTALL_KEY, "DisplayVersion") == "0.2.1-test"
    assert (instdir / "AI PCB Router.exe").is_file()
    assert (instdir / "_internal" / "sub" / "data file.txt").is_file()
    assert settings.is_file() and log_file.is_file()
    assert (instdir / "my notes.txt").is_file()
    assert not calls.exists() or "--forget-api-keys" not in calls.read_text("utf-8", "replace")

    # --- 3. uninstall, keeping user data (the default) ---------------------------
    assert box.run(str(instdir / "Uninstall.exe"), "/S") == 0
    box.wait_idle()
    remaining = {p.relative_to(instdir).as_posix() for p in instdir.rglob("*")}
    assert remaining == {"my notes.txt"}  # only what Setup did not install
    assert not box.key_exists(UNINSTALL_KEY)
    assert not box.key_exists(APP_KEY)
    assert not box.key_exists(PROGID_KEY)
    assert box.reg_value(OPEN_WITH_KEY, "AIPCBRouter.kicad_pcb") is None
    assert box.reg_value(OPEN_WITH_KEY, "KiCad.pcb") is not None  # KiCad untouched
    assert box.reg_value(BOARD_EXT_KEY, None) == "KiCad.pcb"
    assert not (start_menu / "AI PCB Router.lnk").exists()
    assert not (user / "Desktop" / "AI PCB Router.lnk").exists()
    assert settings.is_file() and log_file.is_file()
    assert not calls.exists() or "--forget-api-keys" not in calls.read_text("utf-8", "replace")

    # --- 4. reinstall, then uninstall removing user data ----------------------------
    assert box.run(str(setup_v2), "/S") == 0
    assert box.run(str(instdir / "Uninstall.exe"), "/S", "/REMOVEUSERDATA") == 0
    box.wait_idle()
    assert "--forget-api-keys" in calls.read_text("utf-8", "replace")
    assert not settings.parent.exists()
    assert not (local / "AI PCB Router").exists()
    assert {p.name for p in instdir.iterdir()} == {"my notes.txt"}
    assert box.reg_value(BOARD_EXT_KEY, None) == "KiCad.pcb"

    # --- 5. custom folder via /D, then uninstall removes the folder entirely -----------
    # NSIS wants /D= last and unquoted; an argv list would quote a path with spaces
    # (spaces are already covered by the default "AI PCB Router" folder).
    assert box.run(str(setup_v1), "/S", r"/D=C:\Tools\PCBRouter") == 0
    custom = Path(box.env["WINEPREFIX"]) / "drive_c" / "Tools" / "PCBRouter"
    assert (custom / "AI PCB Router.exe").is_file()
    assert box.reg_value(UNINSTALL_KEY, "InstallLocation") == r"C:\Tools\PCBRouter"
    assert box.run(str(custom / "Uninstall.exe"), "/S") == 0
    box.wait_idle()
    assert not custom.exists()
    assert (custom.parent).is_dir()  # the parent folder the user chose is never removed
