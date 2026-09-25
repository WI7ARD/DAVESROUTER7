"""AI ▸ Set Up Local AI (Ollama)…: check Ollama, download a model, use it.

All network calls (to localhost only) run on background threads; progress comes
back through Qt signals. Every button answers in the dialog.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from pcbrouter.ai import ollama
from pcbrouter.ui.workers import JobRunner

if TYPE_CHECKING:
    from pcbrouter.ui.main_window import MainWindow


class _PullSignals(QObject):
    progress = Signal(str, object)


class OllamaDialog(QDialog):
    def __init__(self, window: MainWindow, url: str = ollama.DEFAULT_URL) -> None:
        super().__init__(window)
        self.w = window
        self.url = url
        self.setWindowTitle("Set Up Local AI (Ollama)")
        self.resize(620, 460)
        self.status = QLabel("Checking for Ollama…")
        self.status.setWordWrap(True)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setToolTip("An installed model, or any model name from ollama.com/library")
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.check_button = QPushButton("Check again")
        self.pull_button = QPushButton("Download model")
        self.use_button = QPushButton("Use this model")
        self.test_button = QPushButton("Test")
        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("Model:"))
        model_row.addWidget(self.model, 1)
        row = QHBoxLayout()
        for b in (self.check_button, self.pull_button, self.use_button, self.test_button):
            row.addWidget(b)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addLayout(model_row)
        layout.addLayout(row)
        layout.addWidget(self.bar)
        layout.addWidget(self.output, 1)
        layout.addWidget(close)
        self.state: ollama.OllamaStatus | None = None
        self._jobs = JobRunner(self)
        self._cancel = threading.Event()
        self._signals = _PullSignals()
        self._signals.progress.connect(self._on_progress)
        self.check_button.clicked.connect(self.check)
        self.pull_button.clicked.connect(self.pull)
        self.use_button.clicked.connect(self.use)
        self.test_button.clicked.connect(self.test)
        self._fill_models([])
        self.check()

    def say(self, text: str) -> None:
        self.output.appendPlainText(text)

    def _fill_models(self, installed: list[str]) -> None:
        current = self.model.currentText()
        self.model.clear()
        for name in installed:
            self.model.addItem(f"{name}", name)
        for name, why in ollama.RECOMMENDED:
            if name not in installed:
                self.model.addItem(f"{name} — {why}", name)
        if current:
            idx = self.model.findData(current)
            self.model.setCurrentIndex(max(0, idx))

    def model_name(self) -> str:
        text = self.model.currentText()
        idx = self.model.findText(text)
        data = self.model.itemData(idx) if idx >= 0 else None
        return str(data) if data else text.split(" — ")[0].strip()

    # -------------------------------------------------------------- buttons
    def check(self) -> None:
        def done(result: object, _s: float) -> None:
            self.state = result  # type: ignore[assignment]
            assert isinstance(result, ollama.OllamaStatus)
            self.status.setText(result.text())
            self._fill_models(result.models)

        self._jobs.start("check", lambda: ollama.ollama_status(self.url), done,
                         lambda m, _d: self.say(f"Check failed: {m}"))  # fmt: skip

    def _need_running(self) -> bool:
        if self.state is None or not self.state.running:
            self.say(ollama.INSTALL_HINT)
            return False
        return True

    def pull(self) -> None:
        if not self._need_running():
            return
        if self._jobs.is_running("pull"):
            self.say("A download is already running…")
            return
        name = self.model_name()
        self.say(f"Downloading {name} (this can take a while)…")
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)
        self._cancel.clear()
        sig = self._signals

        def work() -> None:
            ollama.pull_model(name, self.url, lambda s, f: sig.progress.emit(s, f), self._cancel)

        def done(_r: object, _s: float) -> None:
            self.bar.setVisible(False)
            self.say(f"{name} is ready. Click 'Use this model'.")
            self.check()

        def failed(m: str, _d: str) -> None:
            self.bar.setVisible(False)
            self.say(f"Download failed: {m}")

        self._jobs.start("pull", work, done, failed)

    def _on_progress(self, status: str, frac: object) -> None:
        if isinstance(frac, float):
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(frac * 1000))
        else:
            self.bar.setRange(0, 0)
        self.bar.setFormat(status)

    def use(self) -> None:
        if not self._need_running():
            return
        name = self.model_name()
        if self.state is not None and name not in self.state.models:
            self.say(f"{name} is not downloaded yet: click 'Download model' first.")
            return
        ollama.apply_profile(self.w.settings.ai, ollama.ollama_profile(name, self.url))
        self.w.save_settings()
        self.w.ai_panel.settings = self.w.settings
        self.w.ai_panel.refresh_profiles()
        if self.w.ai_service.session is not None:
            self.w._start_ai_session()
        self.say(f"The AI assistant now uses {name} on this computer (no API key, nothing "
                 "is sent to the internet). Next: 'Test'.")  # fmt: skip

    def test(self) -> None:
        if not self._need_running():
            return
        name = self.model_name()
        self.say(f"Asking {name} for a short JSON reply (the first answer can take a minute "
                 "while the model loads)…")  # fmt: skip

        def done(reply: Any, secs: float) -> None:
            self.say(f"Reply in {secs:.1f} s: {str(reply)[:200]}\nLocal AI works.")

        self._jobs.start("test", lambda: ollama.chat_check(name, self.url), done,
                         lambda m, _d: self.say(f"Test failed: {m}"))  # fmt: skip

    def reject(self) -> None:
        self._cancel.set()
        super().reject()
