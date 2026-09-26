"""In-app log viewer fed by a thread-safe logging handler."""

from __future__ import annotations

import collections
import contextlib
import logging
import threading
from collections.abc import Callable

from PySide6.QtCore import QCoreApplication, QObject, QThread, QTimer, Signal
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


MAX_LINES_PER_FLUSH = 400
MAX_BUFFERED = 5000
FLUSH_MS = 100


class QtLogHandler(logging.Handler):
    """Buffers formatted records (thread-safe). Records from the GUI thread are
    shown at once; records from other threads are appended by the panel in
    batches every ``FLUSH_MS`` (at most ``MAX_LINES_PER_FLUSH`` per batch), so a
    burst of log lines can never flood the Qt event queue."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.bridge = _Bridge()
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
        )
        self._buf: collections.deque[tuple[int, str]] = collections.deque()
        self._lock = threading.Lock()
        self.dropped = 0
        self.on_gui_thread: Callable[[], None] | None = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:
            return
        with self._lock:
            if len(self._buf) >= MAX_BUFFERED:
                self._buf.popleft()
                self.dropped += 1
            self._buf.append((record.levelno, text))
        app = QCoreApplication.instance()
        if (
            self.on_gui_thread is not None
            and app is not None
            and QThread.currentThread() is app.thread()
        ):
            with contextlib.suppress(RuntimeError):
                self.on_gui_thread()

    def take(self, limit: int = MAX_LINES_PER_FLUSH) -> tuple[list[tuple[int, str]], int]:
        with self._lock:
            n = min(limit, len(self._buf))
            out = [self._buf.popleft() for _ in range(n)]
            dropped, self.dropped = self.dropped, 0
            return out, dropped


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
        self.handler.on_gui_thread = self.flush
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(FLUSH_MS)
        self._flush_timer.timeout.connect(self.flush)
        self._flush_timer.start()

    def set_log_file(self, path: str | None) -> None:
        self.file_label.setText(f"Log file: {path}" if path else "File logging unavailable")

    def append(self, levelno: int, message: str) -> None:
        if levelno >= _LEVELS[self.level.currentText()]:
            self.text.appendPlainText(message)

    def flush(self) -> None:
        """Append buffered lines in one batch (GUI thread only)."""
        lines, dropped = self.handler.take()
        if not lines and not dropped:
            return
        minimum = _LEVELS[self.level.currentText()]
        shown = [text for level, text in lines if level >= minimum]
        if dropped:
            shown.append(f"… {dropped} log line(s) skipped (too many at once; see the log file)")
        if shown:
            self.text.appendPlainText("\n".join(shown))
