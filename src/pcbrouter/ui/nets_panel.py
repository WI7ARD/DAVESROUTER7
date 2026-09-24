"""Searchable net table with show/hide/isolate controls (view-only: no routing)."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QItemSelection,
    QModelIndex,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from pcbrouter.domain.board import Board
from pcbrouter.domain.net import NetStatistics
from pcbrouter.domain.units import internal_to_mm

COLUMNS = ("Net", "Code", "Pads", "Tracks", "Vias", "Routed length (mm)", "View")
_ModelIndex = QModelIndex | QPersistentModelIndex


class NetTableModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._rows: list[NetStatistics] = []
        self._hidden: frozenset[str] = frozenset()
        self._isolated: str | None = None

    def set_board(self, board: Board | None) -> None:
        self.beginResetModel()
        self._rows = (
            [board.index.net_statistics[n.name] for n in board.nets if not n.is_unconnected]
            if board is not None
            else []
        )
        self._hidden = frozenset()
        self._isolated = None
        self.endResetModel()

    def set_view_state(self, hidden: frozenset[str], isolated: str | None) -> None:
        self._hidden, self._isolated = hidden, isolated
        if self._rows:
            last = len(COLUMNS) - 1
            self.dataChanged.emit(self.index(0, last), self.index(len(self._rows) - 1, last))

    def net_at(self, row: int) -> str | None:
        return self._rows[row].net.name if 0 <= row < len(self._rows) else None

    def row_of(self, name: str) -> int:
        return next((i for i, r in enumerate(self._rows) if r.net.name == name), -1)

    def rowCount(self, parent: _ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: _ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.ToolTipRole:
            return {
                1: "KiCad net number (unknown if the file references nets by name only)",
                5: "Sum of existing track centreline lengths on this net (vias excluded)",
                6: "Canvas visibility state for this net",
            }.get(section)
        return None

    def _view_state(self, name: str) -> str:
        if self._isolated is not None:
            return "isolated" if name == self._isolated else "hidden (isolation)"
        return "hidden" if name in self._hidden else "shown"

    def data(self, index: _ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return (
                row.net.display_name,
                str(row.net.code) if row.net.code is not None else "unknown",
                row.pad_count,
                row.track_count,
                row.via_count,
                f"{internal_to_mm(row.routed_length):.3f}",
                self._view_state(row.net.name),
            )[col]
        if role == Qt.ItemDataRole.UserRole:  # sort key
            return (
                row.net.name.casefold(),
                row.net.code if row.net.code is not None else -1,
                row.pad_count,
                row.track_count,
                row.via_count,
                row.routed_length,
                self._view_state(row.net.name),
            )[col]
        if role == Qt.ItemDataRole.TextAlignmentRole and 1 <= col <= 5:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None


class NetsPanel(QWidget):
    netSelected = Signal(str)
    hideRequested = Signal(str)
    showRequested = Signal(str)
    isolateRequested = Signal(str)
    clearIsolationRequested = Signal()
    showAllRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = NetTableModel()
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setFilterKeyColumn(0)
        self.proxy.setSortRole(Qt.ItemDataRole.UserRole)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter nets…")
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip("Case-insensitive substring filter on net names")
        self.search.textChanged.connect(self.proxy.setFilterFixedString)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, len(COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.selectionModel().selectionChanged.connect(self._on_selection)

        self.btn_hide = QPushButton("Hide")
        self.btn_hide.setToolTip("Hide the selected net's copper on the canvas (view only)")
        self.btn_show = QPushButton("Show")
        self.btn_show.setToolTip("Show the selected net again")
        self.btn_isolate = QPushButton("Isolate")
        self.btn_isolate.setToolTip("Show only the selected net's copper")
        self.btn_clear_iso = QPushButton("Clear isolation")
        self.btn_show_all = QPushButton("Show all")
        self.btn_show_all.setToolTip("Show every net and clear isolation")
        self.btn_hide.clicked.connect(lambda: self._emit_for_selected(self.hideRequested))
        self.btn_show.clicked.connect(lambda: self._emit_for_selected(self.showRequested))
        self.btn_isolate.clicked.connect(lambda: self._emit_for_selected(self.isolateRequested))
        self.btn_clear_iso.clicked.connect(self.clearIsolationRequested.emit)
        self.btn_show_all.clicked.connect(self.showAllRequested.emit)

        self.count_label = QLabel("")
        self.count_label.setProperty("role", "muted")

        buttons = QHBoxLayout()
        for b in (
            self.btn_hide,
            self.btn_show,
            self.btn_isolate,
            self.btn_clear_iso,
            self.btn_show_all,
        ):
            buttons.addWidget(b)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.search)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        layout.addWidget(self.count_label)
        self._update_buttons()

    def set_board(self, board: Board | None) -> None:
        self.model.set_board(board)
        self.search.clear()
        n = self.model.rowCount()
        self.count_label.setText(f"{n} nets" if board is not None else "No board loaded")
        self._update_buttons()

    def set_view_state(self, hidden: frozenset[str], isolated: str | None) -> None:
        self.model.set_view_state(hidden, isolated)

    def selected_net(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.model.net_at(self.proxy.mapToSource(rows[0]).row())

    def select_net(self, name: str) -> None:
        row = self.model.row_of(name)
        if row < 0:
            return
        proxy_index = self.proxy.mapFromSource(self.model.index(row, 0))
        if proxy_index.isValid():
            self.table.selectRow(proxy_index.row())
            self.table.scrollTo(proxy_index)

    def _emit_for_selected(self, signal: Any) -> None:
        name = self.selected_net()
        if name is not None:
            signal.emit(name)

    def _on_selection(self, _sel: QItemSelection, _desel: QItemSelection) -> None:
        self._update_buttons()
        name = self.selected_net()
        if name is not None:
            self.netSelected.emit(name)

    def _update_buttons(self) -> None:
        has = self.selected_net() is not None
        for b in (self.btn_hide, self.btn_show, self.btn_isolate):
            b.setEnabled(has)
        loaded = self.model.rowCount() > 0
        self.btn_clear_iso.setEnabled(loaded)
        self.btn_show_all.setEnabled(loaded)
