"""Internal Geometry Check results panel (Stage 3 spec §51-§53).

Named "Internal Geometry Check" everywhere: this is the application's own
geometry/rule scan and is NOT KiCad DRC (see docs/internal_drc.md). Clicking a row
asks the window to zoom to the violation and highlight the objects involved.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from pcbrouter.domain.units import format_mm, internal_to_mm
from pcbrouter.drc.result import CHECK_NAME, DRCResult, DRCStatus
from pcbrouter.drc.violation import DRCViolation, Severity
from pcbrouter.ui.overlays import severity_color

COLUMNS = ("Severity", "Type", "Net", "Layer", "Location", "Message")
ALL_LAYERS = "All layers"
DISCLAIMER = (
    f"{CHECK_NAME} — the router's own geometry and rule scan. It is NOT KiCad DRC and "
    "does not cover every KiCad rule; run KiCad DRC before manufacturing."
)
_Index = QModelIndex | QPersistentModelIndex


def _location(v: DRCViolation) -> str:
    if v.location is None:
        return "—"
    return f"({internal_to_mm(v.location.x):.3f}, {internal_to_mm(v.location.y):.3f})"


def violation_tooltip(v: DRCViolation) -> str:
    lines = [f"{v.severity.value.upper()} · {v.kind.value}", v.message]
    if v.actual_value is not None:
        lines.append(f"Actual: {internal_to_mm(round(v.actual_value)):.4f} mm")
    if v.required_value is not None:
        lines.append(f"Required: {format_mm(v.required_value)}")
    if v.rule_source:
        lines.append(f"Rule source: {v.rule_source}")
    lines.append(f"Geometry: {v.accuracy.label}")
    lines.append(f"Id: {v.id}")
    return "\n".join(lines)


class ViolationModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[DRCViolation] = []

    def set_violations(self, rows: list[DRCViolation]) -> None:
        self.beginResetModel()
        self.rows = sorted(rows, key=lambda v: (v.severity.rank, v.kind.value, v.id))
        self.endResetModel()

    def rowCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        v = self.rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return (
                v.severity.value.upper(), v.kind.value, v.nets, v.layer or "—", _location(v),
                v.message,
            )[col]  # fmt: skip
        if role == Qt.ItemDataRole.ToolTipRole:
            return violation_tooltip(v)
        if role == Qt.ItemDataRole.ForegroundRole and col == 0:
            return severity_color(v.severity)
        if role == Qt.ItemDataRole.UserRole:
            return v
        return None


class ViolationFilter(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self.severities: set[Severity] = {Severity.ERROR, Severity.WARNING, Severity.INFO}
        self.layer: str | None = None
        self.net_text = ""

    def set_criteria(self, severities: set[Severity], layer: str | None, net_text: str) -> None:
        self.beginFilterChange()  # Qt 6.10+: bracket filter changes
        self.severities = severities
        self.layer = layer
        self.net_text = net_text
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, row: int, parent: _Index) -> bool:
        model = self.sourceModel()
        assert isinstance(model, ViolationModel)
        v = model.rows[row]
        if v.severity not in self.severities:
            return False
        if self.layer is not None and v.layer != self.layer:
            return False
        if self.net_text:
            needle = self.net_text.lower()
            if not any(n and needle in n.lower() for n in (v.net_a, v.net_b)):
                return False
        return True


class DRCPanel(QWidget):
    runRequested = Signal()
    violationActivated = Signal(object)  # DRCViolation

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.result: DRCResult | None = None
        self.model = ViolationModel()
        self.proxy = ViolationFilter()
        self.proxy.setSourceModel(self.model)

        self.run_button = QPushButton(f"Run {CHECK_NAME}")
        self.run_button.setToolTip(DISCLAIMER)
        self.run_button.clicked.connect(self.runRequested)
        self.summary = QLabel("Not run.")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        note = QLabel(DISCLAIMER)
        note.setWordWrap(True)
        note.setProperty("role", "muted")

        self.show_errors = QCheckBox("Errors")
        self.show_warnings = QCheckBox("Warnings")
        self.show_info = QCheckBox("Info")
        for box in (self.show_errors, self.show_warnings, self.show_info):
            box.setChecked(True)
            box.toggled.connect(self._apply_filters)
        self.layer_combo = QComboBox()
        self.layer_combo.addItem(ALL_LAYERS)
        self.layer_combo.currentIndexChanged.connect(self._apply_filters)
        self.net_filter = QLineEdit()
        self.net_filter.setPlaceholderText("Filter by net…")
        self.net_filter.setClearButtonEnabled(True)
        self.net_filter.textChanged.connect(self._apply_filters)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.table.clicked.connect(self._on_clicked)
        self.table.activated.connect(self._on_clicked)

        top = QHBoxLayout()
        top.addWidget(self.run_button)
        top.addWidget(self.summary, 1)
        filters = QHBoxLayout()
        for w in (self.show_errors, self.show_warnings, self.show_info, self.layer_combo):
            filters.addWidget(w)
        filters.addWidget(self.net_filter, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(top)
        layout.addWidget(note)
        layout.addLayout(filters)
        layout.addWidget(self.table, 1)
        self.set_result(None)

    # ------------------------------------------------------------ public
    def set_enabled_for_board(self, has_board: bool) -> None:
        self.run_button.setEnabled(has_board)

    def set_running(self, running: bool) -> None:
        self.run_button.setEnabled(not running)
        if running:
            self.summary.setText(f"{CHECK_NAME}: running…")

    def set_result(self, result: DRCResult | None) -> None:
        self.result = result
        self.model.set_violations(list(result.violations) if result else [])
        layers = sorted({v.layer for v in result.violations if v.layer}) if result else []
        self.layer_combo.blockSignals(True)
        self.layer_combo.clear()
        self.layer_combo.addItem(ALL_LAYERS)
        self.layer_combo.addItems(layers)
        self.layer_combo.blockSignals(False)
        self._apply_filters()
        if result is None:
            self.summary.setText(f"{CHECK_NAME}: not run.")
            return
        color = {DRCStatus.PASS: "#34d399", DRCStatus.WARNINGS: "#ffb020",
                 DRCStatus.FAIL: "#ff3b30"}[result.status]  # fmt: skip
        skipped = sum(result.skipped.values())
        extra = f" · {skipped:,} check(s) skipped (rule unknown)" if skipped else ""
        self.summary.setText(
            f"<b>{CHECK_NAME}</b>: <span style='color:{color}'><b>{result.status.value}</b>"
            f"</span> · Errors: {len(result.errors)} · Warnings: {len(result.warnings)} · "
            f"Info: {len(result.infos)} · Checks: {result.check_count:,} · "
            f"Duration: {result.elapsed_time:.2f} s{extra}"
        )

    def visible_violations(self) -> list[DRCViolation]:
        out: list[DRCViolation] = []
        for row in range(self.proxy.rowCount()):
            v = self.proxy.data(self.proxy.index(row, 0), Qt.ItemDataRole.UserRole)
            if isinstance(v, DRCViolation):
                out.append(v)
        return out

    def select_row(self, row: int) -> DRCViolation | None:
        index = self.proxy.index(row, 0)
        if not index.isValid():
            return None
        self.table.selectRow(row)
        self._on_clicked(index)
        v = self.proxy.data(index, Qt.ItemDataRole.UserRole)
        return v if isinstance(v, DRCViolation) else None

    # ------------------------------------------------------------ internals
    def _apply_filters(self) -> None:
        sev = set()
        if self.show_errors.isChecked():
            sev.add(Severity.ERROR)
        if self.show_warnings.isChecked():
            sev.add(Severity.WARNING)
        if self.show_info.isChecked():
            sev.add(Severity.INFO)
        layer = self.layer_combo.currentText()
        self.proxy.set_criteria(
            sev, None if layer in ("", ALL_LAYERS) else layer, self.net_filter.text().strip()
        )

    def _on_clicked(self, index: QModelIndex) -> None:
        v = self.proxy.data(self.proxy.index(index.row(), 0), Qt.ItemDataRole.UserRole)
        if isinstance(v, DRCViolation):
            self.violationActivated.emit(v)


def status_text(result: DRCResult | None) -> tuple[str, QColor | None]:
    """Status-bar text for the last check."""
    if result is None:
        return "Internal DRC: Not run", None
    label = {DRCStatus.PASS: "Pass", DRCStatus.WARNINGS: "Warnings", DRCStatus.FAIL: "Fail"}
    color = {DRCStatus.PASS: "#34d399", DRCStatus.WARNINGS: "#ffb020", DRCStatus.FAIL: "#ff3b30"}
    return f"Internal DRC: {label[result.status]}", QColor(color[result.status])
