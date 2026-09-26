"""Project / board tree: file facts, statistics, load notes and component list."""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLineEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from pcbrouter.domain.units import format_mm, internal_to_mm
from pcbrouter.project.manager import ProjectSession
from pcbrouter.ui.pcb_canvas import RenderStats

_ROLE_ID = Qt.ItemDataRole.UserRole


def _row(parent: QTreeWidgetItem, label: str, value: str | None) -> QTreeWidgetItem:
    item = QTreeWidgetItem([label, value if value is not None else "unknown"])
    parent.addChild(item)
    return item


class ProjectPanel(QWidget):
    componentActivated = Signal(str)  # footprint/component id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Item", "Value"])
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.itemClicked.connect(self._on_clicked)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter components…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.filter)
        layout.addWidget(self.tree, 1)
        self._components_root: QTreeWidgetItem | None = None
        self._root: QTreeWidgetItem | None = None
        self._geometry: QTreeWidgetItem | None = None
        self.set_session(None, None)

    def set_session(self, session: ProjectSession | None, render: RenderStats | None) -> None:
        self.tree.clear()
        self._components_root = None
        self._root = None
        self._geometry = None
        self.filter.clear()
        if session is None:
            self.tree.addTopLevelItem(QTreeWidgetItem(["No board loaded", "File ▸ Open Board"]))
            return
        board = session.board
        md = board.metadata
        stats = board.statistics
        load = session.load_result.stats

        root = QTreeWidgetItem([session.name, "read-only"])
        root.setToolTip(0, str(session.source_path))
        self.tree.addTopLevelItem(root)
        self._root = root

        info = QTreeWidgetItem(["Board", ""])
        root.addChild(info)
        _row(info, "File", str(session.source_path))
        _row(info, "Format version", str(md.format_version) if md.format_version else None)
        _row(info, "KiCad version (guess)", md.kicad_major_version_guess)
        _row(
            info,
            "Generator",
            " ".join(x for x in (md.generator, md.generator_version) if x) or None,
        )
        _row(info, "Title", md.title)
        _row(info, "Revision", md.revision)
        if stats.width is not None and stats.height is not None:
            size = f"{internal_to_mm(stats.width):.3f} × {internal_to_mm(stats.height):.3f} mm"
        else:
            size = None
        _row(info, "Outline size", size)
        _row(
            info,
            "Thickness",
            format_mm(board.rules.board_thickness) if board.rules.board_thickness else None,
        )
        _row(info, "Copper layers", ", ".join(board.copper_layer_names) or "none")

        st = QTreeWidgetItem(["Statistics", ""])
        root.addChild(st)
        _row(st, "Components", str(stats.footprint_count))
        _row(st, "Pads", str(stats.pad_count))
        _row(st, "Nets", str(stats.net_count))
        _row(st, "Tracks", f"{stats.track_count} ({stats.arc_track_count} arcs)")
        _row(st, "Vias", str(stats.via_count))
        _row(st, "Total track length", format_mm(stats.total_track_length))
        _row(st, "File size", f"{load.file_size_bytes / 1024:.1f} KiB")
        _row(st, "Parse time", f"{load.parse_seconds * 1e3:.1f} ms")
        _row(st, "Model build time", f"{load.build_seconds * 1e3:.1f} ms")
        if render is not None:
            _row(st, "Render items", str(render.item_count))
            _row(st, "Scene build time", f"{render.build_seconds * 1e3:.1f} ms")
        _row(st, "SHA-256", load.sha256[:16] + "…").setToolTip(1, load.sha256)

        warnings = session.load_result.warnings
        notes = QTreeWidgetItem(["Load notes", str(len(warnings))])
        root.addChild(notes)
        for w in warnings:
            item = QTreeWidgetItem(
                [w.severity.value, w.message + (f" (line {w.line})" if w.line else "")]
            )
            item.setToolTip(1, item.text(1))
            notes.addChild(item)

        ws = QTreeWidgetItem(["Workspace", "created on demand"])
        ws.setToolTip(
            0,
            "Application data for this board (router overrides, saved AI sessions). "
            "Nothing is ever written next to or into the KiCad files.",
        )
        root.addChild(ws)
        _row(ws, "Location", str(session.workspace.root))
        _row(ws, "Snapshots", str(session.workspace.snapshots_dir))

        comps = QTreeWidgetItem(["Components", str(len(board.components))])
        root.addChild(comps)
        for comp in sorted(board.components, key=lambda c: _natural_key(c.reference)):
            item = QTreeWidgetItem([comp.reference, comp.value or ""])
            item.setData(0, _ROLE_ID, comp.id)
            item.setToolTip(0, f"{comp.footprint.lib_id} — click to select on the board")
            comps.addChild(item)
        self._components_root = comps

        root.setExpanded(True)
        info.setExpanded(True)
        st.setExpanded(True)
        self.tree.resizeColumnToContents(0)

    def set_geometry_stats(self, rows: list[tuple[str, str]]) -> None:
        """Stage 3 statistics (engine facts: connectivity, internal DRC, index)."""
        if self._root is None:
            return
        if self._geometry is None:
            self._geometry = QTreeWidgetItem(["Geometry & Rules", "Stage 3 engine"])
            self._root.insertChild(2, self._geometry)
        self._geometry.takeChildren()
        for label, value in rows:
            _row(self._geometry, label, value)
        self._geometry.setExpanded(True)

    def geometry_stats(self) -> dict[str, str]:
        g = self._geometry
        if g is None:
            return {}
        out: dict[str, str] = {}
        for i in range(g.childCount()):
            child = g.child(i)
            if child is not None:
                out[child.text(0)] = child.text(1)
        return out

    def _on_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        comp_id = item.data(0, _ROLE_ID)
        if comp_id:
            self.componentActivated.emit(str(comp_id))

    def _apply_filter(self, text: str) -> None:
        root = self._components_root
        if root is None:
            return
        needle = text.casefold()
        for i in range(root.childCount()):
            child = root.child(i)
            if child is None:
                continue
            haystack = f"{child.text(0)} {child.text(1)}".casefold()
            child.setHidden(bool(needle) and needle not in haystack)
        if needle:
            root.setExpanded(True)


def _natural_key(text: str) -> tuple[object, ...]:
    """Sort R2 before R10. ``re.split`` with a capture group alternates str/int, so
    tuples always compare like-typed elements."""
    return tuple(int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", text))
