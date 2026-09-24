"""Routing Rules inspector (Stage 3 spec §67, §75).

Shows, for the selected net, every resolved routing value *with its source*
(net class, custom rule, board minimum, override…). Values no rule states are
shown as "unknown", never guessed. Board-level rule files, net classes and
unsupported rules are listed when no net is selected.
"""

from __future__ import annotations

from html import escape

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from pcbrouter.board_engine import BoardEngine
from pcbrouter.domain.units import format_mm
from pcbrouter.rules.model import UNBOUNDED, ResolvedValue

BOARD_ITEM = "(board rules)"
UNSUPPORTED_BANNER = (
    "Unsupported deterministic rule detected. Routing for affected nets will be "
    "conservative until supported."
)


def _value(v: ResolvedValue) -> str:
    text = "<i>unknown</i>" if v.value is None else f"<b>{escape(format_mm(v.value))}</b>"
    src = escape(v.source.describe())
    extra = ""
    if v.possibly_stricter is not None:
        rules = ", ".join(v.possibly_stricter_rules)
        limit = (
            "an unreadable value"
            if v.possibly_stricter >= UNBOUNDED
            else escape(format_mm(v.possibly_stricter))
        )
        extra = (
            f"<br><span style='color:#ffb020'>unsupported rule {escape(rules)} may require "
            f"up to {limit}</span>"
        )
    return f"{text}<br><span style='color:#8a94a3'>{src}</span>{extra}"


def _row(label: str, cell: str) -> str:
    return (
        f"<tr><td style='padding-right:12px' valign='top'>{escape(label)}</td><td>{cell}</td></tr>"
    )


def net_rules_html(engine: BoardEngine, net: str) -> str:
    s = engine.resolver.summary(net)
    conn = engine.connectivity.net(net)
    classes = ", ".join(s.net_classes) or "none"
    rows = [
        _row("Net", f"<b>{escape(net)}</b>"),
        _row("Net class", f"<b>{escape(classes)}</b><br><span style='color:#8a94a3'>"
                          f"{escape(s.class_source.describe())}</span>"),
        _row("Preferred width", _value(s.width.preferred)),
        _row("Minimum width (routing)", _value(s.width.minimum)),
        _row("Minimum width (fabrication)", _value(s.width.fab_minimum)),
        _row("Maximum width", _value(s.width.maximum)),
        _row("Clearance", _value(s.clearance)),
        _row("Via diameter", _value(s.via.diameter)),
        _row("Via drill", _value(s.via.drill)),
        _row("Min via diameter", _value(s.via.min_diameter)),
        _row("Min via drill", _value(s.via.min_drill)),
        _row("Board-edge clearance", _value(s.edge_clearance)),
        _row("Max vias", _value_count(s.max_vias)),
        _row("Allowed layers", escape(", ".join(s.layers.allowed) or "none")
             + f"<br><span style='color:#8a94a3'>{escape(s.layers.source.describe())}</span>"),
    ]  # fmt: skip
    if s.layers.forbidden:
        rows.append(_row("Forbidden layers", escape(", ".join(s.layers.forbidden))))
    if conn is not None:
        rows.append(
            _row(
                "Connectivity",
                f"{escape(conn.status.value)} · {conn.pad_count} pad(s) · "
                f"{len(conn.groups)} group(s) · {conn.remaining_connections} connection(s) "
                f"remaining · {conn.via_count} via(s)",
            )
        )
    notes = "".join(f"<li>{escape(n)}</li>" for n in s.notes)
    return (
        "<h3>Routing Rules</h3><table>" + "".join(rows) + "</table>"
        + (f"<p><b>Notes</b></p><ul>{notes}</ul>" if notes else "")
        + "<p style='color:#8a94a3'>Values are resolved deterministically from the board "
        "file, .kicad_pro and .kicad_dru (read-only) and your overrides. Unknown means no "
        "rule states the value; nothing is guessed.</p>"
    )  # fmt: skip


def _value_count(v: ResolvedValue) -> str:
    text = "<i>no limit stated</i>" if v.value is None else f"<b>{v.value}</b>"
    return f"{text}<br><span style='color:#8a94a3'>{escape(v.source.describe())}</span>"


