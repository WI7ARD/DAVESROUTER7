"""Route Review panel: router job status, candidate list and accept/reject controls.

Candidates are proposals only: they are drawn as an overlay and nothing reaches
the working board until the user presses **Accept Route** (and nothing reaches the
KiCad file until an explicit export).
"""

from __future__ import annotations

from html import escape

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from pcbrouter.domain.units import format_mm
from pcbrouter.routing.result import RouteCandidate, RouteResult, RouteStatus


def candidate_html(result: RouteResult, cand: RouteCandidate | None) -> str:
    head = (
        f"<p><b>ROUTER RESULT</b>: {escape(result.status.value)}"
        + (f" ({escape(result.reason.value)})" if result.reason else "")
        + f" — net <b>{escape(result.net)}</b></p>"
    )
    parts = [head]
    if result.message:
        parts.append(f"<p>{escape(result.message)}</p>")
    if cand is not None:
        s = cand.score
        p = cand.proposal
        parts.append(
            "<table>"
            f"<tr><td>Candidate</td><td><b>{escape(cand.label)}</b></td></tr>"
            f"<tr><td>Validation</td><td><b>{escape(cand.validation.status.value)}</b> "
            "(deterministic geometry check)</td></tr>"
            f"<tr><td>Length</td><td>{escape(format_mm(round(s.length_nm)))}</td></tr>"
            f"<tr><td>Vias</td><td>{s.vias}</td></tr>"
            f"<tr><td>Bends</td><td>{s.bends}</td></tr>"
            f"<tr><td>Layers</td><td>{escape(', '.join(p.layers))}</td></tr>"
            f"<tr><td>Width</td><td>{escape(format_mm(p.width) if p.width else 'mixed')}"
            f" ({escape(str(p.metadata.get('width_source', '')))})</td></tr>"
            f"<tr><td>Segments</td><td>{len(p.segments)}</td></tr>"
            f"<tr><td>Score (router metric)</td><td>{s.cost / 1e6:.2f}</td></tr>"
            "</table>"
        )
        warns = [*cand.warnings, *[m for m in cand.validation.messages if m.startswith("warning")]]
        if warns:
            parts.append(
                "<p style='color:#ffb020'>" + "<br>".join(escape(w) for w in warns) + "</p>"
            )
    if result.details:
        parts.append(
            "<p style='color:#8a94a3'>"
            + "<br>".join(escape(d) for d in result.details[:8])
            + "</p>"
        )
    if result.blockers and not result.candidates:
        rows = "".join(f"<li>{escape(k)}: {v:,}</li>" for k, v in sorted(result.blockers.items()))
        parts.append(f"<p>Blocking cells in the searched area:</p><ul>{rows}</ul>")
    m = result.metrics
    parts.append(
        f"<p style='color:#8a94a3'>{m.expanded_nodes:,} nodes · {m.searches} search(es) · "
        f"{m.repairs} repair(s) · {m.elapsed_s:.2f} s · backend {escape(m.backend)}</p>"
    )
    return "".join(parts)


class RoutePanel(QWidget):
    acceptRequested = Signal(object)  # RouteCandidate
    rejectRequested = Signal()
    candidateChanged = Signal(object)  # RouteCandidate | None
    cancelRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.result: RouteResult | None = None
        self.status = QLabel("No routing job.")
        self.status.setWordWrap(True)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancelRequested)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._on_row)
        self.view = QTextBrowser()
        self.accept_button = QPushButton("Accept Route")
        self.accept_button.setToolTip("Commit this candidate to the working board (undoable)")
        self.reject_button = QPushButton("Reject")
        self.alt_button = QPushButton("Try Alternative")
        self.accept_button.clicked.connect(self._accept)
        self.reject_button.clicked.connect(self.rejectRequested)
        self.alt_button.clicked.connect(self.next_alternative)
        top = QHBoxLayout()
        top.addWidget(self.status, 1)
        top.addWidget(self.cancel_button)
        buttons = QHBoxLayout()
        for b in (self.accept_button, self.reject_button, self.alt_button):
            buttons.addWidget(b)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(top)
        layout.addWidget(self.list)
        layout.addWidget(self.view, 1)
        layout.addLayout(buttons)
        self.set_result(None)

    @property
    def current(self) -> RouteCandidate | None:
        if self.result is None:
            return None
        row = self.list.currentRow()
        if 0 <= row < len(self.result.candidates):
            return self.result.candidates[row]
        return None

    def set_running(self, text: str) -> None:
        self.status.setText(text)
        self.cancel_button.setEnabled(True)
        for b in (self.accept_button, self.reject_button, self.alt_button):
            b.setEnabled(False)

    def set_result(self, result: RouteResult | None) -> None:
        self.result = result
        self.cancel_button.setEnabled(False)
        self.list.blockSignals(True)
        self.list.clear()
        if result is not None:
            for c in result.candidates:
                item = QListWidgetItem(
                    f"{c.label}: {format_mm(round(c.score.length_nm))}, {c.score.vias} via(s), "
                    f"{', '.join(c.proposal.layers)} — {c.validation.status.value}"
                )
                item.setToolTip(c.score.describe())
                self.list.addItem(item)
        self.list.blockSignals(False)
        if result is None:
            self.status.setText(
                "No routing job. Select a net and press R (Router ▸ Route Selected Net)."
            )
            self.view.clear()
        else:
            ok = result.status in (RouteStatus.SUCCESS, RouteStatus.PARTIAL)
            self.status.setText(
                f"{result.net}: {result.status.value}"
                + (f" — {len(result.candidates)} validated candidate(s)" if ok else "")
            )
        has = bool(result and result.candidates)
        self.accept_button.setEnabled(has)
        self.reject_button.setEnabled(has)
        self.alt_button.setEnabled(bool(result and len(result.candidates) > 1))
        if has:
            self.list.setCurrentRow(0)
            self._on_row(0)
        elif result is not None:
            self.view.setHtml(candidate_html(result, None))
            self.candidateChanged.emit(None)

    def next_alternative(self) -> None:
        n = self.list.count()
        if n > 1:
            self.list.setCurrentRow((self.list.currentRow() + 1) % n)

    def _on_row(self, row: int) -> None:
        if self.result is None:
            return
        cand = self.current
        self.view.setHtml(candidate_html(self.result, cand))
        self.candidateChanged.emit(cand)

    def _accept(self) -> None:
        cand = self.current
        if cand is not None:
            self.acceptRequested.emit(cand)

    def text(self) -> str:
        return self.view.toPlainText()


__all__ = ["RoutePanel", "candidate_html"]
