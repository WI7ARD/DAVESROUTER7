"""Routing Jobs panel: live board-routing progress and per-net batch review.

Pause / Resume / Cancel control the running job; when it finishes, each net is
listed with its router result. Accept All, Accept Checked (granular) or Reject.
Nothing reaches the working board before Accept.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pcbrouter.routing.board_router import BoardRoutingResult
from pcbrouter.routing.result import RouteStatus

COLUMNS = ("Net", "Status", "Reason", "Length (mm)", "Vias", "Pass", "Message")


class ProgressBridge(QObject):
    """Emitted from the worker thread; Qt queues it onto the GUI thread."""

    progress = Signal(object)


class BoardJobPanel(QWidget):
    pauseRequested = Signal()
    resumeRequested = Signal()
    cancelRequested = Signal()
    acceptRequested = Signal(object)  # set[str] | None  (None = all)
    rejectRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.result: BoardRoutingResult | None = None
        self.status = QLabel("No routing job.")
        self.status.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.pause_button = QPushButton("Pause")
        self.cancel_button = QPushButton("Cancel")
        self.pause_button.clicked.connect(self._toggle_pause)
        self.cancel_button.clicked.connect(self.cancelRequested)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.accept_all = QPushButton("Accept All")
        self.accept_checked = QPushButton("Accept Checked Nets")
        self.reject = QPushButton("Reject")
        self.accept_all.clicked.connect(lambda: self.acceptRequested.emit(None))
        self.accept_checked.clicked.connect(lambda: self.acceptRequested.emit(self.checked_nets()))
        self.reject.clicked.connect(self.rejectRequested)
        self._paused = False
        top = QHBoxLayout()
        top.addWidget(self.status, 1)
        top.addWidget(self.pause_button)
        top.addWidget(self.cancel_button)
        buttons = QHBoxLayout()
        for b in (self.accept_all, self.accept_checked, self.reject):
            buttons.addWidget(b)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(top)
        layout.addWidget(self.bar)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        self.set_idle()

    # ------------------------------------------------------------ states
    def set_idle(self) -> None:
        self._running(False)
        for b in (self.accept_all, self.accept_checked, self.reject):
            b.setEnabled(False)

    def set_running(self, text: str) -> None:
        self.result = None
        self.table.setRowCount(0)
        self.status.setText(text)
        self.bar.setRange(0, 0)  # busy until the first progress report
        self._paused = False
        self.pause_button.setText("Pause")
        self._running(True)
        for b in (self.accept_all, self.accept_checked, self.reject):
            b.setEnabled(False)

    def _running(self, on: bool) -> None:
        self.pause_button.setEnabled(on)
        self.cancel_button.setEnabled(on)

    def on_progress(self, info: Any) -> None:
        if not isinstance(info, dict):
            return
        m = info.get("metrics", {})
        if info.get("state") == "done":
            return
        total = int(info.get("total", 0) or 0)
        index = int(info.get("index", 0) or 0)
        if total:
            self.bar.setRange(0, total)
            self.bar.setValue(index - 1)
        self.status.setText(
            f"Pass {info.get('pass_no', 1)} · net {info.get('net', '?')} ({index}/{total}) · "
            f"{m.get('expanded_nodes', 0):,} nodes · {m.get('ripups', 0)} rip-up(s)"
            + (" · PAUSED" if self._paused else "")
        )

    def set_result(self, result: BoardRoutingResult | None) -> None:
        self.result = result
        self._running(False)
        self.bar.setRange(0, 1)
        self.bar.setValue(1 if result else 0)
        self.table.setRowCount(0)
        if result is None:
            self.set_idle()
            return
        self.status.setText(result.summary())
        for net in result.plan.nets:
            o = result.outcomes[net]
            row = self.table.rowCount()
            self.table.insertRow(row)
            name = QTableWidgetItem(net)
            ok = o.status is RouteStatus.SUCCESS and bool(o.added_ids)
            name.setFlags(name.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            name.setCheckState(Qt.CheckState.Checked if ok else Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, name)
            # status in words (not only colour)
            vals = (
                o.status.value,
                o.reason.value if o.reason else "",
                f"{o.length_nm / 1e6:.2f}",
                str(o.vias),
                str(o.passes),
                o.message,
            )
            for col, text in enumerate(vals, start=1):
                self.table.setItem(row, col, QTableWidgetItem(text))
        has = bool(result.added_tracks or result.added_vias)
        self.accept_all.setEnabled(has)
        self.accept_checked.setEnabled(has)
        self.reject.setEnabled(True)

    def checked_nets(self) -> set[str]:
        out: set[str] = set()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                out.add(item.text())
        return out

    def set_checked(self, nets: set[str]) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None:
                item.setCheckState(
                    Qt.CheckState.Checked if item.text() in nets else Qt.CheckState.Unchecked
                )

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self.pause_button.setText("Resume" if self._paused else "Pause")
        (self.pauseRequested if self._paused else self.resumeRequested).emit()
