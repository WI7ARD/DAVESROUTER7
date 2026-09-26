"""Tools ▸ Set Up GPU…: detect, install the right library, switch to GPU, test.

Every button is always clickable and always answers in the dialog: it either does
the job or says exactly why it cannot (no silently disabled buttons). Detection
runs again in the background when the dialog opens; if it finds nothing, the
user can still pick "Intel" or "NVIDIA" by hand. pip runs in a ``QProcess``
(asynchronous, output streamed) using the console Python (``python.exe``, not
``pythonw.exe``, which has no output streams).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QProcess
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from pcbrouter.compute.gpu_setup import (
    MODULES,
    PACKAGES,
    can_install_packages,
    installed,
    pip_command,
    plan_setup,
)
from pcbrouter.ui.workers import JobRunner

if TYPE_CHECKING:
    from pcbrouter.ui.main_window import MainWindow

AUTO = "Detected GPU"


class GpuSetupDialog(QDialog):
    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.w = window
        self.setWindowTitle("Set Up GPU")
        self.resize(600, 460)
        self.summary = QLabel("Detecting your GPU…")
        self.summary.setWordWrap(True)
        self.vendor = QComboBox()
        self.vendor.addItems([AUTO, "Intel", "NVIDIA"])
        self.vendor.setToolTip("If detection missed your GPU, choose its maker here")
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.install_button = QPushButton("Install GPU support")
        self.use_button = QPushButton("Use GPU for routing")
        self.test_button = QPushButton("Test GPU on this board")
        vendor_row = QHBoxLayout()
        vendor_row.addWidget(QLabel("GPU maker:"))
        vendor_row.addWidget(self.vendor, 1)
        row = QHBoxLayout()
        for b in (self.install_button, self.use_button, self.test_button):
            row.addWidget(b)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        guide = close.addButton("Guide", QDialogButtonBox.ButtonRole.HelpRole)
        guide.clicked.connect(lambda: self.w.open_guides("gpu"))
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addLayout(vendor_row)
        layout.addLayout(row)
        layout.addWidget(self.output, 1)
        layout.addWidget(close)
        self.process: QProcess | None = None
        self.detection: Any = window.compute.gpu.detection
        self._jobs = JobRunner(self)
        self.install_button.clicked.connect(self.install)
        self.use_button.clicked.connect(self.use_gpu)
        self.test_button.clicked.connect(self.test)
        self.vendor.currentTextChanged.connect(lambda _t: self.refresh())
        window.route_jobs.finished.connect(self._job_finished)
        self.refresh()
        self.detect()

    # -------------------------------------------------------------- state
    def say(self, text: str) -> None:
        self.output.appendPlainText(text)

    def detect(self) -> None:
        from pcbrouter.compute.detection import detect_gpu

        def done(result: object, _secs: float) -> None:
            self.detection = result
            self.w.compute.update_gpu_detection(result)  # type: ignore[arg-type]
            self.refresh()

        self._jobs.start(
            "detect", detect_gpu, done, lambda m, _d: self.say(f"GPU detection failed: {m}")
        )

    def package(self) -> str | None:
        choice = self.vendor.currentText()
        if choice != AUTO:
            return PACKAGES[choice]
        return plan_setup(self.detection).package

    def refresh(self) -> None:
        plan = plan_setup(self.detection)
        text = plan.text()
        if plan.package is None and self.vendor.currentText() == AUTO:
            text += "\n• If you do have an Intel or NVIDIA GPU, choose it under 'GPU maker'."
        pkg = self.package()
        if pkg is not None and self.vendor.currentText() != AUTO:
            state = "installed" if installed(pkg) else "not installed"
            text += f"\n• Selected by hand: {self.vendor.currentText()} → '{pkg}' ({state})."
        self.summary.setText(text)
        self.install_button.setText(
            "Reinstall GPU support" if pkg and installed(pkg) else "Install GPU support"
        )

    # -------------------------------------------------------------- buttons
    def install(self) -> None:
        if self.process is not None:
            self.say("An installation is already running…")
            return
        pkg = self.package()
        if pkg is None:
            self.say("No GPU found. If you have one, choose Intel or NVIDIA under 'GPU maker'.")
            return
        ok, why = can_install_packages()
        if not ok:
            self.say(why)
            return
        cmd = pip_command(pkg)
        self.say("$ " + " ".join(cmd) + "\n(downloading can take a few minutes…)")
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.readyReadStandardOutput.connect(
            lambda: self.say(
                bytes(proc.readAllStandardOutput().data()).decode("utf-8", "replace").rstrip()
            )
        )
        proc.finished.connect(self._installed)
        proc.errorOccurred.connect(self._install_error)
        self.process = proc
        self.install_button.setEnabled(False)
        proc.start(cmd[0], cmd[1:])

    def _install_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self.say(
                "Could not start pip. Run setup_gpu.bat instead, or in a command prompt: "
                f"python -m pip install {self.package()}"
            )
            self.process = None
            self.install_button.setEnabled(True)

    def _installed(self, code: int, _status: object) -> None:
        from pcbrouter.compute.probe import reset_probe

        self.process = None
        self.install_button.setEnabled(True)
        if code != 0:
            self.say(f"pip failed (exit code {code}); see the messages above.")
            return
        pkg = self.package() or ""
        if not installed(pkg):
            self.say(
                f"pip finished but '{MODULES.get(pkg, pkg)}' still cannot be found; "
                "restart the app and open Set Up GPU again."
            )
        else:
            self.say("Installed. Next: 'Use GPU for routing', then 'Test GPU on this board'.")
        reset_probe()
        self.w.gpu_probe = None
        self.w.start_gpu_probe()
        if not self.w.route_jobs.busy:
            self.w.route_jobs.restart_worker()  # the worker imports GPU libraries
        self.detect()

    def use_gpu(self) -> None:
        from pcbrouter.settings.settings import ComputeBackendChoice

        self.w.settings.default_compute_backend = ComputeBackendChoice.GPU
        self.w.save_settings()
        self.w._update_backend_label()
        self.w.engine_ui.lbl_routing.setText(self.w.engine_ui.routing_status())
        pkg = self.package()
        note = (
            ""
            if pkg and installed(pkg)
            else (
                " The GPU library is not installed yet, so routing will fall back to the CPU "
                "(the routing card shows 'CPU fallback') until you click 'Install GPU support'."
            )
        )
        self.say("Routing backend set to GPU (Settings ▸ Compute)." + note)

    def test(self) -> None:
        if self.w.bus.context.project.session is None:
            self.say("Open a board first (File ▸ Open), then click 'Test GPU on this board'.")
            return
        if self.w.route_jobs.busy:
            self.say("A routing job is running; wait for it or cancel it, then test again.")
            return
        if self.w.routing_ui.test_gpu():
            self.say("GPU test running (CPU A* vs GPU on unrouted nets)…")
        else:
            self.say(
                "Nothing to test: every net on this board is already routed. Open a "
                "board with unrouted nets (or Router ▸ Reset Working Board)."
            )

    def _job_finished(self, done: Any) -> None:
        if not self.isVisible():
            return
        check: Any = self.w.routing_ui.last_gpu_check
        value: Any = getattr(done, "value", None)
        if check is not None and value is check:
            self.say(str(check.text()))
            return
        error = str(getattr(done, "error", "") or "")
        if error:
            self.say(f"Job {done.status.lower()}: {error}")

    def reject(self) -> None:
        if self.process is not None:
            self.process.kill()
            self.process = None
        with contextlib.suppress(RuntimeError, TypeError):
            self.w.route_jobs.finished.disconnect(self._job_finished)
        super().reject()
