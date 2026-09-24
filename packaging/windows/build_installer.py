"""Build the Windows installer: PyInstaller bundle -> NSIS setup wizard.

Run on Windows from the repository root, in a venv with the app installed::

    pip install -e ".[ai,packaging]"
    python packaging/windows/build_installer.py

Steps:

1. Read the version from ``src/pcbrouter/__init__.py`` and the publisher from
   ``pyproject.toml`` (single sources of truth).
2. Run PyInstaller with ``pcbrouter.spec`` -> ``dist/AI PCB Router/`` (skipped with
   ``--payload DIR``, which packages an existing bundle).
3. Scan the bundle and generate two NSIS include files: the exact list of files to
   install and the exact list to delete on uninstall. The uninstaller therefore never
   runs a recursive delete on the install folder, which the user may have chosen.
4. Run ``makensis`` on ``installer.nsi`` with warnings treated as errors ->
   ``dist/installer/AI-PCB-Router-<version>-Setup-x64.exe``.

makensis also runs on Linux (``apt install nsis``), so step 4 works there with
``--payload``; PyInstaller can only build for the OS it runs on.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WINDOWS_DIR = ROOT / "packaging" / "windows"
NSI_SCRIPT = WINDOWS_DIR / "installer.nsi"
SPEC_FILE = WINDOWS_DIR / "pcbrouter.spec"
ASSETS_DIR = WINDOWS_DIR / "assets"
LICENSE_FILE = ROOT / "LICENSE"

APP_NAME = "AI PCB Router"
GUI_EXE = f"{APP_NAME}.exe"
CLI_EXE = "pcbrouter.exe"
REQUIRED_EXES = (GUI_EXE, CLI_EXE)
#: Names the installer itself creates in $INSTDIR; a payload must not contain them.
RESERVED_NAMES = frozenset({"uninstall.exe"})

_VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)
_NUMERIC_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?")
#: Characters NSIS or Windows cannot carry in a quoted file path.
_FORBIDDEN_PATH_CHARS = frozenset('"*?<>|\n\r\t`')


class BuildError(Exception):
    """A build step failed; the message says what to fix."""


# ---------------------------------------------------------------- versions / metadata


def read_version(root: Path = ROOT) -> str:
    text = (root / "src" / "pcbrouter" / "__init__.py").read_text(encoding="utf-8")
    match = _VERSION_RE.search(text)
    if not match:
        raise BuildError("__version__ not found in src/pcbrouter/__init__.py")
    return match.group(1)


def read_publisher(root: Path = ROOT) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    authors = project.get("authors") or [{}]
    return str(authors[0].get("name") or APP_NAME)


def numeric_version(version: str) -> tuple[int, int, int, int]:
    """Windows version resources need four integers: ``0.2.0-stage2`` -> (0, 2, 0, 0)."""
    match = _NUMERIC_RE.match(version)
    if not match:
        raise BuildError(f"version {version!r} does not start with a number")
    parts = tuple(int(p) if p is not None else 0 for p in match.groups())
    if any(p > 65535 for p in parts):
        raise BuildError(f"version {version!r} has a component above 65535")
    return parts  # type: ignore[return-value]


def installer_filename(version: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z.\-]+", "-", version)
    return f"AI-PCB-Router-{safe}-Setup-x64.exe"


def version_info_text(version: str, publisher: str) -> str:
    """PyInstaller version-resource file (shown in Explorer > Properties > Details)."""
    nums = numeric_version(version)
    strings = {
        "CompanyName": publisher,
        "FileDescription": APP_NAME,
        "FileVersion": version,
        "InternalName": "pcbrouter",
        "LegalCopyright": "MIT License",
        "ProductName": APP_NAME,
        "ProductVersion": version,
    }
    table = ",\n          ".join(f"StringStruct({k!r}, {v!r})" for k, v in strings.items())
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={nums}, prodvers={nums}, mask=0x3F, flags=0x0,
    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
          {table}
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


# ---------------------------------------------------------------- payload / file lists


@dataclass(frozen=True)
class Payload:
    root: Path
    files: tuple[Path, ...]  # relative, sorted
    dirs: tuple[Path, ...]  # relative, sorted, excluding the root itself


def scan_payload(payload_dir: Path) -> Payload:
    root = payload_dir.resolve()
    if not root.is_dir():
        raise BuildError(f"payload folder not found: {payload_dir}")
    files: list[Path] = []
    dirs: list[Path] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_symlink():
            raise BuildError(f"payload contains a symbolic link: {rel}")
        bad = _FORBIDDEN_PATH_CHARS.intersection(str(rel))
        if bad:
            raise BuildError(f"payload path {rel} contains unsupported characters {sorted(bad)}")
        if path.is_dir():
            dirs.append(rel)
        else:
            files.append(rel)
    top_level = {f.name.lower() for f in files if len(f.parts) == 1}
    for exe in REQUIRED_EXES:
        if exe.lower() not in top_level:
            raise BuildError(
                f"payload has no {exe!r} at its top level; build it with pcbrouter.spec "
                "on Windows (PyInstaller cannot cross-compile)"
            )
    clash = top_level & RESERVED_NAMES
    if clash:
        raise BuildError(f"payload contains names reserved by the installer: {sorted(clash)}")
    return Payload(root, tuple(files), tuple(dirs))


def _check_quotable(text: str) -> None:
    if any(c in text for c in '"\n\r'):
        raise BuildError(f"cannot quote {text!r} for NSIS")


def nsis_runtime_string(text: str) -> str:
    """Escape for an NSIS string evaluated at install time, where ``$`` starts a
    variable (``$INSTDIR``) and ``$$`` is a literal dollar sign."""
    _check_quotable(text)
    return text.replace("$", "$$")


def nsis_compile_time_path(text: str) -> str:
    """Quote a build-machine path for ``File``/``!include``. makensis reads these at
    compile time: ``$`` is literal there, but ``${..}``, ``$(..)`` and ``$%..%`` would
    still be expanded, so those sequences are refused rather than escaped."""
    _check_quotable(text)
    if any(seq in text for seq in ("${", "$(", "$%")):
        raise BuildError(f"path {text!r} contains an NSIS expansion sequence")
    return text


def _define_value(text: str) -> str:
    """-D values are pasted into both compile-time and install-time strings, so the
    only safe policy is: no ``$`` and no quotes at all."""
    _check_quotable(text)
    if "$" in text:
        raise BuildError(f"value {text!r} for makensis must not contain '$'")
    return text


def _instdir_path(rel: Path) -> str:
    return "$INSTDIR" + "".join("\\" + nsis_runtime_string(part) for part in rel.parts)


def render_install_list(payload: Payload) -> str:
    """``SetOutPath`` + ``File`` for every file, grouped by folder."""
    lines = ["; Generated by build_installer.py -- do not edit.", ""]
    by_dir: dict[Path, list[Path]] = {}
    for rel in payload.files:
        by_dir.setdefault(rel.parent, []).append(rel)
    for folder in sorted(by_dir, key=lambda p: (len(p.parts), p.parts)):
        lines.append(f'SetOutPath "{_instdir_path(folder)}"')
        for rel in by_dir[folder]:
            lines.append(f'File "{nsis_compile_time_path(str(payload.root / rel))}"')
    lines += ['SetOutPath "$INSTDIR"', ""]
    return "\n".join(lines)


def render_uninstall_list(payload: Payload) -> str:
    """Delete exactly the installed files, then remove folders deepest first.

    ``RMDir`` without ``/r`` only removes an empty folder, so anything the user put
    into the install folder survives the uninstall.
    """
    lines = ["; Generated by build_installer.py -- do not edit.", ""]
    lines += [f'Delete "{_instdir_path(rel)}"' for rel in payload.files]
    deepest_first = sorted(payload.dirs, key=lambda p: (-len(p.parts), p.parts))
    lines += [f'RMDir "{_instdir_path(rel)}"' for rel in deepest_first]
    lines.append("")
    return "\n".join(lines)


def write_file_lists(payload: Payload, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    install = out_dir / "files_install.nsh"
    uninstall = out_dir / "files_uninstall.nsh"
    # UTF-8 with BOM: makensis then reads non-ASCII file names correctly.
    install.write_text(render_install_list(payload), encoding="utf-8-sig")
    uninstall.write_text(render_uninstall_list(payload), encoding="utf-8-sig")
    return install, uninstall


# ---------------------------------------------------------------- tools


def find_makensis(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    found = shutil.which("makensis")
    if found:
        return found
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env)
        if base:
            candidate = Path(base) / "NSIS" / "makensis.exe"
            if candidate.is_file():
                return str(candidate)
    raise BuildError(
        "makensis not found. Install NSIS 3 (https://nsis.sourceforge.io, or "
        "'winget install NSIS.NSIS' / 'choco install nsis'; 'apt install nsis' on Linux) "
        "or pass --makensis PATH."
    )


def makensis_command(
    makensis: str,
    *,
    version: str,
    publisher: str,
    install_list: Path,
    uninstall_list: Path,
    outfile: Path,
) -> list[str]:
    nums = numeric_version(version)
    defines = {
        "APP_VERSION": version,
        "APP_VERSION_NUMERIC": ".".join(str(n) for n in nums),
        "APP_PUBLISHER": publisher,
        "FILES_INSTALL": str(install_list),
        "FILES_UNINSTALL": str(uninstall_list),
        "OUTFILE": str(outfile),
        "ASSETS_DIR": str(ASSETS_DIR),
        "LICENSE_FILE": str(LICENSE_FILE),
    }
    return [
        makensis,
        "-V2",
        "-WX",  # warnings are errors: a sloppy script must not ship
        "-INPUTCHARSET",
        "UTF8",
        *(f"-D{k}={_define_value(v)}" for k, v in defines.items()),
        str(NSI_SCRIPT),
    ]


def run_pyinstaller(version: str, publisher: str, work: Path, dist: Path) -> Path:
    version_file = work / "version_info.txt"
    version_file.parent.mkdir(parents=True, exist_ok=True)
    version_file.write_text(version_info_text(version, publisher), encoding="utf-8")
    env = dict(os.environ, PCBROUTER_VERSION_FILE=str(version_file))
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(dist),
        "--workpath",
        str(work / "pyinstaller"),
        str(SPEC_FILE),
    ]
    print("running:", " ".join(cmd), flush=True)
    result = subprocess.run(cmd, env=env, check=False)
    if result.returncode != 0:
        raise BuildError(f"PyInstaller failed with exit code {result.returncode}")
    return dist / APP_NAME


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------- main


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--payload", type=Path, help="package an existing bundle folder")
    parser.add_argument(
        "--skip-installer", action="store_true", help="only build the PyInstaller bundle"
    )
    parser.add_argument("--makensis", help="path to makensis (default: PATH / Program Files)")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "installer")
    parser.add_argument("--work", type=Path, default=ROOT / "build" / "windows")
    parser.add_argument("--version-override", help=argparse.SUPPRESS)  # tests only
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        version = args.version_override or read_version()
        publisher = read_publisher()
        print(f"{APP_NAME} {version} (publisher: {publisher})")

        if args.payload is None:
            if sys.platform != "win32" and not args.skip_installer:
                raise BuildError(
                    "PyInstaller builds for the OS it runs on. Run this on Windows, or "
                    "pass --payload DIR to package an existing Windows bundle."
                )
            bundle = run_pyinstaller(version, publisher, args.work, ROOT / "dist")
        else:
            bundle = args.payload
        if args.skip_installer:
            print(f"bundle: {bundle}")
            return 0

        payload = scan_payload(bundle)
        install_list, uninstall_list = write_file_lists(payload, args.work)
        print(f"payload: {len(payload.files)} files in {len(payload.dirs)} folders")

        args.output.mkdir(parents=True, exist_ok=True)
        outfile = (args.output / installer_filename(version)).resolve()
        cmd = makensis_command(
            find_makensis(args.makensis),
            version=version,
            publisher=publisher,
            install_list=install_list,
            uninstall_list=uninstall_list,
            outfile=outfile,
        )
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            raise BuildError(f"makensis failed with exit code {result.returncode}")
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    size_mb = outfile.stat().st_size / 1e6
    print(f"installer: {outfile} ({size_mb:.1f} MB)")
    print(f"sha256:    {sha256_of(outfile)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
