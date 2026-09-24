"""Layer visibility and active-layer selection."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import QLabel, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from pcbrouter.domain.board import Board
from pcbrouter.domain.layer import EDGE_CUTS, LayerKind
from pcbrouter.ui import theme

_ROLE_LAYER = Qt.ItemDataRole.UserRole
_ROLE_KIND = Qt.ItemDataRole.UserRole + 1
_KIND_LAYER = "layer"
_KIND_FOOTPRINTS = "footprints"
_KIND_LABELS = "labels"

_KIND_TEXT = {
    LayerKind.FRONT_COPPER: "front copper",
    LayerKind.INNER_COPPER: "inner copper",
    LayerKind.BACK_COPPER: "back copper",
    LayerKind.BOARD_OUTLINE: "board outline",
}


def _swatch(color: QColor) -> QIcon:
    pm = QPixmap(12, 12)
    pm.fill(color)
    return QIcon(pm)


class LayersPanel(QWidget):
    layerVisibilityChanged = Signal(str, bool)
    activeLayerChanged = Signal(str)
    footprintsVisibilityChanged = Signal(bool)
    labelsVisibilityChanged = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Layer", "Type"])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemClicked.connect(self._on_item_clicked)
        self.active_label = QLabel("Active layer: —")
        self.active_label.setProperty("role", "muted")
        hint = QLabel("Tick to show/hide. Click a copper layer to make it active (drawn on top).")
        hint.setProperty("role", "muted")
        hint.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.active_label)
        layout.addWidget(hint)
        self._updating = False
        self._items: dict[str, QTreeWidgetItem] = {}
        self._overlays: dict[str, QTreeWidgetItem] = {}  # "footprints" / "labels"

    def set_board(
        self,
        board: Board | None,
        active_layer: str | None = None,
        *,
        show_footprints: bool = True,
        show_labels: bool = True,
    ) -> None:
        self._updating = True
        self.tree.clear()
        self._items.clear()
        self._overlays.clear()
        if board is not None:
            copper = QTreeWidgetItem(["Copper", ""])
            copper.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tree.addTopLevelItem(copper)
            for layer in board.copper_layers:
                item = QTreeWidgetItem(
                    [layer.display_name, f"{_KIND_TEXT[layer.kind]} · {layer.copper_type.value}"]
                )
                self._make_checkable(item, _KIND_LAYER, layer.name)
                item.setIcon(0, _swatch(theme.layer_color(layer.name)))
                item.setToolTip(0, f"{layer.name} ({_KIND_TEXT[layer.kind]})")
                copper.addChild(item)
                self._items[layer.name] = item
            board_group = QTreeWidgetItem(["Board", ""])
            board_group.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tree.addTopLevelItem(board_group)
            if board.layer(EDGE_CUTS) is not None or not board.outline.is_empty:
                edge = QTreeWidgetItem([EDGE_CUTS, _KIND_TEXT[LayerKind.BOARD_OUTLINE]])
                self._make_checkable(edge, _KIND_LAYER, EDGE_CUTS)
                edge.setIcon(0, _swatch(theme.OUTLINE_COLOR))
                board_group.addChild(edge)
                self._items[EDGE_CUTS] = edge
            display = QTreeWidgetItem(["Display", ""])
            display.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tree.addTopLevelItem(display)
            fps = QTreeWidgetItem(["Footprint bodies", "overlay"])
            self._make_checkable(fps, _KIND_FOOTPRINTS, "")
            labels = QTreeWidgetItem(["Reference labels", "overlay"])
            labels.setToolTip(0, "Labels appear once zoomed in far enough to be legible")
            self._make_checkable(labels, _KIND_LABELS, "")
            fps.setCheckState(
                0, Qt.CheckState.Checked if show_footprints else Qt.CheckState.Unchecked
            )
            labels.setCheckState(
                0, Qt.CheckState.Checked if show_labels else Qt.CheckState.Unchecked
            )
            display.addChildren([fps, labels])
            self._overlays = {_KIND_FOOTPRINTS: fps, _KIND_LABELS: labels}
            self.tree.expandAll()
            self.tree.resizeColumnToContents(0)
        self._updating = False
        self.set_active_layer(active_layer)

    def set_active_layer(self, layer: str | None) -> None:
        self.active_label.setText(f"Active layer: {layer or '—'}")
        for name, item in self._items.items():
            font = item.font(0)
            font.setBold(name == layer)
            item.setFont(0, font)

    def is_checked(self, layer: str) -> bool:
        item = self._items.get(layer)
        return item is not None and item.checkState(0) == Qt.CheckState.Checked

    def is_overlay_checked(self, overlay: str) -> bool:
        """``overlay`` is ``"footprints"`` or ``"labels"``."""
        item = self._overlays.get(overlay)
        return item is not None and item.checkState(0) == Qt.CheckState.Checked

    def set_checked(self, layer: str, checked: bool) -> None:
        item = self._items.get(layer)
        if item is not None:
            item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def _make_checkable(self, item: QTreeWidgetItem, kind: str, layer: str) -> None:
        item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsSelectable
        )
        item.setCheckState(0, Qt.CheckState.Checked)
        item.setData(0, _ROLE_KIND, kind)
        item.setData(0, _ROLE_LAYER, layer)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._updating or column != 0:
            return
        checked = item.checkState(0) == Qt.CheckState.Checked
        kind = item.data(0, _ROLE_KIND)
        if kind == _KIND_LAYER:
            self.layerVisibilityChanged.emit(str(item.data(0, _ROLE_LAYER)), checked)
        elif kind == _KIND_FOOTPRINTS:
            self.footprintsVisibilityChanged.emit(checked)
        elif kind == _KIND_LABELS:
            self.labelsVisibilityChanged.emit(checked)

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        layer = item.data(0, _ROLE_LAYER)
        if item.data(0, _ROLE_KIND) == _KIND_LAYER and layer and layer.endswith(".Cu"):
            self.set_active_layer(str(layer))
            self.activeLayerChanged.emit(str(layer))
