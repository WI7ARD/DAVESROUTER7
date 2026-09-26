"""Windows packaging: version handling, generated NSIS file lists, script policy."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

import pcbrouter

ROOT = Path(__file__).resolve().parents[2]
NSI = ROOT / "packaging" / "windows" / "installer.nsi"


def _load_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_installer", ROOT / "packaging" / "windows" / "build_installer.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


b = _load_builder()


def _payload(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    root = tmp_path / "bundle"
    files = {
        "AI PCB Router.exe": "gui",
        "pcbrouter.exe": "cli",
        "_internal/python312.dll": "py",
        "_internal/PySide6/plugins/platforms/qwindows.dll": "qt",
        **(extra or {}),
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


# ------------------------------------------------------------------- versions


def test_version_comes_from_the_package() -> None:
    assert b.read_version() == pcbrouter.__version__
    assert b.read_publisher() == "Davi"


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.2.0-stage2", (0, 2, 0, 0)),
        ("1.2.3", (1, 2, 3, 0)),
        ("10", (10, 0, 0, 0)),
        ("1.2.3.4.5", (1, 2, 3, 4)),
        ("2.0rc1", (2, 0, 0, 0)),
    ],
)
def test_numeric_version(version: str, expected: tuple[int, ...]) -> None:
    assert b.numeric_version(version) == expected


@pytest.mark.parametrize("version", ["stage2", "", "70000.0.0"])
def test_numeric_version_rejects_unusable_versions(version: str) -> None:
    with pytest.raises(b.BuildError):
        b.numeric_version(version)


def test_installer_filename_is_filesystem_safe() -> None:
    assert b.installer_filename("0.2.0-stage2") == "AI-PCB-Router-0.2.0-stage2-Setup-x64.exe"
    assert b.installer_filename("1.0 beta/2") == "AI-PCB-Router-1.0-beta-2-Setup-x64.exe"


def test_version_info_is_valid_python_expression() -> None:
    text = b.version_info_text("0.2.0-stage2", "Davi")
    assert "filevers=(0, 2, 0, 0)" in text
    assert "StringStruct('ProductVersion', '0.2.0-stage2')" in text
    compile(text, "version_info", "eval")  # PyInstaller evaluates this file


# ------------------------------------------------------------------- payload scan


def test_scan_payload_lists_files_and_folders(tmp_path: Path) -> None:
    payload = b.scan_payload(_payload(tmp_path))
    assert {p.as_posix() for p in payload.files} == {
        "AI PCB Router.exe",
        "pcbrouter.exe",
        "_internal/python312.dll",
        "_internal/PySide6/plugins/platforms/qwindows.dll",
    }
    assert {p.as_posix() for p in payload.dirs} == {
        "_internal",
        "_internal/PySide6",
        "_internal/PySide6/plugins",
        "_internal/PySide6/plugins/platforms",
    }


@pytest.mark.parametrize("missing", ["AI PCB Router.exe", "pcbrouter.exe"])
def test_scan_payload_requires_both_executables(tmp_path: Path, missing: str) -> None:
    root = _payload(tmp_path)
    (root / missing).unlink()
    with pytest.raises(b.BuildError, match="PyInstaller cannot cross-compile"):
        b.scan_payload(root)


def test_scan_payload_rejects_linux_bundle_and_reserved_names(tmp_path: Path) -> None:
    root = _payload(tmp_path, {"Uninstall.exe": "not ours"})
    with pytest.raises(b.BuildError, match="reserved"):
        b.scan_payload(root)
    with pytest.raises(b.BuildError, match="not found"):
        b.scan_payload(tmp_path / "nope")


def test_scan_payload_rejects_symlinks(tmp_path: Path) -> None:
    root = _payload(tmp_path)
    try:
        (root / "link.dll").symlink_to(root / "pcbrouter.exe")
    except OSError:
        pytest.skip("symlinks not permitted here")
    with pytest.raises(b.BuildError, match="symbolic link"):
        b.scan_payload(root)


# ------------------------------------------------------------------- file lists


def test_install_list_sets_out_path_per_folder(tmp_path: Path) -> None:
    payload = b.scan_payload(_payload(tmp_path))
    text = b.render_install_list(payload)
    lines = text.splitlines()
    i = lines.index('SetOutPath "$INSTDIR\\_internal\\PySide6\\plugins\\platforms"')
    assert lines[i + 1].startswith('File "') and lines[i + 1].endswith('qwindows.dll"')
    assert lines[-1] == 'SetOutPath "$INSTDIR"'  # shortcuts get the right working dir
    assert text.count('File "') == len(payload.files)


def test_uninstall_list_is_exact_and_never_recursive(tmp_path: Path) -> None:
    payload = b.scan_payload(_payload(tmp_path))
    text = b.render_uninstall_list(payload)
    assert "/r" not in text  # never a recursive delete of a user-chosen folder
    deletes = re.findall(r'^Delete "(.+)"$', text, re.MULTILINE)
    assert len(deletes) == len(payload.files)
    assert all(d.startswith("$INSTDIR\\") for d in deletes)
    rmdirs = re.findall(r'^RMDir "(.+)"$', text, re.MULTILINE)
    depths = [d.count("\\") for d in rmdirs]
    assert depths == sorted(depths, reverse=True)  # deepest first
    assert "$INSTDIR" not in rmdirs  # the folder itself is handled by the script


def test_dollar_signs_are_escaped_only_where_nsis_expands_them(tmp_path: Path) -> None:
    payload = b.scan_payload(_payload(tmp_path, {"_internal/cost$1.txt": "x"}))
    install = b.render_install_list(payload)
    uninstall = b.render_uninstall_list(payload)
    assert 'Delete "$INSTDIR\\_internal\\cost$$1.txt"' in uninstall  # runtime string
    assert 'cost$1.txt"' in install  # compile-time source path: literal


@pytest.mark.parametrize("bad", ['a"b', "line\nbreak", "tab\there", "star*"])
def test_unquotable_paths_are_refused(tmp_path: Path, bad: str) -> None:
    if sys.platform == "win32" and any(c in bad for c in '"*\t'):
        pytest.skip("Windows cannot even create this file name")
    root = _payload(tmp_path)
    try:
        (root / bad).write_text("x")
    except OSError:
        pytest.skip("filesystem refuses this name")
    with pytest.raises(b.BuildError):
        b.scan_payload(root)


def test_compile_time_path_refuses_nsis_expansions() -> None:
    assert b.nsis_compile_time_path(r"C:\build\a$b") == r"C:\build\a$b"
    for bad in (r"C:\${X}", r"C:\$(lang)", r"C:\$%TEMP%"):
        with pytest.raises(b.BuildError):
            b.nsis_compile_time_path(bad)


def test_makensis_command_passes_all_defines(tmp_path: Path) -> None:
    cmd = b.makensis_command(
        "makensis",
        version="0.2.0-stage2",
        publisher="Davi",
        install_list=tmp_path / "i.nsh",
        uninstall_list=tmp_path / "u.nsh",
        outfile=tmp_path / "Setup.exe",
    )
    assert "-WX" in cmd  # warnings are errors
    defines = {a[2:].split("=", 1)[0] for a in cmd if a.startswith("-D")}
    nsi = NSI.read_text(encoding="utf-8")
    required = re.search(r"^!ifndef (.+)$", nsi, re.MULTILINE)
    assert required is not None
    assert defines == {d.strip() for d in required.group(1).split("|")}
    assert cmd[-1] == str(NSI)
    with pytest.raises(b.BuildError):
        b.makensis_command(
            "makensis",
            version="1$",
            publisher="x",
            install_list=tmp_path,
            uninstall_list=tmp_path,
            outfile=tmp_path,
        )


def test_build_refuses_pyinstaller_on_non_windows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(b.sys, "platform", "linux")
    assert b.main([]) == 1
    assert "--payload" in capsys.readouterr().err


# ------------------------------------------------------------------- script policy


def _nsi_code() -> str:
    """installer.nsi without comments."""
    lines = NSI.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith(";"))


def test_script_installs_per_user_without_admin() -> None:
    code = _nsi_code()
    assert "RequestExecutionLevel user" in code
    assert "HKLM" not in code
    # 32-bit Setup (stock NSIS on Windows has only x86 stubs) for a 64-bit app:
    assert "Target x86-unicode" in code
    assert "${IfNot} ${RunningX64}" in code  # refuses 32-bit Windows
    assert code.count("SetRegView 64") == 2  # installer and uninstaller
    assert 'InstallDir "$LOCALAPPDATA\\Programs\\${APP_NAME}"' in code


def test_script_never_deletes_recursively_outside_app_data_folders() -> None:
    recursive = [line.strip() for line in _nsi_code().splitlines() if "RMDir /r" in line]
    assert recursive == [
        'RMDir /r "$APPDATA\\${APP_NAME}"',
        'RMDir /r "$LOCALAPPDATA\\${APP_NAME}"',
    ]


def test_script_never_takes_over_or_deletes_kicad_association() -> None:
    code = _nsi_code()
    # Only our ProgID and our value under OpenWithProgids; never the extension key,
    # never /ifempty (it ignores values, so it could delete other programs' entries).
    assert "/ifempty" not in code
    assert not re.search(r'DeleteRegKey\s+HKCU\s+"Software\\Classes\\\$\{BOARD_EXT\}', code)
    assert not re.search(r'WriteRegStr\s+HKCU\s+"Software\\Classes\\\$\{BOARD_EXT\}"\s', code)
    assert 'DeleteRegValue HKCU "Software\\Classes\\${BOARD_EXT}\\OpenWithProgids"' in code


def test_user_data_removal_is_opt_in() -> None:
    code = _nsi_code()
    assert re.search(r'Section /o "un\.My settings', code)
    assert "--forget-api-keys" in code


def test_window_icon_resource_is_packaged() -> None:
    icon = ROOT / "src" / "pcbrouter" / "resources" / "app_icon.png"
    assert icon.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    ico = (ROOT / "packaging" / "windows" / "assets" / "app.ico").read_bytes()
    assert ico[:4] == b"\x00\x00\x01\x00" and int.from_bytes(ico[4:6], "little") >= 5
