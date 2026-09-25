"""Tools ▸ Set Up GPU…: detect, install the right library, switch to GPU, test.

pip runs in a ``QProcess`` (asynchronous, output streamed): the window stays
responsive during the download. After installing, the GUI's light probe is
re-run and the routing worker is restarted so it can import the new library.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QProcess
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from pcbrouter.compute.gpu_setup import GpuSetupPlan, pip_command, plan_setup

if TYPE_CHECKING:
    from pcbrouter.ui.main_window import MainWindow


class GpuSetupDialog(QDialog):
    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.w = window
        self.setWindowTitle("Set Up GPU")
        self.resize(560, 420)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("Installer output appears here.")
        self.install_button = QPushButton("Install GPU support")
        self.use_button = QPushButton("Use GPU for routing")
        self.test_button = QPushButton("Test GPU on this board")
        row = QHBoxLayout()
        for b in (self.install_button, self.use_button, self.test_button):
            row.addWidget(b)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addLayout(row)
        layout.addWidget(self.output, 1)
        layout.addWidget(close)
        self.process: QProcess | None = None
        self.install_button.clicked.connect(self.install)
        self.use_button.clicked.connect(self.use_gpu)
        self.test_button.clicked.connect(self.test)
        self.refresh()

    def plan(self) -> GpuSetupPlan:
        return plan_setup(self.w.compute.gpu.detection)

    def refresh(self) -> None:
        p = self.plan()
        self.summary.setText(p.text())
        busy = self.process is not None
        self.install_button.setEnabled(
            not busy and p.package is not None and not p.installed and p.can_install
        )
        self.use_button.setEnabled(not busy and p.ready)
        self.test_button.setEnabled(
            not busy and p.ready and self.w.bus.context.project.session is not None
        )

    def install(self) -> None:
        p = self.plan()
        if p.package is None or self.process is not None:
            return
        cmd = pip_command(p.package)
        self.output.appendPlainText("$ " + " ".join(cmd))
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.readyReadStandardOutput.connect(
            lambda: self.output.appendPlainText(
                bytes(proc.readAllStandardOutput().data()).decode("utf-8", "replace").rstrip()
            )
        )
        proc.finished.connect(self._installed)
        self.process = proc
        proc.start(cmd[0], cmd[1:])
        self.refresh()

    def _installed(self, code: int, _status: object) -> None:
        from pcbrouter.compute.probe import reset_probe

        self.process = None
        self.output.appendPlainText(
            "Installed." if code == 0 else f"pip failed (exit code {code}); see above."
        )
        reset_probe()
        self.w.gpu_probe = None
        self.w.start_gpu_probe()
        if not self.w.route_jobs.busy:
            self.w.route_jobs.restart_worker()  # the worker imports GPU libraries
        self.refresh()

    def use_gpu(self) -> None:
        from pcbrouter.settings.settings import ComputeBackendChoice

        self.w.settings.default_compute_backend = ComputeBackendChoice.GPU
        self.w.save_settings()
        self.w._update_backend_label()
        self.w.engine_ui.lbl_routing.setText(self.w.engine_ui.routing_status())
        self.output.appendPlainText("Routing backend set to GPU (Settings ▸ Compute).")

    def test(self) -> None:
        if self.w.routing_ui.test_gpu():
            self.output.appendPlainText("GPU test started — results appear when it finishes.")

    def reject(self) -> None:
        if self.process is not None:
            self.process.kill()
            self.process = None
        super().reject()
