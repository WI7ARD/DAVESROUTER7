"""Manual trace/via drawing on the canvas (Stage 10).

Click to place trace vertices for the selected net on the active layer, ``V``
drops a via and switches copper side, Enter/double-click commits, Esc cancels.
Every click is snapped to the grid (Shift constrains to 45 degrees) with a
live DRC-coloured preview; the commit goes through
:class:`CommitManualCopperCommand`, so hand-drawn copper is validated exactly
like router copper and participates in undo/redo.
"""

from __future__ import annotations

import contextlib
from itertools import pairwise
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QPointF, Qt
from PySide6.QtGui import QAction, QColor, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPathItem,
    QMenu,
    QToolBar,
)

from pcbrouter.commands.route_commands import CommitManualCopperCommand
from pcbrouter.domain.geometry import Point
from pcbrouter.domain.units import Nm, internal_to_mm, mm_to_internal
from pcbrouter.domain.via import Via
from pcbrouter.routing.collision import ValidationStatus
from pcbrouter.routing.manual import (
    DEFAULT_SNAP_MM,
    ManualDrawError,
    ManualViaSizes,
    build_manual_tracks,
    build_manual_via,
    new_run_id,
    resolve_manual_via,
    resolve_manual_width,
    snap_45,
    snap_to_grid,
)
from pcbrouter.routing.working_board import WorkingBoard
from pcbrouter.ui import theme
from pcbrouter.ui.overlays import Z_OVERLAY_BASE

if TYPE_CHECKING:
    from pcbrouter.ui.main_window import MainWindow

GROUP = "manual"


