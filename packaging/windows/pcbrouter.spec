# PyInstaller spec for the Windows build. Run through build_installer.py, which sets
# PCBROUTER_VERSION_FILE (Windows version resource) before calling PyInstaller.
#
# Output: dist/AI PCB Router/
#   AI PCB Router.exe   windowed GUI (no console window flashes up)
#   pcbrouter.exe       console build of the same program, for the CLI
#                       (--inspect, --check-command, --diagnostics, --forget-api-keys)
#   _internal/          Python runtime, Qt, dependencies — shared by both executables
#
# Both executables come from one Analysis, so the bundle is stored once.
# ruff: noqa  (PyInstaller injects Analysis, PYZ, EXE, COLLECT and SPECPATH)

import importlib.util
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parents[1]
SRC = ROOT / "src"
ICON = ROOT / "packaging" / "windows" / "assets" / "app.ico"
VERSION_FILE = os.environ.get("PCBROUTER_VERSION_FILE") or None

APP_NAME = "AI PCB Router"

# The AI SDKs and keyring are optional extras imported lazily inside functions. List
# them explicitly so the bundle does not depend on bytecode scanning, and only when
# they are installed in the build environment (the app works without them).
OPTIONAL = ("openai", "anthropic", "keyring", "tiktoken")
# The routing worker process imports job code lazily (by name, after spawn): bundle
# every pcbrouter module so the frozen worker can unpickle any job.
hiddenimports = collect_submodules("pcbrouter")
datas = [(str(SRC / "pcbrouter" / "resources"), "pcbrouter/resources")]
for name in OPTIONAL:
    if importlib.util.find_spec(name) is None:
        continue
    hiddenimports.append(name)
    datas += copy_metadata(name)  # importlib.metadata.version() and entry points
if importlib.util.find_spec("win32ctypes") is not None:
    # keyring's Windows backend -> pywin32-ctypes, which selects its ctypes/cffi
    # backend dynamically at import time.
    hiddenimports += collect_submodules("win32ctypes")

binaries = []
# Intel GPU support (dpnp/dpctl + the SYCL/oneMKL runtime) is bundled when it is
# installed in the build environment, so the installed app can route on Iris Xe /
# Arc without a separate Python. pip puts the runtime DLLs in <env>\Library\bin;
# they are copied to the same relative place and registered at run time by
# pcbrouter.compute.gpu_runtime.prepare_gpu_runtime().
GPU_PACKAGES = ("dpnp", "dpctl")
if all(importlib.util.find_spec(name) is not None for name in GPU_PACKAGES):
    import sys as _sys

    from PyInstaller.utils.hooks import collect_all

    for name in GPU_PACKAGES:
        d, b, h = collect_all(name)
        datas += d
        binaries += b
        hiddenimports += h
    runtime = Path(_sys.prefix) / "Library" / "bin"
    if runtime.is_dir():
        for dll in runtime.glob("*.dll"):
            binaries.append((str(dll), "Library/bin"))
        for extra in runtime.iterdir():  # e.g. sycl/ur adapter subfolders
            if extra.is_dir():
                for f in extra.rglob("*"):
                    if f.is_file():
                        rel = f.parent.relative_to(runtime)
                        binaries.append((str(f), str(Path("Library/bin") / rel)))
    print(f"[spec] bundling Intel GPU support: dpnp + dpctl + {runtime}")
else:
    print("[spec] dpnp/dpctl not installed: building without Intel GPU support")

a = Analysis(
    [str(SRC / "pcbrouter" / "__main__.py")],
    pathex=[str(SRC)],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=[
        # Development tools that may be installed in the build venv.
        "pytest",
        "pytestqt",
        "mypy",
        "black",
        "ruff",
        "tkinter",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)


def _exe(name, console):
    return EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=name,
        console=console,
        icon=str(ICON),
        version=VERSION_FILE,
        upx=False,  # UPX-packed binaries trigger antivirus false positives
    )


gui = _exe(APP_NAME, console=False)
cli = _exe("pcbrouter", console=True)

coll = COLLECT(gui, cli, a.binaries, a.datas, name=APP_NAME, upx=False)