def board_rules_html(engine: BoardEngine) -> str:
    rs = engine.ruleset
    pr = engine.project_rules
    parts = ["<h3>Board Rules</h3>"]
    files = [
        f"{escape(name)} <span style='color:#8a94a3'>sha256 {escape((sha or '—')[:16])}</span>"
        for name, sha in rs.sources.items()
    ]
    parts.append(
        "<p><b>Rule sources (read-only):</b><br>" + ("<br>".join(files) or "none") + "</p>"
    )
    if not pr.found_any:
        parts.append(
            "<p style='color:#ffb020'>No .kicad_pro / .kicad_dru found next to the board: "
            "only the board file's own minimums are known.</p>"
        )
    parts.append(
        "<p>Conservative Rule Handling: "
        f"<b>{'ON' if engine.config.conservative else 'OFF (expert)'}</b>"
        f" · rule digest {escape(rs.digest[:12])}</p>"
    )
    classes = sorted(rs.classes.classes)
    parts.append(
        f"<p><b>Net classes ({len(classes)}):</b> {escape(', '.join(classes) or 'none')}</p>"
    )
    parts.append(
        f"<p><b>Custom rules evaluated:</b> {len(rs.custom_rules)}"
        + (
            f" · <b>ignored (disabled in file):</b> {escape(', '.join(rs.ignored))}"
            if rs.ignored
            else ""
        )
        + "</p>"
    )
    if rs.unsupported:
        items = "".join(
            f"<li>{'<b>CRITICAL</b> ' if u.critical else ''}{escape(u.describe())}</li>"
            for u in rs.unsupported
        )
        parts.append(
            f"<p style='color:#ffb020'><b>{escape(UNSUPPORTED_BANNER)}</b></p><ul>{items}</ul>"
        )
    b = engine.resolver
    rows = [
        _row("Default clearance", _value(b.resolve_net_clearance(None))),
        _row("Default track width", _value(b.resolve_trace_width(None))),
        _row("Board-edge clearance", _value(b.resolve_edge_clearance(None))),
        _row("Hole-to-hole", _value(b.resolve_hole_to_hole())),
    ]
    parts.append("<table>" + "".join(rows) + "</table>")
    for w in rs.warnings:
        parts.append(f"<p style='color:#ffb020'>{escape(w)}</p>")
    return "".join(parts)


class RulesPanel(QWidget):
    netSelected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.engine: BoardEngine | None = None
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter nets…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._on_current)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        self.placeholder = QLabel("Open a board to inspect its routing rules.")
        self.placeholder.setWordWrap(True)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(self.filter)
        lv.addWidget(self.list)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(self.view)
        split.setStretchFactor(1, 3)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.placeholder)
        layout.addWidget(split, 1)
        self.set_engine(None)

    def set_engine(self, engine: BoardEngine | None) -> None:
        self.engine = engine
        current = self.current_net()
        self.list.blockSignals(True)
        self.list.clear()
        self.placeholder.setVisible(engine is None)
        if engine is not None:
            self.list.addItem(QListWidgetItem(BOARD_ITEM))
            for net in sorted(n.name for n in engine.board.nets if n.name):
                self.list.addItem(QListWidgetItem(net))
        self.list.blockSignals(False)
        self._apply_filter()
        if engine is None:
            self.view.clear()
            return
        if current and self.show_net(current):
            return
        self.list.setCurrentRow(0)
        self._render(BOARD_ITEM)

    def current_net(self) -> str | None:
        item = self.list.currentItem()
        if item is None or item.text() == BOARD_ITEM:
            return None
        return item.text()

    def show_net(self, net: str) -> bool:
        matches = self.list.findItems(net, Qt.MatchFlag.MatchExactly)
        if not matches:
            return False
        self.list.setCurrentItem(matches[0])
        return True

    def html(self) -> str:
        return self.view.toHtml()

    def _apply_filter(self) -> None:
        needle = self.filter.text().strip().lower()
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setHidden(
                bool(needle) and item.text() != BOARD_ITEM and needle not in item.text().lower()
            )

    def _on_current(self, item: QListWidgetItem | None, _prev: QListWidgetItem | None) -> None:
        if item is not None:
            self._render(item.text())
            if item.text() != BOARD_ITEM:
                self.netSelected.emit(item.text())

    def _render(self, key: str) -> None:
        if self.engine is None:
            return
        try:
            text = (
                board_rules_html(self.engine)
                if key == BOARD_ITEM
                else net_rules_html(self.engine, key)
            )
        except Exception as exc:  # show, never hide, a resolution failure
            text = f"<p style='color:#ff3b30'>Rule resolution failed: {escape(str(exc))}</p>"
        self.view.setHtml(text)
