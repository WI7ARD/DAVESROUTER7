"""Property inspector for the selected object.

Property extraction is kept in plain functions (``*_properties``) returning
``(label, value | None)`` rows so it can be unit-tested without widgets. ``None``
is rendered as *unknown* — values are never invented.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLabel, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from pcbrouter.domain.board import Board
from pcbrouter.domain.component import Component
from pcbrouter.domain.geometry import Point
from pcbrouter.domain.pad import Pad, PadShape
from pcbrouter.domain.track import Track
from pcbrouter.domain.units import format_mm, internal_to_mm
from pcbrouter.domain.via import Via
from pcbrouter.ui.pcb_canvas import ItemKind

Rows = list[tuple[str, str | None]]
UNKNOWN = "unknown"
_APPROXIMATED_SHAPES = (PadShape.TRAPEZOID, PadShape.CUSTOM, PadShape.UNKNOWN)


def fmt_point(p: Point) -> str:
    return f"({internal_to_mm(p.x):.4f}, {internal_to_mm(p.y):.4f}) mm"


def fmt_angle(deg: float) -> str:
    return f"{deg:g}°"


def component_properties(comp: Component) -> Rows:
    fp = comp.footprint
    rows: Rows = [
        ("Reference", comp.reference),
        ("Value", comp.value),
        ("Footprint", fp.lib_id or None),
        ("Position", fmt_point(fp.position)),
        ("Rotation", fmt_angle(fp.rotation_deg)),
        ("Side", fp.side.value.capitalize()),
        ("Pads", str(len(fp.pads))),
        ("Locked", "yes" if fp.locked else "no"),
        ("Attributes", ", ".join(fp.attributes) if fp.attributes else None),
    ]
    rows.extend((f"Property: {k}", v or None) for k, v in sorted(comp.properties.items()))
    return rows


def pad_properties(pad: Pad) -> Rows:
    shape = pad.shape.value
    if pad.shape in _APPROXIMATED_SHAPES:
        shape += " (drawn as rectangle)"
    return [
        ("Number", pad.number or "(unnumbered)"),
        ("Component", pad.footprint_ref),
        ("Net", pad.net_name if pad.net_name is not None else "<no net>"),
        ("Position", fmt_point(pad.position)),
        ("Size", f"{format_mm(pad.size[0])} × {format_mm(pad.size[1])}"),
        ("Shape", shape),
        ("Type", pad.pad_type.value),
        ("Rotation", fmt_angle(pad.rotation_deg)),
        (
            "Drill",
            format_mm(pad.drill) if pad.drill else ("none" if not pad.is_through_hole else None),
        ),
        ("Copper layers", ", ".join(pad.copper_layers) or "none"),
        ("All layers", ", ".join(pad.layers) or None),
    ]


def track_properties(track: Track) -> Rows:
    rows: Rows = [
        ("Net", track.net_name if track.net_name is not None else "<no net>"),
        ("Layer", track.layer),
        ("Width", format_mm(track.width)),
        ("Type", "Arc" if track.is_arc else "Straight segment"),
        ("Start", fmt_point(track.start)),
        ("End", fmt_point(track.end)),
    ]
    if track.mid is not None:
        rows.append(("Mid", fmt_point(track.mid)))
    rows += [("Length", format_mm(track.length)), ("Locked", "yes" if track.locked else "no")]
    return rows


def via_properties(via: Via) -> Rows:
    return [
        ("Net", via.net_name if via.net_name is not None else "<no net>"),
        ("Position", fmt_point(via.position)),
        ("Diameter", format_mm(via.diameter)),
        ("Drill", format_mm(via.drill) if via.drill is not None else None),
        ("Start layer", via.start_layer),
        ("End layer", via.end_layer),
        ("Type", via.via_type.value.replace("_", "/")),
        ("Locked", "yes" if via.locked else "no"),
    ]


def net_properties(board: Board, name: str) -> Rows:
    idx = board.index
    net = idx.nets_by_name.get(name)
    stats = idx.net_statistics.get(name)
    refs = sorted({p.footprint_ref for p in idx.pads_by_net.get(name, [])})
    return [
        ("Name", name),
        ("Number / ID", str(net.code) if net is not None and net.code is not None else None),
        ("Pads", str(stats.pad_count) if stats else "0"),
        ("Tracks", str(stats.track_count) if stats else "0"),
        ("Vias", str(stats.via_count) if stats else "0"),
        ("Routed length", format_mm(stats.routed_length) if stats else None),
        ("Components", ", ".join(refs) if refs else "none"),
    ]


def properties_for(board: Board, kind: ItemKind, obj_id: str) -> tuple[str, Rows] | None:
    idx = board.index
    if kind is ItemKind.COMPONENT and obj_id in idx.components_by_id:
        comp = idx.components_by_id[obj_id]
        return f"Component {comp.reference}", component_properties(comp)
    if kind is ItemKind.PAD and obj_id in idx.pads_by_id:
        pad = idx.pads_by_id[obj_id]
        return f"Pad {pad.footprint_ref}.{pad.number}", pad_properties(pad)
    if kind is ItemKind.TRACK and obj_id in idx.tracks_by_id:
        return "Track", track_properties(idx.tracks_by_id[obj_id])
    if kind is ItemKind.VIA and obj_id in idx.vias_by_id:
        return "Via", via_properties(idx.vias_by_id[obj_id])
    if kind is ItemKind.NET and obj_id in idx.nets_by_name:
        return f"Net {obj_id}", net_properties(board, obj_id)
    return None


class InspectorPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = QLabel("Nothing selected")
        bold = QFont(self._title.font())
        bold.setBold(True)
        self._title.setFont(bold)
        self._tree = QTreeWidget()
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels(["Property", "Value"])
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setColumnWidth(0, 120)
        self._tree.setToolTip(
            "Properties of the selected object. 'unknown' means the board "
            "file did not state the value."
        )
        self._hint = QLabel("Click an object on the board, or a net/component in a list.")
        self._hint.setProperty("role", "muted")
        self._hint.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self._title)
        layout.addWidget(self._tree, 1)
        layout.addWidget(self._hint)

    def show_rows(self, title: str, rows: Rows) -> None:
        self._title.setText(title)
        self._tree.clear()
        muted = self.palette().placeholderText()
        for label, value in rows:
            item = QTreeWidgetItem([label, value if value is not None else UNKNOWN])
            item.setToolTip(1, value if value is not None else "Not stated in the board file")
            if value is None:
                font = item.font(1)
                font.setItalic(True)
                item.setFont(1, font)
                item.setForeground(1, muted)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._tree.addTopLevelItem(item)
        self._tree.resizeColumnToContents(0)

    def show_object(self, board: Board, kind: ItemKind, obj_id: str) -> None:
        found = properties_for(board, kind, obj_id)
        if found is None:
            self.clear()
            return
        self.show_rows(*found)

    def add_rows(self, section: str, rows: Rows) -> None:
        """Append a titled block (e.g. the Stage 8 Route Inspector facts)."""
        head = QTreeWidgetItem([section, ""])
        font = head.font(0)
        font.setBold(True)
        head.setFont(0, font)
        head.setFlags(head.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._tree.addTopLevelItem(head)
        for label, value in rows:
            item = QTreeWidgetItem([label, value if value is not None else UNKNOWN])
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            head.addChild(item)
        head.setExpanded(True)

    def row_values(self) -> dict[str, str]:
        """Displayed rows as a dict (used by tests and diagnostics)."""
        out = {}
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            if item is not None:
                out[item.text(0)] = item.text(1)
        return out

    @property
    def title(self) -> str:
        return self._title.text()

    def clear(self) -> None:
        self._title.setText("Nothing selected")
        self._tree.clear()
