"""In-app log viewer fed by a thread-safe logging handler."""

from __future__ import annotations

import contextlib
import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

MAX_LINES = 5000
_LEVELS = {
    "Debug": logging.DEBUG,
    "Info": logging.INFO,
    "Warning": logging.WARNING,
    "Error": logging.ERROR,
}


class _Bridge(QObject):
    record = Signal(int, str)


class QtLogHandler(logging.Handler):
    """Forwards formatted records through a Qt signal (safe from worker threads:
    queued connections deliver them on the GUI thread)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.bridge = _Bridge()
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(RuntimeError):  # bridge already deleted during shutdown
            self.bridge.record.emit(record.levelno, self.format(record))


class LogPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(MAX_LINES)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.text.setFont(mono)
        self.level = QComboBox()
        self.level.addItems(list(_LEVELS))
        self.level.setCurrentText("Info")
        self.level.setToolTip("Minimum level shown here (the log file keeps everything)")
        clear = QPushButton("Clear")
        clear.clicked.connect(self.text.clear)
        self.file_label = QLabel("")
        self.file_label.setProperty("role", "muted")
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Level:"))
        bar.addWidget(self.level)
        bar.addWidget(clear)
        bar.addStretch(1)
        bar.addWidget(self.file_label)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.addLayout(bar)
        layout.addWidget(self.text, 1)
        self.handler = QtLogHandler()
        self.handler.bridge.record.connect(self.append)

    def set_log_file(self, path: str | None) -> None:
        self.file_label.setText(f"Log file: {path}" if path else "File logging unavailable")

    def append(self, levelno: int, message: str) -> None:
        if levelno >= _LEVELS[self.level.currentText()]:
            self.text.appendPlainText(message)
