"""Application bootstrap: logging, settings, core services, then GUI or CLI.

Core services (project manager, history, compute, command bus) are created by
:func:`build_services`, which has no Qt dependency; the GUI and the headless CLI
both run on the same services.
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import logging
import multiprocessing
import sys
import threading
import time
import traceback
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any

from pcbrouter import APP_NAME, APP_SLUG, __version__
from pcbrouter.ai.service import AIService
from pcbrouter.app_logging import log_startup_banner, setup_logging
from pcbrouter.commands import CloseBoardCommand, CommandBus, CommandContext
from pcbrouter.compute.backend import BackendKind
from pcbrouter.compute.detection import GpuDetectionResult, GpuStatus
from pcbrouter.compute.manager import ComputeManager
from pcbrouter.history.history import HistoryManager
from pcbrouter.project.manager import ProjectManager
from pcbrouter.settings.settings import AppSettings, ComputeBackendChoice, SettingsStore
from pcbrouter.utils.paths import log_dir

if TYPE_CHECKING:  # Qt stays a runtime import of the GUI path only
    from PySide6.QtWidgets import QApplication

log = logging.getLogger("pcbrouter.app")


@dataclass
class Services:
    bus: CommandBus
    compute: ComputeManager
    project: ProjectManager
    history: HistoryManager
    ai: AIService


def build_services(
    settings: AppSettings, *, detect_gpu_now: bool, ai_service: AIService | None = None
) -> Services:
    """Create the Qt-free core. With ``detect_gpu_now=False`` GPU detection is left
    pending so the GUI can run it in the background."""
    compute = ComputeManager(
        gpu_detection=None if detect_gpu_now else GpuDetectionResult(GpuStatus.PENDING)
    )
    preferred = (
        BackendKind.GPU
        if settings.default_compute_backend is ComputeBackendChoice.GPU
        else BackendKind.CPU
    )
    compute.select(preferred if detect_gpu_now else BackendKind.CPU)
    if not detect_gpu_now and preferred is BackendKind.GPU:
        compute.fallback_reason = "GPU routing runs in the routing worker process"
    # The GUI never initialises a GPU in its own process: GPU routing (and the
    # device probe it needs) runs in the routing worker process, chosen per job
    # from ``settings.default_compute_backend``.
    project = ProjectManager()
    history = HistoryManager()
    # No network, thread or SDK import happens here: the AI runner starts lazily on
    # the first explicit AI action, and SDKs are imported only inside adapters.
    ai = ai_service if ai_service is not None else AIService()
    bus = CommandBus(
        CommandContext(project=project, history=history, compute=compute, ai=ai), read_only=True
    )
    return Services(bus=bus, compute=compute, project=project, history=history, ai=ai)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=APP_SLUG,
        description=f"{APP_NAME} {__version__} — read-only KiCad board inspector with an "
        "AI planning assistant (no autorouting; never modifies board files).",
    )
    parser.add_argument("board", nargs="?", type=Path, help="optional .kicad_pcb to open")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="print a JSON summary of BOARD without starting the GUI",
    )
    parser.add_argument(
        "--check-command",
        metavar="JSON",
        help="validate a structured PCB command (as an AI would emit) against "
        "BOARD without executing it; implies --inspect mode",
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    parser.add_argument("--no-log-file", action="store_true", help="log to the console only")
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="print a JSON report of the runtime environment (versions, paths, optional "
        "packages, credential store) and exit",
    )
    parser.add_argument(
        "--setup-ollama",
        nargs="?",
        const="qwen2.5:7b",
        metavar="MODEL",
        help="set up free local AI with Ollama: download MODEL (default qwen2.5:7b), "
        "make it the AI provider and test it",
    )
    parser.add_argument(
        "--gpu-check",
        action="store_true",
        help="print which app copy runs, whether the GPU library (dpnp/CuPy) loads, and "
        "the GPU devices found (JSON); exit 0 when the library loads",
    )
    parser.add_argument(
        "--setup-gpu",
        action="store_true",
        help="detect the GPU, install the matching GPU library (pip) and test it",
    )
    parser.add_argument(
        "--worker-selftest",
        action="store_true",
        help="start the routing worker process, run a synthetic job (and route BOARD "
        "if given), print a JSON report and exit",
    )
    parser.add_argument(
        "--forget-api-keys",
        action="store_true",
        help="delete the stored API key of every configured AI provider profile from the "
        "OS credential store and exit (used by the Windows uninstaller)",
    )
    return parser.parse_args(argv)


def gpu_check_cli() -> dict[str, Any]:
    """Which copy of the app is this, and can it load the GPU library? (No GUI.)"""
    from pcbrouter.compute.probe import gpu_library_report

    return gpu_library_report()


def setup_gpu_cli() -> int:
    """``--setup-gpu``: detect → pip install the right library → deep probe in a
    fresh process (where the new library can be imported)."""
    import subprocess

    from pcbrouter.compute.detection import detect_gpu
    from pcbrouter.compute.gpu_setup import pip_command, plan_setup

    plan = plan_setup(detect_gpu())
    print(plan.text())
    if plan.package is None:
        return 0
    if not plan.installed:
        if not plan.can_install:
            return 1
        cmd = pip_command(plan.package)
        print("$ " + " ".join(cmd))
        if subprocess.run(cmd, check=False, timeout=1800).returncode != 0:
            print("pip install failed; see the output above.")
            return 1
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pcbrouter.compute.probe import probe_gpu; " "print(probe_gpu().summary())",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    print(probe.stdout.strip() or probe.stderr.strip()[-500:])
    ok = "GPU available" in probe.stdout
    print(
        "GPU ready: choose GPU in Settings ▸ Compute (or Tools ▸ Set Up GPU…)."
        if ok
        else "GPU not usable yet: update the graphics driver, then run --setup-gpu again."
    )
    return 0 if ok else 1


def setup_ollama_cli(model: str) -> int:
    """``--setup-ollama [MODEL]``: check → pull → save profile → test."""
    from pcbrouter.ai import ollama

    status = ollama.ollama_status()
    print(status.text())
    if not status.running:
        return 1
    if model not in status.models:
        print(f"Downloading {model}…")
        last = [""]

        def show(msg: str, frac: float | None) -> None:
            line = f"  {msg}" + (f" {frac * 100:5.1f}%" if frac is not None else "")
            if line != last[0]:
                print(line, flush=True)
                last[0] = line

        try:
            ollama.pull_model(model, progress=show, timeout=120)
        except ollama.OllamaError as exc:
            print(f"Download failed: {exc}")
            return 1
    store = SettingsStore()
    settings = store.load()
    ollama.apply_profile(settings.ai, ollama.ollama_profile(model))
    store.save(settings)
    print(f"AI provider set to '{ollama.PROFILE_NAME}' with {model}.")
    try:
        reply = ollama.chat_check(model)
    except ollama.OllamaError as exc:
        print(f"Test failed: {exc}")
        return 1
    print(f"Test reply: {reply[:120]}\nLocal AI works: open the AI panel in the app.")
    return 0


_CRASH_LOG: Any = None


def enable_crash_log(log_file: Path | None) -> None:
    """Native crashes (e.g. inside a GPU driver or Qt) kill Python without an
    exception; faulthandler still writes every thread's stack to crash.log next to
    the log file, so the cause can be found afterwards."""
    global _CRASH_LOG
    if log_file is None:
        return
    try:
        _CRASH_LOG = open(log_file.with_name("crash.log"), "a", encoding="utf-8")  # noqa: SIM115
        _CRASH_LOG.write(f"--- {APP_NAME} {__version__} started {time.ctime()} ---\n")
        _CRASH_LOG.flush()
        faulthandler.enable(file=_CRASH_LOG, all_threads=True)
    except OSError as exc:
        log.warning("app.crash_log_unavailable error=%s", exc)


def main(argv: Sequence[str] | None = None) -> int:
    # The routing worker is a spawned copy of this program: in a frozen build that
    # invocation must become the worker, never a second application window.
    multiprocessing.freeze_support()
    args = parse_args(argv)
    # print() is used for output because the windowed Windows executable has no console:
    # there sys.stdout/sys.stderr are None, and print() then silently does nothing.
    if args.version:
        print(f"{APP_NAME} {__version__}")
        return 0
    if args.setup_ollama:
        return setup_ollama_cli(args.setup_ollama)
    if args.gpu_check:
        report = gpu_check_cli()
        print(json.dumps(report, indent=2))
        return 0 if report["library_loads"] else 1
    if args.setup_gpu:
        return setup_gpu_cli()
    if args.worker_selftest:
        from pcbrouter.jobs.selftest import run_worker_selftest

        report = run_worker_selftest(args.board)
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    if args.diagnostics or args.forget_api_keys:
        # Utility commands: no log file, no board, no GUI.
        from pcbrouter.app import maintenance

        if args.diagnostics:
            print(json.dumps(maintenance.diagnostics(), indent=2))
            return 0
        return maintenance.forget_api_keys()
    headless = args.inspect or args.check_command is not None
    log_file = setup_logging(
        None if args.no_log_file else log_dir(),
        getattr(logging, args.log_level),
        console=sys.stderr is not None,
    )
    log_startup_banner()
    log.info("app.log_file path=%s", log_file)
    enable_crash_log(log_file)
    if headless:
        from pcbrouter.app.cli import run_cli

        return run_cli(args)
    return run_gui(args, log_file)


def _install_exception_hooks() -> None:
    def hook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical(
            "app.unhandled_exception\n%s", "".join(traceback.format_exception(exc_type, exc, tb))
        )
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                QMessageBox.critical(
                    None,
                    "Unexpected error",
                    f"An unexpected error occurred:\n\n{exc}\n\n"
                    "Details were written to the log. The board file was not modified.",
                )
        except Exception:  # never let the hook itself crash
            pass

    sys.excepthook = hook

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        log.critical(
            "app.thread_exception thread=%s error=%r",
            args.thread.name if args.thread else "?",
            args.exc_value,
        )

    threading.excepthook = thread_hook


def _set_window_icon(app: QApplication) -> None:
    """Window/taskbar icon (generated by packaging/windows/make_assets.py)."""
    from importlib.resources import as_file, files

    from PySide6.QtGui import QIcon, QPixmap

    resource = files("pcbrouter").joinpath("resources", "app_icon.png")
    with as_file(resource) as path:
        pixmap = QPixmap(str(path))  # loaded now, so a temporary file may be removed
    if pixmap.isNull():
        log.warning("app.icon_missing resource=%s", resource)
        return
    app.setWindowIcon(QIcon(pixmap))


def run_gui(args: argparse.Namespace, log_file: Path | None) -> int:
    try:
        from PySide6.QtCore import QObject, Signal
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        log.critical("app.qt_unavailable error=%s", exc)
        print(
            f"Could not start the GUI because Qt failed to load: {exc}\n"
            "Install the project dependencies (pip install -e .). On Linux, Qt also needs "
            "system libraries such as libEGL, libxkbcommon and libfontconfig.\n"
            "Headless use: pcbrouter BOARD --inspect",
            file=sys.stderr,
        )
        return 3

    from pcbrouter.compute.detection import detect_gpu
    from pcbrouter.ui.log_panel import LogPanel
    from pcbrouter.ui.main_window import MainWindow
    from pcbrouter.ui.theme import apply_theme

    _install_exception_hooks()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    assert isinstance(app, QApplication)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_SLUG)
    _set_window_icon(app)

    from PySide6 import __version__ as pyside_version
    from PySide6.QtCore import qVersion

    log.info("app.gui qt=%s pyside=%s", qVersion(), pyside_version)

    store = SettingsStore()
    settings = store.load()
    apply_theme(app, settings.theme)
    services = build_services(settings, detect_gpu_now=False)

    from pcbrouter.app_logging import add_handler, remove_handler

    log_panel = LogPanel()
    log_panel.set_log_file(str(log_file) if log_file else None)
    add_handler(log_panel.handler)

    window = MainWindow(
        bus=services.bus,
        compute=services.compute,
        settings=settings,
        settings_store=store,
        log_panel=log_panel,
    )

    class GpuDetector(QObject):
        finished = Signal(object)

        def run(self) -> None:
            self.finished.emit(detect_gpu())

    detector = GpuDetector()
    # Bound QObject method => queued connection back onto the GUI thread.
    detector.finished.connect(window.on_gpu_detected)
    threading.Thread(target=detector.run, name="gpu-detect", daemon=True).start()

    window.show()
    if store.last_load_problem:
        window.statusBar().showMessage(
            "Settings file was invalid and has been reset (a backup was kept).", 10000
        )
    if args.board is not None:
        window.open_board(args.board)

    code = app.exec()
    # The loop can end without the window being closed (app.quit(), OS logout), so save
    # settings and release the board explicitly. Both are idempotent.
    window.save_settings()
    if services.project.is_open:
        services.bus.dispatch(CloseBoardCommand())
    remove_handler(log_panel.handler)
    services.ai.shutdown()
    services.compute.shutdown()
    log.info("app.shutdown exit_code=%d", code)
    return int(code)
