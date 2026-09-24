"""Stage 3 engineering dialogs: test geometry, routing grid, overrides, diagnostics.

"Validate Test Segment/Via" is geometry *validation*, not PCB editing: the
candidate lives only in memory (a :class:`RouteProposal`), is drawn as a temporary
overlay, and is never added to the board or written to any file.
"""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from pcbrouter import __version__
from pcbrouter.board_engine import BoardEngine
from pcbrouter.domain.geometry import Point
from pcbrouter.domain.units import Nm, format_mm, internal_to_mm, mm_to_internal
from pcbrouter.geometry.shapes import Shape, capsule, circle
from pcbrouter.routing.collision import CollisionResult, ValidationStatus
from pcbrouter.routing.occupancy import GRID_RESOLUTIONS_MM
from pcbrouter.routing.proposal import RouteProposal, RouteSegment, RouteVia
from pcbrouter.rules.overrides import BoardOverride, NetOverride, RuleOverrides

NO_NET = "(no net)"
NOT_ADDED = "Hypothetical geometry — validated in memory only, NOT added to the board."
_STATUS_COLOR = {
    ValidationStatus.VALID: "#34d399",
    ValidationStatus.VALID_WITH_WARNINGS: "#34d399",
    ValidationStatus.INVALID: "#ff3b30",
    ValidationStatus.RULE_UNKNOWN: "#ffb020",
}


def _mm_spin(value_mm: float = 0.0, lo: float = -10_000.0, hi: float = 10_000.0) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(lo, hi)
    spin.setDecimals(4)
    spin.setSingleStep(0.05)
    spin.setSuffix(" mm")
    spin.setValue(value_mm)
    return spin


def _mm(nm: float | None) -> str:
    return "unknown" if nm is None else format_mm(round(nm))


def collision_report(result: CollisionResult) -> str:
    """Plain-text report of one validation (also used by tests and the log)."""
    lines = [f"Status: {result.status.value}", NOT_ADDED]
    if result.collisions:
        lines.append(f"Violations ({len(result.collisions)}):")
        for c in result.collisions:
            detail = []
            if c.distance is not None:
                detail.append(f"actual {_mm(c.distance)}")
            if c.required is not None:
                detail.append(f"required {_mm(c.required)}")
            if c.rule_source:
                detail.append(c.rule_source)
            if c.accuracy.value != "exact":
                detail.append(c.accuracy.label)
            where = ""
            if c.location is not None:
                where = (
                    f" at ({internal_to_mm(c.location.x):.3f}, {internal_to_mm(c.location.y):.3f})"
                )
            lines.append(f"  - [{c.violation.label}] {c.message}{where}"
                         + (f" ({'; '.join(detail)})" if detail else ""))  # fmt: skip
    for u in result.unknowns:
        src = f" ({u.rule_source})" if u.rule_source else ""
        lines.append(f"  - RULE UNKNOWN: {u.message}{src}")
    for w in result.warnings:
        lines.append(f"  - warning: {w}")
    if result.same_net_contacts:
        lines.append("Same-net contacts: " + ", ".join(result.same_net_contacts[:10]))
    if result.minimum_observed_clearance is not None:
        lines.append(f"Minimum observed clearance: {_mm(result.minimum_observed_clearance)}")
    if result.required_clearance is not None:
        lines.append(f"Required clearance: {_mm(result.required_clearance)}")
    lines.append(f"Checks: {result.checks} in {result.elapsed_s * 1e3:.2f} ms")
    return "\n".join(lines)


def report_html(result: CollisionResult) -> str:
    color = _STATUS_COLOR[result.status]
    body = collision_report(result).split("\n", 1)[1]
    return (
        f"<p><b>DETERMINISTIC GEOMETRY CHECK:</b> <span style='color:{color}'><b>"
        f"{result.status.value}</b></span></p><pre>{body.replace('<', '&lt;')}</pre>"
    )


