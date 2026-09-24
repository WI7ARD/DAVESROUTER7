"""Application bootstrap: logging, settings, core services, then GUI or CLI.

Core services (project manager, history, compute, command bus) are created by
:func:`build_services`, which has no Qt dependency; the GUI and the headless CLI
both run on the same services.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import traceback
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from pcbrouter import APP_NAME, APP_SLUG, __version__
from pcbrouter.app_logging import log_startup_banner, setup_logging
from pcbrouter.commands import CloseBoardCommand, CommandBus, CommandContext
from pcbrouter.compute.backend import BackendKind
from pcbrouter.compute.detection import GpuDetectionResult, GpuStatus
from pcbrouter.compute.manager import ComputeManager
from pcbrouter.history.history import HistoryManager
from pcbrouter.project.manager import ProjectManager
from pcbrouter.settings.settings import AppSettings, ComputeBackendChoice, SettingsStore
from pcbrouter.utils.paths import log_dir

log = logging.getLogger("pcbrouter.app")


@dataclass
class Services:
    bus: CommandBus
    compute: ComputeManager
    project: ProjectManager
    history: HistoryManager


def build_services(settings: AppSettings, *, detect_gpu_now: bool) -> Services:
    """Create the Qt-free core. With ``detect_gpu_now=False`` GPU detection is left
    pending so the GUI can run it in the background."""
    compute = ComputeManager(
        gpu_detection=None if detect_gpu_now else GpuDetectionResult(GpuStatus.PENDING)
    )
    preferred = (
        BackendKind.GPU if settings.default_compute_backend is ComputeBackendChoice.GPU
        else BackendKind.CPU
    )  # fmt: skip
    compute.select(preferred)
    project = ProjectManager()
    history = HistoryManager()
    bus = CommandBus(
        CommandContext(project=project, history=history, compute=compute), read_only=True
    )
    return Services(bus=bus, compute=compute, project=project, history=history)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=APP_SLUG,
        description=f"{APP_NAME} {__version__} — read-only KiCad board inspector "
        "(Stage 1: no autorouting, no AI API calls).",
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
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.version:
        sys.stdout.write(f"{APP_NAME} {__version__}\n")
        return 0
    headless = args.inspect or args.check_command is not None
    log_file = setup_logging(
        None if args.no_log_file else log_dir(), getattr(logging, args.log_level), console=True
    )
    log_startup_banner()
    log.info("app.log_file path=%s", log_file)
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


def run_gui(args: argparse.Namespace, log_file: Path | None) -> int:
    try:
        from PySide6.QtCore import QObject, Signal
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        log.critical("app.qt_unavailable error=%s", exc)
        sys.stderr.write(
            f"Could not start the GUI because Qt failed to load: {exc}\n"
            "Install the project dependencies (pip install -e .). On Linux, Qt also needs "
            "system libraries such as libEGL, libxkbcommon and libfontconfig.\n"
            "Headless use: pcbrouter BOARD --inspect\n"
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
    services.compute.shutdown()
    log.info("app.shutdown exit_code=%d", code)
    return int(code)