class ManualDrawController(QObject):
    """Click-to-draw traces for the selected net (undoable, validated)."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.w = window
        self.act_draw = QAction("Draw &Trace", self)
        self.act_draw.setShortcut("T")
        self.act_draw.setStatusTip(
            "Hand-draw a trace for the selected net (click: vertex, V: via, "
            "Enter: commit, Esc: cancel)"
        )
        self.act_draw.setCheckable(True)
        self.act_draw.triggered.connect(self._on_toggled)
        self.act_finish = QAction("&Finish Trace", self)
        self.act_finish.setStatusTip("Commit the hand-drawn trace (validated, undoable)")
        self.act_finish.triggered.connect(lambda: self.finish())
        self.act_finish.setEnabled(False)
        self._net: str | None = None
        self._width: Nm | None = None
        self._runs: list[list[Point]] = []
        self._run_layers: list[str] = []
        self._vias: list[tuple[Point, str, str, ManualViaSizes]] = []
        self._cursor: Point | None = None
        window.canvas.aboutToClear.connect(self._on_board_cleared)

    def install(self, edit_menu: QMenu, toolbar: QToolBar) -> None:
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_draw)
        edit_menu.addAction(self.act_finish)
        toolbar.addAction(self.act_draw)

    @property
    def active(self) -> bool:
        return self._net is not None

    # ------------------------------------------------------------ lifecycle
    def _on_toggled(self, on: bool) -> None:
        from pcbrouter.ui.shortcuts import typing_focus

        if on and typing_focus():
            self.act_draw.setChecked(False)
            return
        if on:
            if not self.start():
                self.act_draw.setChecked(False)
        else:
            self.cancel()

    def start(self) -> bool:
        wb = self._working()
        if wb is None:
            self._say("Open a board first.")
            return False
        net = self.w.routing_ui.selected_net()
        if not net:
            self._say("Select a net first (Nets panel or canvas), then draw.")
            return False
        try:
            width = resolve_manual_width(wb, net)
        except ManualDrawError as exc:
            self._say(f"Cannot draw {net}: {exc}")
            return False
        layer = self.w.canvas.active_layer
        copper = list(wb.engine.geometry.copper_layers)
        if layer not in copper:
            self._say("Select a copper layer first.")
            return False
        self._reset()  # also disconnects a previous session if start ran twice
        self._net = net
        self._runs, self._run_layers, self._vias, self._cursor = [], [], [], None
        canvas = self.w.canvas
        canvas.drawClicked.connect(self._on_click)
        canvas.drawMoved.connect(self._on_move)
        canvas.drawFinished.connect(self.finish)
        canvas.drawViaRequested.connect(self.place_via)
        canvas.drawCancelled.connect(self.cancel)
        canvas.set_draw_mode(True)
        self.act_draw.setChecked(True)
        self.act_finish.setEnabled(True)
        self._width = width
        self._say(
            f"Drawing {net} on {layer} "
            f"({internal_to_mm(width):g} mm): click adds a point, "
            "V places a via, Enter commits, Esc cancels."
        )
        return True

    def cancel(self) -> None:
        if not self.active:
            return
        self._reset()

    def _on_board_cleared(self) -> None:
        self._reset()

    def _reset(self) -> None:
        canvas = self.w.canvas
        for signal, slot in (
            (canvas.drawClicked, self._on_click),
            (canvas.drawMoved, self._on_move),
            (canvas.drawFinished, self.finish),
            (canvas.drawViaRequested, self.place_via),
            (canvas.drawCancelled, self.cancel),
        ):
            with contextlib.suppress(RuntimeError):
                signal.disconnect(slot)
        canvas.set_draw_mode(False)
        self.w.engine_ui.overlays.clear(GROUP)
        self._net = None
        self._width = None
        self._runs, self._run_layers, self._vias, self._cursor = [], [], [], None
        self.act_draw.setChecked(False)
        self.act_finish.setEnabled(False)

    # ------------------------------------------------------------ input
    def _snap(self, x_mm: float, y_mm: float) -> Point:
        canvas = self.w.canvas
        grid_mm = canvas.grid_spacing_mm if canvas.grid_visible else DEFAULT_SNAP_MM
        return snap_to_grid(
            Point(mm_to_internal(x_mm), mm_to_internal(y_mm)),
            mm_to_internal(grid_mm),
        )

    def _on_click(self, x_mm: float, y_mm: float) -> None:
        wb = self._working()
        if wb is None or self._net is None:
            return
        pt = self._snap(x_mm, y_mm)
        layer = self.w.canvas.active_layer
        if layer not in wb.engine.geometry.copper_layers:
            self._say("Select a copper layer first (V places a via to switch sides).")
            return
        if (
            self._runs
            and self._run_layers[-1] == layer
            and self._runs[-1]
            and QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            pt = snap_45(self._runs[-1][-1], pt)
        if not self._runs or self._run_layers[-1] != layer:
            self._runs.append([])
            self._run_layers.append(layer)
        self._runs[-1].append(pt)
        self._refresh_preview()

    def _on_move(self, x_mm: float, y_mm: float) -> None:
        if not self.active:
            return
        self._cursor = self._snap(x_mm, y_mm)
        self._refresh_preview()

    # ------------------------------------------------------------ via + finish
    def place_via(self) -> bool:
        wb = self._working()
        if wb is None or self._net is None or self._width is None:
            return False
        if not self._runs or not self._runs[-1]:
            self._say("Click to place a first point, then V drops a via there.")
            return False
        try:
            via_sizes = resolve_manual_via(wb, self._net)
        except ManualDrawError as exc:
            self._say(f"Cannot place via: {exc}")
            return False
        copper = list(wb.engine.geometry.copper_layers)
        if len(copper) < 2:
            self._say("Single copper layer: no via needed.")
            return False
        cur = self._run_layers[-1]
        other = copper[-1] if cur == copper[0] else copper[0]
        pos = self._runs[-1][-1]
        self._vias.append((pos, cur, other, via_sizes))
        self._runs.append([])
        self._run_layers.append(other)
        self.w.canvas.set_active_layer(other)
        self._refresh_preview()
        self._say(f"Via on {self._net} ({cur} to {other}); drawing continues on {other}.")
        return True

    def finish(self) -> bool:
        wb = self._working()
        if wb is None or self._net is None or self._width is None:
            return False
        net, width = self._net, self._width
        run_id = new_run_id()
        tracks = [
            t
            for points, layer in zip(self._runs, self._run_layers, strict=True)
            for t in build_manual_tracks(net, points, width, layer, run_id)
        ]
        vias: list[Via] = [
            build_manual_via(net, pos, sizes.diameter, sizes.drill, start, end, run_id)
            for pos, start, end, sizes in self._vias
        ]
        # A via with no segments after it still counts (single-point tap).
        if not tracks and not vias:
            self._say("Nothing to commit: click at least two points.")
            return False
        result = self.w.bus.dispatch(CommitManualCopperCommand(net, tuple(tracks), tuple(vias)))
        if not result.success:
            self._say(f"Not committed: {result.message}")
            return False
        self._say(str(result.message))
        self.w._update_undo_actions()
        self._reset()
        return True

    # ------------------------------------------------------------ preview
    def _refresh_preview(self) -> None:
        wb = self._working()
        if wb is None or self._net is None or self._width is None:
            return
        width = self._width
        items: list[QGraphicsItem] = []
        width_mm = internal_to_mm(width)
        validator = wb.engine.validator
        bad = theme.CANDIDATE_INVALID_COLOR
        for points, layer in zip(self._runs, self._run_layers, strict=True):
            pts = list(points)
            if self._cursor is not None and layer == self._run_layers[-1]:
                pts.append(self._cursor)
            if len(pts) < 2:
                continue
            good_path = self._path(pts)
            pen = QPen(theme.copper_color(layer), max(width_mm, 1e-4))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            item = QGraphicsPathItem(good_path)
            item.setPen(pen)
            item.setZValue(Z_OVERLAY_BASE + 10)
            items.append(item)
            bad_path = self._empty_path()
            for start, end in pairwise(pts):
                res = validator.validate_segment(self._net, layer, start, end, width)
                if res.status is ValidationStatus.INVALID:
                    bad_path.addPath(self._path([start, end]))
            if not bad_path.isEmpty():
                bad_pen = QPen(QColor(bad), 2.0)
                bad_pen.setCosmetic(True)
                bad_item = QGraphicsPathItem(bad_path)
                bad_item.setPen(bad_pen)
                bad_item.setZValue(Z_OVERLAY_BASE + 11)
                items.append(bad_item)
        for pos, _start, _end, sizes in self._vias:
            dia_mm = internal_to_mm(sizes.diameter)
            x, y = internal_to_mm(pos.x), internal_to_mm(pos.y)
            dot = QGraphicsEllipseItem(x - dia_mm / 2, y - dia_mm / 2, dia_mm, dia_mm)
            dot.setBrush(theme.VIA_COLOR)
            dot.setPen(QPen(Qt.PenStyle.NoPen))
            dot.setZValue(Z_OVERLAY_BASE + 10)
            items.append(dot)
        self.w.engine_ui.overlays.set_group(GROUP, items)

    @staticmethod
    def _path(pts: list[Point]) -> QPainterPath:
        path = QPainterPath()
        path.moveTo(QPointF(internal_to_mm(pts[0].x), internal_to_mm(pts[0].y)))
        for p in pts[1:]:
            path.lineTo(QPointF(internal_to_mm(p.x), internal_to_mm(p.y)))
        return path

    @staticmethod
    def _empty_path() -> QPainterPath:
        return QPainterPath()

    # ------------------------------------------------------------ helpers
    def _working(self) -> WorkingBoard | None:
        return self.w.bus.context.project.working

    def _say(self, message: str) -> None:
        self.w.statusBar().showMessage(message, 10000)
