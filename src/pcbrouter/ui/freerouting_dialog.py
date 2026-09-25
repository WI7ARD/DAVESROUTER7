"""Tools ▸ Set Up Freerouting…: find KiCad's Python and Freerouting, get what is
missing, and test both on the open board.

Detection, the download and the test run on background threads; Java installs in
a ``QProcess`` after the user confirms. Every button answers in the dialog.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QProcess, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from pcbrouter.routing import freerouting_setup as frs
from pcbrouter.routing.freerouting import RELEASES_URL, _tool_for
from pcbrouter.ui.workers import JobRunner

if TYPE_CHECKING:
    from pcbrouter.ui.main_window import MainWindow

LICENSE_NOTE = (
    "Freerouting is a separate open-source program (GPL-3.0, freerouting.org). This app "
    "runs it; it does not include or change it."
)


def _why(message: str) -> str:
    """JobRunner messages read "<job> failed: <reason>"; keep the reason."""
    return message.split(" failed: ", 1)[-1]


class _Signals(QObject):
    line = Signal(str)
    progress = Signal(str, object)


class FreeroutingDialog(QDialog):
    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.w = window
        self.setWindowTitle("Set Up Freerouting")
        self.resize(680, 520)
        self.status = QLabel("Checking this computer…")
        self.status.setWordWrap(True)
        note = QLabel(LICENSE_NOTE)
        note.setWordWrap(True)
        self.passes = QSpinBox()
        self.passes.setRange(1, 10_000)
        self.passes.setValue(self.w.settings.routing.freerouting_passes)
        self.passes.setToolTip("Maximum Freerouting passes (more = better result, slower)")
        self.passes.valueChanged.connect(self._save_passes)
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.check_button = QPushButton("Check again")
        self.page_button = QPushButton("Open download page")
        self.download_button = QPushButton("Download .jar")
        self.choose_button = QPushButton("Choose file…")
        self.java_button = QPushButton("Install Java")
        self.kicad_button = QPushButton("Get KiCad")
        self.test_button = QPushButton("Test on open board")
        grid = QGridLayout()
        buttons = (
            self.check_button, self.page_button, self.download_button, self.choose_button,
            self.java_button, self.kicad_button, self.test_button,
        )  # fmt: skip
        for i, b in enumerate(buttons):
            grid.addWidget(b, i // 4, i % 4)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        guide = close.addButton("Guide", QDialogButtonBox.ButtonRole.HelpRole)
        guide.clicked.connect(lambda: self.w.open_guides("freerouting"))
        passes_row = QGridLayout()
        passes_row.addWidget(QLabel("Max passes:"), 0, 0)
        passes_row.addWidget(self.passes, 0, 1)
        passes_row.setColumnStretch(2, 1)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addLayout(grid)
        layout.addLayout(passes_row)
        layout.addWidget(note)
        layout.addWidget(self.bar)
        layout.addWidget(self.output, 1)
        layout.addWidget(close)
        self.state: frs.FreeroutingSetup | None = None
        self.process: QProcess | None = None
        self._jobs = JobRunner(self)
        self._cancel = threading.Event()
        self._signals = _Signals()
        self._signals.line.connect(self.say)
        self._signals.progress.connect(self._on_progress)
        self.check_button.clicked.connect(self.check)
        self.page_button.clicked.connect(self.open_page)
        self.download_button.clicked.connect(self.download)
        self.choose_button.clicked.connect(self.choose)
        self.java_button.clicked.connect(self.install_java)
        self.kicad_button.clicked.connect(
            lambda: self._open_url("https://www.kicad.org/download/", "KiCad download page")
        )
        self.test_button.clicked.connect(self.test)
        self.check()

    def say(self, text: str) -> None:
        self.output.appendPlainText(text)

    def _save_passes(self, value: int) -> None:
        self.w.settings.routing.freerouting_passes = int(value)
        self.w.save_settings()

    def _open_url(self, url: str, what: str) -> None:
        if QDesktopServices.openUrl(QUrl(url)):
            self.say(f"Opened the {what} in your browser: {url}")
        else:
            self.say(f"Could not open a browser. Go to: {url}")

    # -------------------------------------------------------------- buttons
    def check(self) -> None:
        if self._jobs.is_running("check"):
            return
        self.status.setText("Checking this computer (KiCad's Python can take a few seconds)…")
        configured = self.w.settings.routing.freerouting_path

        def done(result: Any, _s: float) -> None:
            assert isinstance(result, frs.FreeroutingSetup)
            self.state = result
            self.status.setText(result.text())

        self._jobs.start("check", lambda: frs.check_setup(configured), done,
                         lambda m, _d: self.say(f"Check failed: {_why(m)}"))  # fmt: skip

    def open_page(self) -> None:
        self._open_url(RELEASES_URL, "Freerouting download page")
        self.say(
            "Download the Windows installer (freerouting-…-windows-x64.msi), run it, then "
            "click 'Check again'. The installer includes Java."
        )

    def download(self) -> None:
        if self._jobs.is_running("download"):
            self.say("A download is already running…")
            return
        self.say("Downloading the latest Freerouting .jar from GitHub…")
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)
        self._cancel.clear()
        sig = self._signals

        def work() -> Path:
            return frs.download_jar(lambda s, f: sig.progress.emit(s, f), self._cancel)

        def done(path: Any, _s: float) -> None:
            self.bar.setVisible(False)
            self.say(f"Saved {path}.")
            if self.state is not None and self.state.java is None:
                self.say(f"The .jar needs Java {frs.MIN_JAVA}+: click 'Install Java'.")
            self.check()

        def failed(m: str, _d: str) -> None:
            self.bar.setVisible(False)
            self.say(f"Download failed: {_why(m)}\nUse 'Open download page' instead.")

        self._jobs.start("download", work, done, failed)

    def _on_progress(self, status: str, frac: object) -> None:
        if isinstance(frac, float):
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(frac * 1000))
        else:
            self.bar.setRange(0, 0)
        self.bar.setFormat(status)

    def choose(self) -> None:
        path, _f = QFileDialog.getOpenFileName(
            self, "Choose Freerouting", "", "Freerouting (freerouting*.exe freerouting*.jar)"
        )
        if path:
            self.use_path(Path(path))

    def use_path(self, path: Path) -> bool:
        tool = _tool_for(path)
        if tool is None or tool.kind == "script":
            self.say(f"Not a Freerouting program: {path}")
            return False
        self.w.settings.routing.freerouting_path = str(path)
        self.w.save_settings()
        self.say(f"Using {path}.")
        if tool.kind == "jar" and tool.java is None:
            self.say(f"This .jar needs Java {frs.MIN_JAVA}+: click 'Install Java'.")
        self.check()
        return True

    def install_java(self) -> None:
        if self.process is not None:
            self.say("Java is already being installed…")
            return
        cmd = frs.java_install_command()
        if cmd is None:
            self.say(
                "winget is not available. Install Java 21 from adoptium.net (Temurin JRE), "
                "or use the Freerouting Windows installer, which includes Java."
            )
            self._open_url("https://adoptium.net/temurin/releases/?version=21", "Java page")
            return
        answer = QMessageBox.question(
            self,
            "Install Java",
            f"Install Java 21 (Eclipse Temurin JRE) with Windows Package Manager?\n\n"
            f"{' '.join(cmd)}",
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.say("Java install canceled.")
            return
        self.say("$ " + " ".join(cmd))
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.readyReadStandardOutput.connect(
            lambda: self.say(
                bytes(proc.readAllStandardOutput().data()).decode("utf-8", "replace").rstrip()
            )
        )
        proc.finished.connect(self._java_done)
        proc.errorOccurred.connect(self._java_error)
        self.process = proc
        self.java_button.setEnabled(False)
        proc.start(cmd[0], cmd[1:])

    def _java_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self.say("Could not start winget. Install Java 21 from adoptium.net instead.")
            self.process = None
            self.java_button.setEnabled(True)

    def _java_done(self, code: int, _status: object) -> None:
        self.process = None
        self.java_button.setEnabled(True)
        if code != 0:
            self.say(f"winget failed (exit code {code}); see the messages above.")
        else:
            self.say("Java installed. If it is not found yet, restart the app.")
        self.check()

    def test(self) -> None:
        session = self.w.bus.context.project.session
        if session is None:
            self.say("Open a board first (File ▸ Open); the test routes a copy of it.")
            return
        if self.state is None:
            self.say("Still checking this computer; try again in a moment.")
            return
        if self.state.kicad is None or self.state.tool is None:
            self.say("Not ready yet:\n" + self.state.text())
            return
        if self._jobs.is_running("test"):
            self.say("A test is already running…")
            return
        board, tool, kicad = Path(session.source_path), self.state.tool, self.state.kicad
        self._cancel.clear()
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)
        self.bar.setFormat("testing…")
        sig = self._signals

        def work() -> str:
            return frs.selftest(board, tool, kicad, sig.line.emit, self._cancel)

        def done(msg: Any, secs: float) -> None:
            self.bar.setVisible(False)
            self.say(f"{msg}\nTest took {secs:.0f} s. Next: Router ▸ Route Board with "
                     "Freerouting…")  # fmt: skip

        def failed(m: str, _d: str) -> None:
            self.bar.setVisible(False)
            self.say(f"Test failed: {_why(m)}")

        self._jobs.start("test", work, done, failed)

    def reject(self) -> None:
        self._cancel.set()
        super().reject()