class _TestGeometryDialog(QDialog):
    """Shared net/layer pickers and result view."""

    validated = Signal(object, object, object)  # (RouteProposal, shapes, CollisionResult)

    def __init__(self, engine: BoardEngine, title: str, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(False)
        self.engine = engine
        self.last_result: CollisionResult | None = None
        self.last_proposal: RouteProposal | None = None
        self.net_combo = QComboBox()
        self.net_combo.addItem(NO_NET, None)
        for net in sorted(n.name for n in engine.board.nets if n.name):
            self.net_combo.addItem(net, net)
        self.form = QFormLayout()
        self.form.addRow("Net:", self.net_combo)
        self.result_view = QTextBrowser()
        self.result_view.setMinimumHeight(180)
        banner = QLabel(NOT_ADDED + " This is geometry validation, not PCB editing.")
        banner.setWordWrap(True)
        banner.setProperty("role", "banner")
        self.validate_button = QPushButton("Validate")
        self.validate_button.setDefault(True)  # subclasses connect it to validate()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(self.validate_button)
        row.addStretch(1)
        row.addWidget(buttons)
        layout = QVBoxLayout(self)
        layout.addWidget(banner)
        layout.addLayout(self.form)
        layout.addLayout(row)
        layout.addWidget(self.result_view, 1)

    def _layer_combo(self, default: str | None = None) -> QComboBox:
        combo = QComboBox()
        layers = list(self.engine.geometry.copper_layers)
        combo.addItems(layers)
        if default in layers:
            combo.setCurrentText(default)
        return combo

    @property
    def net(self) -> str | None:
        data = self.net_combo.currentData()
        return data if isinstance(data, str) else None

    def set_net(self, net: str | None) -> None:
        idx = self.net_combo.findData(net) if net else 0
        self.net_combo.setCurrentIndex(max(idx, 0))

    def _show(self, proposal: RouteProposal, shapes: list[Shape], result: CollisionResult) -> None:
        self.last_result = result
        self.last_proposal = proposal
        self.result_view.setHtml(report_html(result))
        self.validated.emit(proposal, shapes, result)


class TestSegmentDialog(_TestGeometryDialog):
    def __init__(
        self,
        engine: BoardEngine,
        parent: QWidget | None = None,
        layer: str | None = None,
        net: str | None = None,
    ) -> None:
        super().__init__(engine, "Validate Test Segment", parent)
        self.layer_combo = self._layer_combo(layer)
        box = engine.board.bounds
        cx = internal_to_mm(box.center.x) if box else 0.0
        cy = internal_to_mm(box.center.y) if box else 0.0
        self.start_x, self.start_y = _mm_spin(cx - 2), _mm_spin(cy)
        self.end_x, self.end_y = _mm_spin(cx + 2), _mm_spin(cy)
        self.width_spin = _mm_spin(0.25, 0.0, 100.0)
        self.use_rule = QPushButton("Use the net's preferred width")
        self.use_rule.clicked.connect(self.apply_preferred_width)
        start = QHBoxLayout()
        start.addWidget(self.start_x)
        start.addWidget(self.start_y)
        end = QHBoxLayout()
        end.addWidget(self.end_x)
        end.addWidget(self.end_y)
        width = QHBoxLayout()
        width.addWidget(self.width_spin)
        width.addWidget(self.use_rule)
        self.form.addRow("Layer:", self.layer_combo)
        self.form.addRow("Start X / Y:", start)
        self.form.addRow("End X / Y:", end)
        self.form.addRow("Width:", width)
        self.net_combo.currentIndexChanged.connect(self.apply_preferred_width)
        self.validate_button.clicked.connect(self.validate)
        self.set_net(net)
        self.apply_preferred_width()

    def apply_preferred_width(self) -> None:
        rule = self.engine.resolver.resolve_trace_width(self.net, self.layer_combo.currentText())
        if rule.value is not None:
            self.width_spin.setValue(internal_to_mm(rule.value))
            self.width_spin.setToolTip(f"Preferred width: {rule.describe()}")

    def set_points(self, start: tuple[float, float], end: tuple[float, float]) -> None:
        self.start_x.setValue(start[0])
        self.start_y.setValue(start[1])
        self.end_x.setValue(end[0])
        self.end_y.setValue(end[1])

    def validate(self) -> CollisionResult:
        layer = self.layer_combo.currentText()
        a = Point(mm_to_internal(self.start_x.value()), mm_to_internal(self.start_y.value()))
        b = Point(mm_to_internal(self.end_x.value()), mm_to_internal(self.end_y.value()))
        width: Nm = mm_to_internal(self.width_spin.value())
        result = self.engine.validator.validate_segment(self.net, layer, a, b, width)
        seg = RouteSegment(a, b, layer, width)
        proposal = RouteProposal(self.net or "", segments=(seg,))
        shapes = [capsule(a, b, width // 2)] if width > 0 else []
        self._show(proposal, shapes, result)
        return result


class TestViaDialog(_TestGeometryDialog):
    def __init__(
        self,
        engine: BoardEngine,
        parent: QWidget | None = None,
        net: str | None = None,
    ) -> None:
        super().__init__(engine, "Validate Test Via", parent)
        layers = list(engine.geometry.copper_layers)
        self.start_layer = self._layer_combo(layers[0] if layers else None)
        self.end_layer = self._layer_combo(layers[-1] if layers else None)
        box = engine.board.bounds
        self.pos_x = _mm_spin(internal_to_mm(box.center.x) if box else 0.0)
        self.pos_y = _mm_spin(internal_to_mm(box.center.y) if box else 0.0)
        self.diameter = _mm_spin(0.6, 0.0, 100.0)
        self.drill = _mm_spin(0.3, 0.0, 100.0)
        self.use_rule = QPushButton("Use the net's via size")
        self.use_rule.clicked.connect(self.apply_via_rules)
        pos = QHBoxLayout()
        pos.addWidget(self.pos_x)
        pos.addWidget(self.pos_y)
        size = QHBoxLayout()
        size.addWidget(self.diameter)
        size.addWidget(self.drill)
        size.addWidget(self.use_rule)
        self.form.addRow("Position X / Y:", pos)
        self.form.addRow("From layer:", self.start_layer)
        self.form.addRow("To layer:", self.end_layer)
        self.form.addRow("Diameter / drill:", size)
        self.net_combo.currentIndexChanged.connect(self.apply_via_rules)
        self.validate_button.clicked.connect(self.validate)
        self.set_net(net)
        self.apply_via_rules()

    def apply_via_rules(self) -> None:
        via = self.engine.resolver.resolve_via_rules(self.net)
        if via.diameter.value is not None:
            self.diameter.setValue(internal_to_mm(via.diameter.value))
        if via.drill.value is not None:
            self.drill.setValue(internal_to_mm(via.drill.value))

    def validate(self) -> CollisionResult:
        p = Point(mm_to_internal(self.pos_x.value()), mm_to_internal(self.pos_y.value()))
        dia = mm_to_internal(self.diameter.value())
        drill = mm_to_internal(self.drill.value())
        a, b = self.start_layer.currentText(), self.end_layer.currentText()
        result = self.engine.validator.validate_via(self.net, p, a, b, dia, drill)
        proposal = RouteProposal(self.net or "", vias=(RouteVia(p, a, b, dia, drill),))
        shapes = [circle(p, dia // 2)] if dia > 0 else []
        self._show(proposal, shapes, result)
        return result


class RoutingGridDialog(QDialog):
    """View ▸ Routing Grid settings (resolution, net, width, layer)."""

    def __init__(
        self,
        engine: BoardEngine,
        parent: QWidget | None = None,
        layer: str | None = None,
        net: str | None = None,
        resolution_mm: float = GRID_RESOLUTIONS_MM[0],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Routing Grid")
        self.engine = engine
        self.layer_combo = QComboBox()
        self.layer_combo.addItems(list(engine.geometry.copper_layers))
        if layer:
            self.layer_combo.setCurrentText(layer)
        self.net_combo = QComboBox()
        self.net_combo.addItem(NO_NET, None)
        for n in sorted(x.name for x in engine.board.nets if x.name):
            self.net_combo.addItem(n, n)
        if net:
            self.net_combo.setCurrentIndex(max(self.net_combo.findData(net), 0))
        self.resolution = QComboBox()
        for mm in GRID_RESOLUTIONS_MM:
            self.resolution.addItem(f"{mm:.2f} mm", mm)
        self.resolution.setCurrentIndex(max(self.resolution.findData(resolution_mm), 0))
        self.width_spin = _mm_spin(0.25, 0.001, 100.0)
        self.net_combo.currentIndexChanged.connect(self._preferred)
        self._preferred()
        form = QFormLayout()
        form.addRow("Layer:", self.layer_combo)
        form.addRow("Net (same-net copper is passable):", self.net_combo)
        form.addRow("Trace width:", self.width_spin)
        form.addRow("Resolution:", self.resolution)
        note = QLabel(
            "Cells are sampled at their centres against obstacles inflated by half the "
            "trace width plus the resolved clearance. Colours: green = same net, red = "
            "foreign copper, grey = holes, magenta = keepout, yellow = board edge, orange = "
            "rule unknown, transparent = free."
        )
        note.setWordWrap(True)
        note.setProperty("role", "muted")
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Show Grid")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def _preferred(self) -> None:
        rule = self.engine.resolver.resolve_trace_width(self.net)
        if rule.value is not None:
            self.width_spin.setValue(internal_to_mm(rule.value))

    @property
    def net(self) -> str | None:
        data = self.net_combo.currentData()
        return data if isinstance(data, str) else None

    def request(self) -> tuple[str, str | None, Nm, Nm]:
        """(layer, net, width nm, cell nm)."""
        return (
            self.layer_combo.currentText(),
            self.net,
            mm_to_internal(self.width_spin.value()),
            mm_to_internal(float(self.resolution.currentData())),
        )


class OverridesDialog(QDialog):
    """Tools ▸ Routing Rule Overrides — session/project router overrides.

    Stored in the board's workspace (``router_overrides.json``), never in KiCad
    files. An override can tighten a rule or define a missing value; it can never
    weaken a known rule (see docs/rules_engine.md).
    """

    COLS = ("Net", "Width (mm)", "Clearance (mm)", "Max vias")

    def __init__(
        self, engine: BoardEngine, current: RuleOverrides, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Routing Rule Overrides")
        self.setMinimumWidth(560)
        self.engine = engine
        b = current.board
        self.board_clearance = self._opt_mm(b.clearance)
        self.board_edge = self._opt_mm(b.edge_clearance)
        self.board_width = self._opt_mm(b.track_width)
        self.board_vias = QSpinBox()
        self.board_vias.setRange(-1, 1000)
        self.board_vias.setSpecialValueText("not set")
        self.board_vias.setValue(b.max_vias if b.max_vias is not None else -1)
        form = QFormLayout()
        form.addRow("Default clearance (where no rule states one):", self.board_clearance)
        form.addRow("Board-edge clearance:", self.board_edge)
        form.addRow("Default track width (where no rule states one):", self.board_width)
        form.addRow("Max vias per net:", self.board_vias)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(list(self.COLS))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for net, ov in sorted(current.nets.items()):
            if ov.origin == "user":
                self._add_row(net, ov)
        self.net_combo = QComboBox()
        for n in sorted(x.name for x in engine.board.nets if x.name):
            self.net_combo.addItem(n)
        add = QPushButton("Add net override")
        add.clicked.connect(lambda: self._add_row(self.net_combo.currentText(), NetOverride()))
        remove = QPushButton("Remove selected")
        remove.clicked.connect(self._remove_selected)
        row = QHBoxLayout()
        row.addWidget(self.net_combo, 1)
        row.addWidget(add)
        row.addWidget(remove)
        note = QLabel(
            "Overrides are router preferences, separate from the KiCad rules, and are saved "
            "in this board's workspace only. They can tighten a rule or define a missing "
            "value — they never weaken a known minimum: a width below the resolved minimum "
            "makes validation INVALID instead of being applied. Leave a cell empty for "
            "'not set'."
        )
        note.setWordWrap(True)
        note.setProperty("role", "banner")
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.error = QLabel("")
        self.error.setStyleSheet("color:#ff3b30")
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addLayout(form)
        layout.addWidget(QLabel("Per-net overrides:"))
        layout.addWidget(self.table, 1)
        layout.addLayout(row)
        layout.addWidget(self.error)
        layout.addWidget(buttons)

    @staticmethod
    def _opt_mm(value: Nm | None) -> QDoubleSpinBox:
        spin = _mm_spin(internal_to_mm(value) if value is not None else 0.0, 0.0, 100.0)
        spin.setSpecialValueText("not set")  # shown at the minimum (0)
        return spin

    def _add_row(self, net: str, ov: NetOverride) -> None:
        if not net:
            return
        for r in range(self.table.rowCount()):
            cell = self.table.item(r, 0)
            if cell is not None and cell.text() == net:
                return
        row = self.table.rowCount()
        self.table.insertRow(row)
        name = QTableWidgetItem(net)
        name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, 0, name)
        vals = (
            "" if ov.width is None else f"{internal_to_mm(ov.width):g}",
            "" if ov.clearance is None else f"{internal_to_mm(ov.clearance):g}",
            "" if ov.max_vias is None else str(ov.max_vias),
        )
        for col, text in enumerate(vals, start=1):
            self.table.setItem(row, col, QTableWidgetItem(text))

    def _remove_selected(self) -> None:
        for r in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(r)

    @staticmethod
    def _spin_nm(spin: QDoubleSpinBox) -> Nm | None:
        return mm_to_internal(spin.value()) if spin.value() > 0 else None

    def result_overrides(self) -> RuleOverrides:
        """Parse the form; raises ValueError with a readable message."""
        board = BoardOverride(
            clearance=self._spin_nm(self.board_clearance),
            edge_clearance=self._spin_nm(self.board_edge),
            track_width=self._spin_nm(self.board_width),
            max_vias=self.board_vias.value() if self.board_vias.value() >= 0 else None,
        )
        nets: dict[str, NetOverride] = {}
        for r in range(self.table.rowCount()):
            net_item = self.table.item(r, 0)
            if net_item is None:
                continue
            net = net_item.text()

            def cell(col: int, net: str = net, row: int = r) -> str:
                item = self.table.item(row, col)
                return item.text().strip() if item is not None else ""

            try:
                width = mm_to_internal(float(cell(1))) if cell(1) else None
                clearance = mm_to_internal(float(cell(2))) if cell(2) else None
                max_vias = int(cell(3)) if cell(3) else None
            except ValueError as exc:
                raise ValueError(f"{net}: not a number ({exc})") from exc
            if any(v is not None and v <= 0 for v in (width, clearance)) or (
                max_vias is not None and max_vias < 0
            ):
                raise ValueError(f"{net}: values must be positive")
            ov = NetOverride(width=width, clearance=clearance, max_vias=max_vias)
            if ov != NetOverride():
                nets[net] = ov
        return RuleOverrides(board=board, nets=nets)

    def accept(self) -> None:
        try:
            self.result_overrides()
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        super().accept()


def diagnostics_data(
    engine: BoardEngine | None, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    data: dict[str, Any] = {"application_version": __version__}
    if engine is None:
        data["board"] = "no board open"
    else:
        data.update(engine.diagnostics())
    if extra:
        data.update(extra)
    return data


class GeometryDiagnosticsDialog(QDialog):
    """Help ▸ Geometry Diagnostics (for bug reports; contains no secrets)."""

    def __init__(self, data: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Geometry Diagnostics")
        self.resize(640, 520)
        self.text = QPlainTextEdit(json.dumps(data, indent=2, default=str))
        self.text.setReadOnly(True)
        copy = QPushButton("Copy to Clipboard")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.text.toPlainText()))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(copy)
        row.addStretch(1)
        row.addWidget(buttons)
        layout = QVBoxLayout(self)
        layout.addWidget(self.text, 1)
        layout.addLayout(row)
