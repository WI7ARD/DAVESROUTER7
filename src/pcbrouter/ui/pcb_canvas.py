"""2D board viewer built on QGraphicsView.

Performance model:

* The scene is built **once** per board (:meth:`PcbCanvas.set_board`). Nothing is
  rebuilt on mouse movement, zoom, pan, selection or visibility changes.
* Every graphics item is registered in a :class:`_Record` with its kind, object id,
  copper layers and net, and indexed by id and by net. Visibility, net isolation
  and highlighting walk these records and flip flags; they never recreate items.
* Hit-testing uses Qt's BSP-tree scene index.
* Reference labels use device-space text and are hidden below a zoom threshold
  (level of detail) to keep zoomed-out views fast and legible.

Scene units are **millimetres** (floats). The one conversion from internal
nanometres happens in :func:`_mm` — the display boundary.
"""

from __future__ import annotations

import logging
import math
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QEvent, QLineF, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QToolTip,
    QWidget,
)

from pcbrouter.domain.board import Board, OutlineShape
from pcbrouter.domain.footprint import BoardSide
from pcbrouter.domain.geometry import Point
from pcbrouter.domain.layer import BACK_COPPER, FRONT_COPPER, copper_stack_position
from pcbrouter.domain.pad import Pad, PadShape, PadType
from pcbrouter.domain.track import Track
from pcbrouter.domain.units import format_mm, internal_to_mm
from pcbrouter.domain.via import Via, ViaType
from pcbrouter.ui import theme

log = logging.getLogger(__name__)

MIN_PX_PER_MM = 0.2
MAX_PX_PER_MM = 5000.0
LABEL_MIN_PX_PER_MM = 6.0
GRID_MIN_PX = 9.0
WHEEL_ZOOM_BASE = 1.0015  # per 1/8 degree of wheel rotation: one notch ~ 1.2x


class ItemKind(Enum):
    COMPONENT = "component"
    PAD = "pad"
    TRACK = "track"
    VIA = "via"
    NET = "net"  # selection target only (no graphics item of its own)
    OUTLINE = "outline"
    LABEL = "label"


#: Click priority: smaller wins when several objects are under the cursor.
_PICK_PRIORITY = {ItemKind.VIA: 0, ItemKind.PAD: 1, ItemKind.TRACK: 2, ItemKind.COMPONENT: 3}

# Z-order bands.
_Z_SUBSTRATE = -10.0
_Z_FOOTPRINT = 1.0
_Z_COPPER_BASE = 10.0  # + position in stack (back copper lowest)
_Z_ACTIVE_BOOST = 40.0
_Z_THT_PAD = 70.0
_Z_VIA = 80.0
_Z_OUTLINE = 90.0
_Z_LABEL = 95.0
_Z_OVERLAY = 1000.0


def _mm(nm: int) -> float:
    return internal_to_mm(nm)


def _qpt(p: Point) -> QPointF:
    return QPointF(internal_to_mm(p.x), internal_to_mm(p.y))


class _DrilledItem(QGraphicsPathItem):
    """Pad/via item painted with its drill hole cut out, but hit-tested as solid
    copper, so clicking the centre of a through-hole pad or via selects it rather
    than whatever lies underneath the hole."""

    def __init__(self, painted: QPainterPath, solid: QPainterPath) -> None:
        super().__init__(painted)
        self._solid = solid

    def shape(self) -> QPainterPath:
        return self._solid


@dataclass(eq=False, slots=True)
class _Record:
    item: QGraphicsItem
    kind: ItemKind
    obj_id: str
    layers: tuple[str, ...]  # copper layers occupied (empty = layer-independent)
    net: str | None
    base_z: float


@dataclass(frozen=True, slots=True)
class RenderStats:
    item_count: int
    by_kind: dict[str, int]
    build_seconds: float


