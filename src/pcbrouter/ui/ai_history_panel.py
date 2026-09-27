"""AI history: every request of this board session and every proposal decision."""

from __future__ import annotations

import time

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pcbrouter.ai.service import AIService
from pcbrouter.history.history import HistoryManager

COLUMNS = ["Time", "Provider", "Mode", "Prompt", "Result", "Rating"]


class AIHistoryPanel(QWidget):
    proposalActivated = Signal(str)

    def __init__(
        self, service: AIService, history: HistoryManager, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.history = history
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setRootIsDecorated(True)
        self.tree.itemDoubleClicked.connect(self._activate)
        self.decisions = QLabel("")
        self.decisions.setProperty("role", "muted")
        self.decisions.setWordWrap(True)
        self.rate_good = QPushButton("Helpful")
        self.rate_good.setToolTip("Mark the selected answer helpful (used to improve prompts).")
        self.rate_good.clicked.connect(lambda: self._rate(1))
        self.rate_bad = QPushButton("Not Helpful")
        self.rate_bad.setToolTip("Mark the selected answer not helpful.")
        self.rate_bad.clicked.connect(lambda: self._rate(-1))
        self.rate_clear = QPushButton("Clear Rating")
        self.rate_clear.clicked.connect(lambda: self._rate(None))
        rate_row = QHBoxLayout()
        rate_row.addWidget(QLabel("Rate selected:"))
        rate_row.addWidget(self.rate_good)
        rate_row.addWidget(self.rate_bad)
        rate_row.addWidget(self.rate_clear)
        rate_row.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.tree, 1)
        layout.addLayout(rate_row)
        layout.addWidget(self.decisions)
        self.refresh()

    def refresh(self) -> None:
        self.tree.clear()
        session = self.service.session
        if session is None:
            self.decisions.setText("No board open.")
            return
        for inter in session.interactions:
            when = time.strftime("%H:%M", time.localtime(inter.started_at))
            result = inter.kind.value.upper() if inter.kind else "PENDING"
            rating = {1: "★ helpful", -1: "✗ not helpful"}.get(inter.rating or 0, "—")
            item = QTreeWidgetItem(
                [
                    when,
                    f"{inter.provider} · {inter.model}",
                    inter.mode.label,
                    inter.prompt[:80],
                    result,
                    rating,
                ]
            )
            item.setData(0, 257, inter.request_id)
            item.setToolTip(3, inter.prompt)
            for pid in inter.proposal_ids:
                p = session.proposals.get(pid)
                if p is None:
                    continue
                child = QTreeWidgetItem(
                    [
                        "",
                        "",
                        "",
                        p.current.operation.label,
                        p.state.value.upper() + (" (edited)" if p.user_modified else ""),
                    ]
                )
                child.setData(0, 256, pid)
                child.setToolTip(4, p.status_note)
                item.addChild(child)
            self.tree.addTopLevelItem(item)
            item.setExpanded(True)
        for col in range(len(COLUMNS)):
            self.tree.resizeColumnToContents(col)
        decisions = [e for e in self.history.entries() if e.kind == "DecisionAction"]
        self.decisions.setText(
            f"{len(decisions)} decision(s) recorded in command history (undoable). "
            "API keys are never stored in history."
            if decisions
            else "No proposal decisions yet. Approvals and rejections are recorded in history."
        )

    def decision_labels(self) -> list[str]:
        return [e.label for e in self.history.entries() if e.kind == "DecisionAction"]

    def _rate(self, rating: int | None) -> None:
        """Rate the selected answer (+1/-1/None). Feeds prompt evolution."""
        session = self.service.session
        current = self.tree.currentItem()
        request_id = current.data(0, 257) if current is not None else None
        if session is None or not isinstance(request_id, str):
            return
        if session.rate_interaction(request_id, rating):
            self.refresh()

    def _activate(self, item: QTreeWidgetItem, _col: int) -> None:
        pid = item.data(0, 256)
        if pid:
            self.proposalActivated.emit(str(pid))
