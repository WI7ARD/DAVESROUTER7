"""Display-only engineering overlays on the PCB canvas (Stage 3 spec §54-§57).

Everything here is *drawing*: overlay items live in the canvas scene next to the
board items but are never board data, are never selectable, and vanish with the
next board load. Overlays are grouped by name so each can be toggled on its own:

* ``drc`` — markers for Internal Geometry Check violations;
* ``candidate`` — a hypothetical test segment/via, coloured by validation status;
* ``envelope`` — the clearance envelope of the selected track/pad/via;
* ``debug:<name>`` — board boundary, keepouts, raw obstacle bounds, inflated
  obstacles, routing grid, airwires, congestion.

Shapes are drawn from the engine's own core+radius shapes (outline polygons are a
display approximation; the engine never uses them for decisions).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import numpy as np
from PySide6.QtCore import QLineF, QObject, QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
)

from pcbrouter.domain.geometry import BoundingBox, Point
from pcbrouter.domain.units import internal_to_mm
from pcbrouter.drc.violation import DRCViolation, Severity
from pcbrouter.geometry.shapes import Shape, outline_points
from pcbrouter.routing.collision import ValidationStatus
from pcbrouter.routing.congestion import CongestionMap
from pcbrouter.routing.connectivity import Airwire
from pcbrouter.routing.occupancy import OccupancyMap
from pcbrouter.ui import theme
from pcbrouter.ui.pcb_canvas import PcbCanvas

Z_OVERLAY_BASE = 900.0  # above copper/labels, below the selection overlay (1000)
MARKER_RADIUS_MM = 0.35

DEBUG_OVERLAYS: dict[str, str] = {
    "boundary": "Board Boundary",
    "keepouts": "Keepouts",
    "raw_bounds": "Raw Obstacle Bounds",
    "inflated": "Inflated Obstacles",
    "grid": "Routing Grid (blocked cells)",
    "airwires": "Airwires (unrouted connections)",
    "congestion": "Congestion Estimate",
}


def _qp(p: Point) -> QPointF:
    return QPointF(internal_to_mm(p.x), internal_to_mm(p.y))


def _pen(color: QColor, width_px: float = 1.5, style: Qt.PenStyle = Qt.PenStyle.SolidLine) -> QPen:
    pen = QPen(color, width_px)
    pen.setCosmetic(True)  # constant on-screen width at every zoom
    pen.setStyle(style)
    return pen


def _alpha(color: QColor, alpha: int) -> QColor:
    c = QColor(color)
    c.setAlpha(alpha)
    return c


def shape_path(shapes: Iterable[Shape]) -> QPainterPath:
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.WindingFill)
    for shape in shapes:
        pts = outline_points(shape)
        if len(pts) >= 3:
            path.addPolygon(QPolygonF([_qp(p) for p in pts]))
            path.closeSubpath()
        elif len(pts) == 2:
            path.moveTo(_qp(pts[0]))
            path.lineTo(_qp(pts[1]))
    return path


def rect_mm(box: BoundingBox) -> QRectF:
    return QRectF(
        internal_to_mm(box.min_x),
        internal_to_mm(box.min_y),
        internal_to_mm(box.width),
        internal_to_mm(box.height),
    )


def _path_item(path: QPainterPath, pen: QPen, brush: QColor | None, z: float) -> QGraphicsPathItem:
    item = QGraphicsPathItem(path)
    item.setPen(pen)
    item.setBrush(QBrush(brush) if brush is not None else QBrush(Qt.BrushStyle.NoBrush))
    item.setZValue(z)
    return item


def severity_color(severity: Severity) -> QColor:
    return {
        Severity.ERROR: theme.DRC_ERROR_COLOR,
        Severity.WARNING: theme.DRC_WARNING_COLOR,
        Severity.INFO: theme.DRC_INFO_COLOR,
    }[severity]


def status_color(status: ValidationStatus) -> QColor:
    if status is ValidationStatus.INVALID:
        return theme.CANDIDATE_INVALID_COLOR
    if status is ValidationStatus.RULE_UNKNOWN:
        return theme.CANDIDATE_UNKNOWN_COLOR
    return theme.CANDIDATE_VALID_COLOR


# ---------------------------------------------------------------- item builders
def violation_items(
    violations: Sequence[DRCViolation], object_shapes: dict[str, tuple[Shape, ...]]
) -> list[QGraphicsItem]:
    """A ring + cross at each located violation and an outline of the objects."""
    items: list[QGraphicsItem] = []
    for v in violations:
        color = severity_color(v.severity)
        involved = [
            s for uid in (v.object_a, v.object_b) if uid for s in object_shapes.get(uid, ())
        ]
        if involved:
            items.append(_path_item(shape_path(involved), _pen(color, 1.0), None, Z_OVERLAY_BASE))
        if v.location is None:
            continue
        c = _qp(v.location)
        r = MARKER_RADIUS_MM
        path = QPainterPath()
        path.addEllipse(c, r, r)
        path.moveTo(c.x() - r * 1.6, c.y())
        path.lineTo(c.x() + r * 1.6, c.y())
        path.moveTo(c.x(), c.y() - r * 1.6)
        path.lineTo(c.x(), c.y() + r * 1.6)
        marker = _path_item(path, _pen(color, 2.0), None, Z_OVERLAY_BASE + 5)
        marker.setToolTip(f"{v.severity.value.upper()}: {v.message}")
        items.append(marker)
    return items


def candidate_items(shapes: Sequence[Shape], status: ValidationStatus) -> list[QGraphicsItem]:
    color = status_color(status)
    item = _path_item(
        shape_path(shapes), _pen(color, 2.0, Qt.PenStyle.DashLine), _alpha(color, 70),
        Z_OVERLAY_BASE + 10,
    )  # fmt: skip
    item.setToolTip(f"Hypothetical geometry (not on the board): {status.value}")
    return [item]


def envelope_items(raw: Sequence[Shape], envelope: Sequence[Shape]) -> list[QGraphicsItem]:
    color = theme.ENVELOPE_COLOR
    return [
        _path_item(shape_path(envelope), _pen(color, 1.5, Qt.PenStyle.DashLine),
                   _alpha(color, 45), Z_OVERLAY_BASE + 2),
        _path_item(shape_path(raw), _pen(color, 1.0), None, Z_OVERLAY_BASE + 3),
    ]  # fmt: skip


def outline_items(
    shapes: Sequence[Shape], color: QColor, fill_alpha: int = 0, dashed: bool = False
) -> list[QGraphicsItem]:
    if not shapes:
        return []
    style = Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine
    brush = _alpha(color, fill_alpha) if fill_alpha else None
    return [_path_item(shape_path(shapes), _pen(color, 1.2, style), brush, Z_OVERLAY_BASE)]


def bounds_items(boxes: Iterable[BoundingBox], color: QColor) -> list[QGraphicsItem]:
    items: list[QGraphicsItem] = []
    pen = _pen(color, 1.0, Qt.PenStyle.DotLine)
    for box in boxes:
        r = QGraphicsRectItem(rect_mm(box))
        r.setPen(pen)
        r.setZValue(Z_OVERLAY_BASE)
        items.append(r)
    return items


def polygon_items(loops: Iterable[Sequence[Point]], color: QColor) -> list[QGraphicsItem]:
    path = QPainterPath()
    for pts in loops:
        if len(pts) >= 3:
            path.addPolygon(QPolygonF([_qp(p) for p in pts]))
            path.closeSubpath()
    return [_path_item(path, _pen(color, 2.0), None, Z_OVERLAY_BASE)]


def airwire_items(airwires: Iterable[Airwire]) -> list[QGraphicsItem]:
    items: list[QGraphicsItem] = []
    pen = _pen(theme.AIRWIRE_COLOR, 1.0, Qt.PenStyle.DashLine)
    for a in airwires:
        line = QGraphicsLineItem(QLineF(_qp(a.start), _qp(a.end)))
        line.setPen(pen)
        line.setZValue(Z_OVERLAY_BASE)
        line.setToolTip(f"{a.net}: unrouted connection ({internal_to_mm(round(a.length)):.3f} mm)")
        items.append(line)
    return items


def _image_item(rgba: np.ndarray, origin: Point, cell_nm: int, tip: str) -> QGraphicsPixmapItem:
    h, w = rgba.shape[:2]
    buf = np.ascontiguousarray(rgba)
    image = QImage(buf.data, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
    item = QGraphicsPixmapItem(QPixmap.fromImage(image))
    item.setTransformationMode(Qt.TransformationMode.FastTransformation)  # crisp cells
    item.setPos(_qp(origin))
    item.setScale(internal_to_mm(cell_nm))
    item.setZValue(Z_OVERLAY_BASE - 50)
    item.setToolTip(tip)
    return item


def occupancy_item(occ: OccupancyMap) -> QGraphicsPixmapItem:
    lut = np.array(theme.GRID_CELL_RGBA, dtype=np.uint8)
    rgba = lut[np.clip(occ.cells, 0, len(lut) - 1)]
    s = occ.spec
    return _image_item(
        rgba, Point(s.origin_x, s.origin_y), s.cell,
        f"Routing grid {s.layer} · {internal_to_mm(s.cell):g} mm cells · net "
        f"{occ.net or '(none)'} · width {internal_to_mm(occ.width):g} mm · "
        f"{occ.free_fraction:.0%} free",
    )  # fmt: skip


def congestion_item(cmap: CongestionMap) -> QGraphicsPixmapItem:
    v = np.clip(cmap.values, 0.0, 1.0)
    rgba = np.zeros((*v.shape, 4), dtype=np.uint8)
    rgba[..., 0] = (255 * v).astype(np.uint8)
    rgba[..., 1] = (200 * (1.0 - v)).astype(np.uint8)
    rgba[..., 2] = 40
    rgba[..., 3] = (40 + 120 * v).astype(np.uint8)
    return _image_item(
        rgba, Point(cmap.origin_x, cmap.origin_y), cmap.tile,
        f"{cmap.layer}: {cmap.label} (not a guarantee of routing difficulty)",
    )  # fmt: skip


# ---------------------------------------------------------------- manager
class OverlayManager(QObject):
    """Owns named groups of overlay items in the canvas scene."""

    def __init__(self, canvas: PcbCanvas) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self._groups: dict[str, list[QGraphicsItem]] = {}
        canvas.aboutToClear.connect(self._forget)

    @property
    def names(self) -> list[str]:
        return sorted(self._groups)

    def items(self, name: str) -> list[QGraphicsItem]:
        return list(self._groups.get(name, ()))

    def has(self, name: str) -> bool:
        return bool(self._groups.get(name))

    def set_group(self, name: str, items: list[QGraphicsItem]) -> None:
        self.clear(name)
        scene = self.canvas.overlay_scene
        for item in items:
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
            item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            scene.addItem(item)
        self._groups[name] = items

    def clear(self, name: str) -> None:
        scene = self.canvas.overlay_scene
        for item in self._groups.pop(name, []):
            if item.scene() is scene:
                scene.removeItem(item)

    def clear_all(self, prefix: str = "") -> None:
        for name in [n for n in self._groups if n.startswith(prefix)]:
            self.clear(name)

    def _forget(self) -> None:
        # The scene is about to delete every item; remove ours first so no Python
        # wrapper outlives its C++ object.
        self.clear_all()