class PcbCanvas(QGraphicsView):
    """Read-only board viewer: pan, zoom, visibility, selection and hover."""

    objectSelected = Signal(object, str)  # (ItemKind, object id)
    selectionCleared = Signal()
    cursorMoved = Signal(float, float)  # scene position in mm
    zoomChanged = Signal(float)  # pixels per mm
    #: Emitted before every scene is cleared: owners of extra scene items (overlays)
    #: must drop their references, because ``QGraphicsScene.clear`` deletes them.
    aboutToClear = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self._scene.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.BspTreeIndex)
        self.setScene(self._scene)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setOptimizationFlags(
            QGraphicsView.OptimizationFlag.DontSavePainterState
            | QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing
        )
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setMouseTracking(True)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setBackgroundBrush(QBrush(theme.CANVAS_BACKGROUND))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Scrollbars are still used internally for panning; hiding them keeps the view clean.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._board: Board | None = None
        self._records: list[_Record] = []
        self._by_id: dict[tuple[ItemKind, str], list[_Record]] = {}
        self._by_net: dict[str, list[_Record]] = {}
        self._by_item: dict[int, _Record] = {}  # id(QGraphicsItem) -> record, for picking
        self._labels: list[QGraphicsItem] = []
        self._layer_visible: dict[str, bool] = {}
        self._show_footprints = True
        self._show_labels = True
        self._hidden_nets: set[str] = set()
        self._isolated_net: str | None = None
        self._highlight_net: str | None = None
        self._active_layer: str | None = None
        self._grid_visible = True
        self._grid_spacing_mm = 1.0
        self._stats = RenderStats(0, {}, 0.0)

        self._selection_overlay = self._make_overlay(theme.SELECTION_COLOR, 2.0)
        self._hover_overlay = self._make_overlay(theme.HOVER_COLOR, 1.0)

        self._panning = False
        self._space_down = False
        self._pan_last = QPointF()
        self._hover_key: tuple[ItemKind, str] | None = None

    # ================================================================ public API
    @property
    def board(self) -> Board | None:
        return self._board

    @property
    def render_stats(self) -> RenderStats:
        return self._stats

    @property
    def px_per_mm(self) -> float:
        return float(self.transform().m11())

    @property
    def active_layer(self) -> str | None:
        return self._active_layer

    @property
    def isolated_net(self) -> str | None:
        return self._isolated_net

    @property
    def hidden_nets(self) -> frozenset[str]:
        return frozenset(self._hidden_nets)

    @property
    def highlighted_net(self) -> str | None:
        return self._highlight_net

    def records_for(self, kind: ItemKind, obj_id: str) -> list[QGraphicsItem]:
        return [r.item for r in self._by_id.get((kind, obj_id), [])]

    def visible_item_count(self) -> int:
        return sum(1 for r in self._records if r.item.isVisible())

    def set_board(self, board: Board | None) -> None:
        self.clear_board()
        self._board = board
        if board is None:
            return
        start = time.perf_counter()
        self._layer_visible = {layer.name: True for layer in board.copper_layers}
        self._active_layer = (
            FRONT_COPPER
            if FRONT_COPPER in self._layer_visible
            else next(iter(self._layer_visible), None)
        )
        self._build(board)
        self._apply_z()
        self._apply_visibility()
        bounds = self._content_rect()
        margin = max(bounds.width(), bounds.height(), 10.0) * 3
        self._scene.setSceneRect(bounds.adjusted(-margin, -margin, margin, margin))
        counts = Counter(r.kind.value for r in self._records)
        self._stats = RenderStats(len(self._records), dict(counts), time.perf_counter() - start)
        log.info(
            "canvas.build items=%d ms=%.1f by_kind=%s",
            self._stats.item_count, self._stats.build_seconds * 1e3, dict(counts),
        )  # fmt: skip
        self.zoom_to_fit()

    @property
    def overlay_scene(self) -> QGraphicsScene:
        """The scene, for display-only overlays (see :mod:`pcbrouter.ui.overlays`)."""
        return self._scene

    def focus_on(self, rect_mm: QRectF, min_px_per_mm: float = 20.0) -> None:
        """Centre ``rect_mm`` and zoom in so it is at least ``min_px_per_mm``."""
        if self.px_per_mm < min_px_per_mm:
            self.zoom_by(min_px_per_mm / max(self.px_per_mm, 1e-9))
        view = self.viewport().rect()
        if rect_mm.width() > 0 and rect_mm.height() > 0:
            fit = min(view.width() / rect_mm.width(), view.height() / rect_mm.height()) * 0.5
            if fit < self.px_per_mm:
                self.zoom_by(fit / self.px_per_mm)
        self.centerOn(rect_mm.center())

    def clear_board(self) -> None:
        self.aboutToClear.emit()
        self._scene.removeItem(self._selection_overlay)
        self._scene.removeItem(self._hover_overlay)
        self._scene.clear()
        self._records.clear()
        self._by_id.clear()
        self._by_net.clear()
        self._by_item.clear()
        self._labels.clear()
        self._hidden_nets.clear()
        self._isolated_net = None
        self._highlight_net = None
        self._hover_key = None
        self._board = None
        self._stats = RenderStats(0, {}, 0.0)
        self._selection_overlay = self._make_overlay(theme.SELECTION_COLOR, 2.0)
        self._hover_overlay = self._make_overlay(theme.HOVER_COLOR, 1.0)
        self._scene.setSceneRect(QRectF(-100, -100, 200, 200))
        self.viewport().update()

    def zoom_to_fit(self) -> None:
        rect = self._content_rect()
        if rect.isEmpty():
            return
        pad = max(rect.width(), rect.height()) * 0.05
        self.fitInView(rect.adjusted(-pad, -pad, pad, pad), Qt.AspectRatioMode.KeepAspectRatio)
        self._after_zoom()

    def zoom_by(self, factor: float) -> None:
        target = min(max(self.px_per_mm * factor, MIN_PX_PER_MM), MAX_PX_PER_MM)
        applied = target / self.px_per_mm if self.px_per_mm else 1.0
        if abs(applied - 1.0) > 1e-9:
            self.scale(applied, applied)
            self._after_zoom()

    def set_grid(self, visible: bool, spacing_mm: float | None = None) -> None:
        self._grid_visible = visible
        if spacing_mm is not None and spacing_mm > 0:
            self._grid_spacing_mm = spacing_mm
        self.resetCachedContent()
        self.viewport().update()

    @property
    def grid_visible(self) -> bool:
        return self._grid_visible

    def set_layer_visible(self, layer: str, visible: bool) -> None:
        if layer in self._layer_visible:
            self._layer_visible[layer] = visible
        elif layer == "Edge.Cuts":
            for r in self._records:
                if r.kind is ItemKind.OUTLINE:
                    r.item.setVisible(visible)
            return
        self._apply_visibility()

    def is_layer_visible(self, layer: str) -> bool:
        return self._layer_visible.get(layer, True)

    def set_footprints_visible(self, visible: bool) -> None:
        self._show_footprints = visible
        self._apply_visibility()

    def set_labels_visible(self, visible: bool) -> None:
        self._show_labels = visible
        self._update_label_lod()

    def set_active_layer(self, layer: str) -> None:
        if layer in self._layer_visible:
            self._active_layer = layer
            self._apply_z()

    def hide_net(self, net: str) -> None:
        self._hidden_nets.add(net)
        self._apply_visibility()

    def show_net(self, net: str) -> None:
        self._hidden_nets.discard(net)
        self._apply_visibility()

    def show_all_nets(self) -> None:
        self._hidden_nets.clear()
        self._isolated_net = None
        self._apply_visibility()

    def isolate_net(self, net: str | None) -> None:
        self._isolated_net = net
        self._apply_visibility()

    def highlight_net(self, net: str | None) -> None:
        """Dim everything except ``net`` (``None`` clears the highlight)."""
        self._highlight_net = net
        for r in self._records:
            if r.kind in (ItemKind.OUTLINE, ItemKind.LABEL):
                continue
            dim = net is not None and r.net != net
            r.item.setOpacity(0.18 if dim else 1.0)
        if net is not None:
            self._set_overlay(self._selection_overlay, self._by_net.get(net, []))
        else:
            self._selection_overlay.setPath(QPainterPath())

    def select_object(self, kind: ItemKind, obj_id: str, *, center: bool = False) -> bool:
        """Highlight one object. Returns False if it is not on the canvas."""
        if kind is ItemKind.NET:
            self.highlight_net(obj_id)
            return obj_id in self._by_net
        records = self._by_id.get((kind, obj_id), [])
        if self._highlight_net is not None:
            self.highlight_net(None)
        self._set_overlay(self._selection_overlay, records)
        if center and records:
            self.centerOn(records[0].item.sceneBoundingRect().center())
        return bool(records)

    def clear_selection(self) -> None:
        self._selection_overlay.setPath(QPainterPath())
        if self._highlight_net is not None:
            self.highlight_net(None)

    # ================================================================ building
    def _add(
        self,
        item: QGraphicsItem,
        kind: ItemKind,
        obj_id: str,
        layers: tuple[str, ...],
        net: str | None,
        z: float,
    ) -> None:
        item.setZValue(z)
        self._scene.addItem(item)
        rec = _Record(item, kind, obj_id, layers, net, z)
        self._records.append(rec)
        self._by_item[id(item)] = rec
        self._by_id.setdefault((kind, obj_id), []).append(rec)
        if net is not None:
            self._by_net.setdefault(net, []).append(rec)

    def _build(self, board: Board) -> None:
        self._build_outline(board)
        for comp in board.components:
            fp = comp.footprint
            outline = fp.outline()
            if outline:
                poly = QGraphicsPolygonItem(QPolygonF([_qpt(p) for p in outline]))
                pen = QPen(theme.FOOTPRINT_OUTLINE, 1.0)
                pen.setCosmetic(True)
                if fp.side is BoardSide.BACK:
                    pen.setStyle(Qt.PenStyle.DashLine)
                poly.setPen(pen)
                fill = QColor(theme.FOOTPRINT_OUTLINE)
                fill.setAlpha(18)
                poly.setBrush(QBrush(fill))
                side_layer = BACK_COPPER if fp.side is BoardSide.BACK else FRONT_COPPER
                self._add(poly, ItemKind.COMPONENT, fp.id, (side_layer,), None, _Z_FOOTPRINT)
            for pad in fp.pads:
                self._build_pad(pad, board)
            label = QGraphicsSimpleTextItem(comp.reference)
            label.setBrush(QBrush(theme.LABEL_COLOR))
            font = QFont()
            font.setPointSizeF(8.0)
            label.setFont(font)
            label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            br = label.boundingRect()
            label.setTransform(QTransform.fromTranslate(-br.width() / 2, -br.height() / 2))
            label.setPos(_qpt(fp.position))
            label.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self._add(label, ItemKind.LABEL, fp.id, (), None, _Z_LABEL)
            self._labels.append(label)
        for track in board.tracks:
            self._build_track(track)
        copper_order = board.copper_layer_names
        for via in board.vias:
            self._build_via(via, copper_order)

    def _build_outline(self, board: Board) -> None:
        if board.outline.is_empty:
            return
        loops, _ = board.outline.closed_loops()
        if loops:
            fill_path = QPainterPath()
            fill_path.setFillRule(Qt.FillRule.OddEvenFill)
            for loop in loops:
                fill_path.addPolygon(QPolygonF([_qpt(p) for p in loop]))
                fill_path.closeSubpath()
            substrate = QGraphicsPathItem(fill_path)
            substrate.setPen(QPen(Qt.PenStyle.NoPen))
            substrate.setBrush(QBrush(theme.BOARD_SUBSTRATE))
            substrate.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            substrate.setZValue(_Z_SUBSTRATE)
            self._scene.addItem(substrate)
        path = QPainterPath()
        for seg in board.outline.segments:
            if seg.shape is OutlineShape.CIRCLE:
                r = _mm(round(seg.start.distance_to(seg.end)))
                path.addEllipse(_qpt(seg.start), r, r)
                continue
            pts = seg.points()
            path.moveTo(_qpt(pts[0]))
            for p in pts[1:]:
                path.lineTo(_qpt(p))
        item = QGraphicsPathItem(path)
        pen = QPen(theme.OUTLINE_COLOR, 1.5)
        pen.setCosmetic(True)
        item.setPen(pen)
        item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._add(item, ItemKind.OUTLINE, "board-outline", (), None, _Z_OUTLINE)

    def _build_pad(self, pad: Pad, board: Board) -> None:
        w, h = _mm(pad.size[0]), _mm(pad.size[1])
        rect = QRectF(-w / 2, -h / 2, w, h)
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.OddEvenFill)
        if pad.shape is PadShape.CIRCLE:
            path.addEllipse(rect)
        elif pad.shape is PadShape.OVAL:
            r = min(w, h) / 2
            path.addRoundedRect(rect, r, r)
        elif pad.shape is PadShape.ROUNDRECT:
            r = min(w, h) * (pad.roundrect_ratio if pad.roundrect_ratio is not None else 0.25)
            path.addRoundedRect(rect, r, r)
        else:  # rect, and approximations for trapezoid/custom/unknown
            path.addRect(rect)
        solid = QPainterPath(path)
        if pad.drill:
            d = _mm(pad.drill)
            path.addEllipse(QPointF(0, 0), d / 2, d / 2)
        item = _DrilledItem(path, solid)
        item.setPos(_qpt(pad.position))
        item.setRotation(-pad.rotation_deg)  # KiCad CCW-positive -> Qt CW-positive
        copper = pad.copper_layers
        if pad.pad_type is PadType.NPTH:
            pen = QPen(theme.NPTH_COLOR, 1.0)
            pen.setCosmetic(True)
            item.setPen(pen)
            item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            z = _Z_THT_PAD
        elif len(copper) > 1:
            item.setPen(QPen(Qt.PenStyle.NoPen))
            item.setBrush(QBrush(theme.THT_PAD_COLOR))
            z = _Z_THT_PAD
        else:
            layer = copper[0] if copper else FRONT_COPPER
            item.setPen(QPen(Qt.PenStyle.NoPen))
            item.setBrush(QBrush(theme.copper_color(layer, 235).lighter(115)))
            z = self._copper_z(layer) + 0.5
        layers = copper if copper else tuple(board.copper_layer_names)
        self._add(item, ItemKind.PAD, pad.id, layers, pad.net_name, z)

    def _build_track(self, track: Track) -> None:
        path = QPainterPath()
        pts = track.centerline()
        path.moveTo(_qpt(pts[0]))
        for p in pts[1:]:
            path.lineTo(_qpt(p))
        item = QGraphicsPathItem(path)
        pen = QPen(theme.copper_color(track.layer), max(_mm(track.width), 1e-4))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        item.setPen(pen)
        item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self._add(
            item,
            ItemKind.TRACK,
            track.id,
            (track.layer,),
            track.net_name,
            self._copper_z(track.layer),
        )

    def _build_via(self, via: Via, copper_order: list[str]) -> None:
        r = _mm(via.diameter) / 2
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.OddEvenFill)
        path.addEllipse(QPointF(0, 0), r, r)
        solid = QPainterPath(path)
        if via.drill:
            path.addEllipse(QPointF(0, 0), _mm(via.drill) / 2, _mm(via.drill) / 2)
        item = _DrilledItem(path, solid)
        item.setPos(_qpt(via.position))
        color = {
            ViaType.THROUGH: theme.VIA_COLOR,
            ViaType.BLIND_BURIED: theme.BLIND_VIA_COLOR,
            ViaType.MICRO: theme.MICRO_VIA_COLOR,
        }[via.via_type]
        item.setBrush(QBrush(color))
        item.setPen(QPen(Qt.PenStyle.NoPen))
        layers = tuple(lyr for lyr in copper_order if via.spans_layer(lyr, copper_order))
        self._add(item, ItemKind.VIA, via.id, layers, via.net_name, _Z_VIA)

    def _make_overlay(self, color: QColor, width: float) -> QGraphicsPathItem:
        overlay = QGraphicsPathItem()
        pen = QPen(color, width)
        pen.setCosmetic(True)
        overlay.setPen(pen)
        overlay.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        overlay.setZValue(_Z_OVERLAY)
        overlay.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._scene.addItem(overlay)
        return overlay

    def _set_overlay(self, overlay: QGraphicsPathItem, records: Iterable[_Record]) -> None:
        path = QPainterPath()
        for r in records:
            if r.kind is ItemKind.LABEL:
                continue
            path.addPath(r.item.mapToScene(r.item.shape()))
        overlay.setPath(path)

    # ================================================================ state
    def _copper_z(self, layer: str) -> float:
        pos = copper_stack_position(layer)
        # Front copper on top: invert physical stack position.
        return _Z_COPPER_BASE + (20.0 - min(pos, 10_000) / 500.0)

    def _content_rect(self) -> QRectF:
        if self._board is None or self._board.bounds is None:
            return QRectF()
        b = self._board.bounds
        return QRectF(_mm(b.min_x), _mm(b.min_y), max(_mm(b.width), 1.0), max(_mm(b.height), 1.0))

    def _apply_z(self) -> None:
        for r in self._records:
            boost = (
                _Z_ACTIVE_BOOST
                if r.kind in (ItemKind.TRACK, ItemKind.PAD)
                and len(r.layers) == 1
                and r.layers[0] == self._active_layer
                else 0.0
            )
            r.item.setZValue(r.base_z + boost)

    def _record_visible(self, r: _Record) -> bool:
        if r.kind is ItemKind.LABEL or r.kind is ItemKind.OUTLINE:
            return r.item.isVisible()
        if r.kind is ItemKind.COMPONENT:
            return self._show_footprints and any(
                self._layer_visible.get(layer, True) for layer in r.layers
            )
        if r.layers and not any(self._layer_visible.get(layer, True) for layer in r.layers):
            return False
        if r.net is not None:
            if r.net in self._hidden_nets:
                return False
            if self._isolated_net is not None and r.net != self._isolated_net:
                return False
        elif self._isolated_net is not None:
            return False
        return True

    def _apply_visibility(self) -> None:
        for r in self._records:
            if r.kind in (ItemKind.LABEL, ItemKind.OUTLINE):
                continue
            r.item.setVisible(self._record_visible(r))
        self._update_label_lod()

    def _update_label_lod(self) -> None:
        show = self._show_labels and self._show_footprints and self.px_per_mm >= LABEL_MIN_PX_PER_MM
        for label in self._labels:
            label.setVisible(show)

    def _after_zoom(self) -> None:
        self._update_label_lod()
        self.zoomChanged.emit(self.px_per_mm)

    # ================================================================ picking
    def _pick(self, view_pos: QPointF) -> _Record | None:
        scene_pos = self.mapToScene(view_pos.toPoint())
        # A few pixels of tolerance so thin tracks are clickable when zoomed out.
        tol = 3.0 / max(self.px_per_mm, 1e-6)
        area = QRectF(scene_pos.x() - tol, scene_pos.y() - tol, 2 * tol, 2 * tol)
        items = self._scene.items(area, Qt.ItemSelectionMode.IntersectsItemShape)
        best: _Record | None = None
        for item in items:
            rec = self._by_item.get(id(item))
            if rec is None or rec.kind not in _PICK_PRIORITY or not item.isVisible():
                continue
            if best is None or (
                (_PICK_PRIORITY[rec.kind], -item.zValue())
                < (_PICK_PRIORITY[best.kind], -best.item.zValue())
            ):
                best = rec
        return best

    def describe(self, kind: ItemKind, obj_id: str) -> str:
        board = self._board
        if board is None:
            return ""
        idx = board.index
        if kind is ItemKind.PAD and obj_id in idx.pads_by_id:
            p = idx.pads_by_id[obj_id]
            return f"Pad {p.footprint_ref}.{p.number or '?'}  net {p.net_name or '<no net>'}"
        if kind is ItemKind.TRACK and obj_id in idx.tracks_by_id:
            t = idx.tracks_by_id[obj_id]
            return (
                f"Track {t.layer}  net {t.net_name or '<no net>'}  w {format_mm(t.width)}  "
                f"len {format_mm(t.length)}"
            )
        if kind is ItemKind.VIA and obj_id in idx.vias_by_id:
            v = idx.vias_by_id[obj_id]
            return f"Via  net {v.net_name or '<no net>'}  Ø {format_mm(v.diameter)}"
        if kind is ItemKind.COMPONENT and obj_id in idx.components_by_id:
            c = idx.components_by_id[obj_id]
            return f"{c.reference}  {c.value or ''}  {c.footprint.lib_id}"
        return ""

    # ================================================================ events
    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta:
            self.zoom_by(math.pow(WHEEL_ZOOM_BASE, delta))
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        button = event.button()
        if button == Qt.MouseButton.MiddleButton or (
            button == Qt.MouseButton.LeftButton and self._space_down
        ):
            self._panning = True
            self._pan_last = event.position()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if button == Qt.MouseButton.LeftButton:
            rec = self._pick(event.position())
            if rec is None:
                self.clear_selection()
                self.selectionCleared.emit()
            else:
                self.select_object(rec.kind, rec.obj_id)
                self.objectSelected.emit(rec.kind, rec.obj_id)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        if self._panning:
            delta = pos - self._pan_last
            self._pan_last = pos
            h, v = self.horizontalScrollBar(), self.verticalScrollBar()
            h.setValue(h.value() - round(delta.x()))
            v.setValue(v.value() - round(delta.y()))
            event.accept()
            return
        scene_pos = self.mapToScene(pos.toPoint())
        self.cursorMoved.emit(scene_pos.x(), scene_pos.y())
        rec = self._pick(pos)
        key = (rec.kind, rec.obj_id) if rec is not None else None
        if key != self._hover_key:
            self._hover_key = key
            if rec is None:
                self._hover_overlay.setPath(QPainterPath())
                QToolTip.hideText()
            else:
                self._set_overlay(self._hover_overlay, self._by_id.get((rec.kind, rec.obj_id), []))
                QToolTip.showText(
                    event.globalPosition().toPoint(), self.describe(rec.kind, rec.obj_id), self
                )
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._panning and event.button() in (
            Qt.MouseButton.MiddleButton,
            Qt.MouseButton.LeftButton,
        ):
            self._panning = False
            self.viewport().setCursor(
                Qt.CursorShape.OpenHandCursor if self._space_down else Qt.CursorShape.ArrowCursor
            )
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._hover_key = None
        self._hover_overlay.setPath(QPainterPath())
        super().leaveEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_down = True
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_down = False
            if not self._panning:
                self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        super().keyReleaseEvent(event)

    # ================================================================ grid
    def drawBackground(self, painter: QPainter, rect: QRectF | QRect) -> None:
        rect = QRectF(rect)
        painter.fillRect(rect, theme.CANVAS_BACKGROUND)
        if not self._grid_visible:
            return
        scale = self.px_per_mm
        if scale <= 0:
            return
        spacing = self._grid_spacing_mm
        steps = (2.0, 2.5, 2.0)  # 1-2-5 progression
        i = 0
        while spacing * scale < GRID_MIN_PX:
            spacing *= steps[i % 3]
            i += 1
        minor_lines: list[QLineF] = []
        major_lines: list[QLineF] = []
        # Integer indices avoid float drift; every 10th line is a major line.
        for k in range(math.floor(rect.left() / spacing), math.ceil(rect.right() / spacing) + 1):
            x = k * spacing
            (major_lines if k % 10 == 0 else minor_lines).append(
                QLineF(x, rect.top(), x, rect.bottom())
            )
        for k in range(math.floor(rect.top() / spacing), math.ceil(rect.bottom() / spacing) + 1):
            y = k * spacing
            (major_lines if k % 10 == 0 else minor_lines).append(
                QLineF(rect.left(), y, rect.right(), y)
            )
        pen = QPen(theme.GRID_MINOR, 1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawLines(minor_lines)
        pen.setColor(theme.GRID_MAJOR)
        painter.setPen(pen)
        painter.drawLines(major_lines)
