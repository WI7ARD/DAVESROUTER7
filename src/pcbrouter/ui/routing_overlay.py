"""The routing-state card shown over the board while a worker job runs.

Everything here is driven by the GUI event loop (the routing itself is in the
worker process), so the spinner and the elapsed-time clock keep moving for the
whole job. The spinner angle is computed from wall-clock time, not from a tick
count, so a starved event loop would be *visible* as a jump, never hidden.
Progress bars are real (net/candidate counts) or explicitly indeterminate.
"""

from __future__ import annotations

import time
from typing import Any

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pcbrouter.jobs.protocol import PHASE_TEXT, JobBase, JobPhase, RouteProgress

SPINNER_MS = 16  # ~60 fps
CLOCK_MS = 250


class Spinner(QWidget):
    def __init__(self, parent: QWidget | None = None, size: int = 36) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._t0 = time.monotonic()
        self.frames = 0  # tests: proves the animation really advanced
        self._timer = QTimer(self)
        self._timer.setInterval(SPINNER_MS)
        self._timer.timeout.connect(self._tick)
        self.setAccessibleName("Routing in progress")

    def start(self) -> None:
        self._t0 = time.monotonic()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    def _tick(self) -> None:
        self.frames += 1
        self.update()

    def angle(self) -> float:
        return ((time.monotonic() - self._t0) * 360.0) % 360.0  # one turn per second

    def paintEvent(self, _event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(4, 4, self.width() - 8, self.height() - 8)
        track = QPen(QColor(128, 128, 128, 70), 4)
        p.setPen(track)
        p.drawEllipse(r)
        pen = QPen(self.palette().highlight().color(), 4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(r, int(-self.angle() * 16), 100 * 16)
        p.end()


def _fmt_elapsed(s: float) -> str:
    s = int(s)
    return (
        f"{s // 3600:d}:{s // 60 % 60:02d}:{s % 60:02d}"
        if s >= 3600
        else f"{s // 60:02d}:{s % 60:02d}"
    )


class RoutingOverlay(QFrame):
    """A floating card over the canvas (the board stays visible and pannable)."""

    WIDTH = 380

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self.setObjectName("routingOverlay")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAutoFillBackground(True)
        self.setStyleSheet(
            "#routingOverlay { border: 1px solid palette(highlight); border-radius: 8px;"
            " background: palette(window); }"
        )
        self.spinner = Spinner(self)
        self.heading = QLabel("Routing board…")
        font = self.heading.font()
        font.setPointSizeF(font.pointSizeF() * 1.25)
        font.setBold(True)
        self.heading.setFont(font)
        self.phase = QLabel("")
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.backend = QLabel("")
        self.backend.setWordWrap(True)
        self.backend.setProperty("role", "muted")
        self.clock = QLabel("Elapsed: 00:00")
        self.alive = QLabel("")
        self.alive.setProperty("role", "muted")
        self.bar = QProgressBar()
        self.bar.setTextVisible(True)
        self.cancel_button = QPushButton("Cancel Routing")
        self.cancel_button.setToolTip("Stop the job; nothing is changed on the board")
        top = QHBoxLayout()
        top.addWidget(self.spinner)
        titles = QVBoxLayout()
        titles.addWidget(self.heading)
        titles.addWidget(self.phase)
        top.addLayout(titles, 1)
        clocks = QHBoxLayout()
        clocks.addWidget(self.clock)
        clocks.addStretch(1)
        clocks.addWidget(self.alive)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.addLayout(top)
        layout.addWidget(self.detail)
        layout.addWidget(self.backend)
        layout.addLayout(clocks)
        layout.addWidget(self.bar)
        layout.addWidget(self.cancel_button, 0, Qt.AlignmentFlag.AlignRight)
        self._t0 = time.monotonic()
        self._last_progress = time.monotonic()
        self._clock = QTimer(self)
        self._clock.setInterval(CLOCK_MS)
        self._clock.timeout.connect(self._tick_clock)
        self.determinate = False
        host.installEventFilter(self)
        self.hide()

    # -------------------------------------------------------------- states
    def show_starting(self, job: JobBase) -> None:
        self._t0 = self._last_progress = time.monotonic()
        self.heading.setText(job.title)
        self.phase.setText(PHASE_TEXT[JobPhase.STARTING])
        self.detail.setText("")
        self.backend.setText(f"Requested backend: {job.mode.upper()}")
        self.alive.setText("")
        self._indeterminate()
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("Cancel Routing")
        self._tick_clock()
        self.spinner.start()
        self._clock.start()
        self._place()
        self.show()
        self.raise_()

    def show_canceling(self) -> None:
        self.phase.setText("Stopping routing…")
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Stopping…")

    def finish(self) -> None:
        self.spinner.stop()
        self._clock.stop()
        self.hide()

    def update_progress(self, p: RouteProgress) -> None:
        self._last_progress = time.monotonic()
        if self.cancel_button.isEnabled():  # keep "Stopping routing…" while canceling
            try:
                self.phase.setText(PHASE_TEXT[JobPhase(p.phase)])
            except ValueError:
                self.phase.setText(p.phase)
        lines: list[str] = []
        if p.total_nets and p.current_net and p.current_net_name:
            lines.append(f"Routing net {p.current_net} of {p.total_nets}: {p.current_net_name}")
        elif p.current_net_name:
            lines.append(f"Net {p.current_net_name}")
        if p.current_pass:
            passes = f" of {p.total_passes}" if p.total_passes else ""
            lines.append(
                f"Pass {p.current_pass}{passes}" + (f" · rip-ups {p.ripups}" if p.ripups else "")
            )
        if p.candidates_total:
            lines.append(
                f"Candidate {min(p.candidates_completed or 0, p.candidates_total) + 1} "
                f"of {p.candidates_total}"
                + (
                    f" · connection {p.connection}/{p.connections_total}"
                    if p.connection and p.connections_total
                    else ""
                )
            )
        if p.grid:
            lines.append(f"Grid {p.grid[0]}×{p.grid[1]} × {p.grid[2]} layer(s)")
        if p.message:
            lines.append(p.message)
        self.detail.setText("\n".join(lines))
        if p.backend is not None:
            self.backend.setText(p.backend.text())
        frac = p.fraction()
        if frac is None or p.phase in (JobPhase.PREPARING_BOARD.value, JobPhase.PLANNING.value):
            self._indeterminate()
        else:
            self.determinate = True
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(frac * 1000))
            if p.total_nets:
                self.bar.setFormat(f"{p.completed_nets or 0} / {p.total_nets} nets")
            else:
                self.bar.setFormat("%p%")

    def set_heartbeat_age(self, seconds: float | None) -> None:
        if seconds is None:
            self.alive.setText("")
        elif seconds < 2:
            self.alive.setText("worker active")
        else:
            self.alive.setText(f"worker active · last update {seconds:.0f} s ago")

    # -------------------------------------------------------------- internals
    def _indeterminate(self) -> None:
        self.determinate = False
        self.bar.setRange(0, 0)  # Qt's busy animation: no invented percentage
        self.bar.setFormat("")

    def _tick_clock(self) -> None:
        self.clock.setText(f"Elapsed: {_fmt_elapsed(time.monotonic() - self._t0)}")

    def _place(self) -> None:
        host = self.parentWidget()
        if host is None:
            return
        self.adjustSize()
        w = min(self.WIDTH, max(260, host.width() - 32))
        self.setFixedWidth(w)
        self.adjustSize()
        self.move(max(8, host.width() - w - 16), 16)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize and self.isVisible():
            self._place()
        return False
