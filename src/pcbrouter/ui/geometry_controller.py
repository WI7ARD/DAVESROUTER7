"""Stage 3 desktop integration: geometry engine, rules, Internal Geometry Check.

Owned by :class:`~pcbrouter.ui.main_window.MainWindow`; keeps the Stage 3 UI out of
the window class. Responsibilities:

* build the board engine (geometry, spatial index, rules, connectivity) on a
  background thread after a board opens, and discard results that arrive after the
  board changed or closed;
* run the Internal Geometry Check in the background and show it in the DRC panel;
* the Routing Rules inspector, test-segment/via validation, routing grid, clearance
  envelope and debug overlays, rule overrides, diagnostics and JSON exports;
* the status-bar fields ``Geometry / Rules / Internal DRC / Routing``.

Nothing here edits the board: candidates and overlays are display-only, overrides
live in the workspace, and exports refuse ``.kicad_*`` file names.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QRectF, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QDialog, QFileDialog, QLabel, QMenu, QToolBar

from pcbrouter.board_engine import BoardEngine
from pcbrouter.domain.units import internal_to_mm
from pcbrouter.drc.result import CHECK_NAME, DRCResult
from pcbrouter.drc.violation import DRCViolation
from pcbrouter.geometry.board import ItemKind as GeoKind
from pcbrouter.geometry.extract import clearance_envelope
from pcbrouter.geometry.shapes import Shape
from pcbrouter.geometry.summary import geometry_summary
from pcbrouter.routing.collision import CollisionResult, item_type_of
from pcbrouter.routing.congestion import CongestionMap
from pcbrouter.routing.obstacle_map import inflated_obstacles
from pcbrouter.routing.occupancy import OccupancyMap
from pcbrouter.routing.proposal import RouteProposal
from pcbrouter.rules.model import ItemType
from pcbrouter.rules.overrides import RuleOverrides
from pcbrouter.rules.snapshot import rules_snapshot
from pcbrouter.ui import dialogs, overlays, theme
from pcbrouter.ui.drc_panel import DRCPanel, status_text
from pcbrouter.ui.geometry_dialogs import (
    GeometryDiagnosticsDialog,
    OverridesDialog,
    RoutingGridDialog,
    TestSegmentDialog,
    TestViaDialog,
    collision_report,
    diagnostics_data,
)
from pcbrouter.ui.pcb_canvas import ItemKind as CanvasKind
from pcbrouter.ui.rules_panel import RulesPanel
from pcbrouter.ui.workers import JobRunner

if TYPE_CHECKING:
    from pcbrouter.ui.main_window import MainWindow

log = logging.getLogger(__name__)

ROUTING_STATUS = "Routing: Ready (CPU)"
_CANVAS_KIND = {GeoKind.PAD: CanvasKind.PAD, GeoKind.TRACK: CanvasKind.TRACK,
                GeoKind.VIA: CanvasKind.VIA}  # fmt: skip
_ENVELOPE_KINDS = {CanvasKind.PAD: "pad", CanvasKind.TRACK: "track", CanvasKind.VIA: "via"}
FOCUS_MARGIN_NM = 1_000_000


class GeometryController(QObject):
    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.w = window
        self.canvas = window.canvas
        self.jobs = JobRunner(self)
        self.overlays = overlays.OverlayManager(window.canvas)
        self.drc_panel = DRCPanel()
        self.rules_panel = RulesPanel()
        #: The engine whose geometry/rules are built and shown (None while building).
        self.engine: BoardEngine | None = None
        self.last_error: str | None = None
        self._session_key: object | None = None
        self._selected: tuple[CanvasKind, str] | None = None
        self._segment_dialog: TestSegmentDialog | None = None
        self._via_dialog: TestViaDialog | None = None
        self.last_occupancy: OccupancyMap | None = None
        self.last_congestion: CongestionMap | None = None
        self.last_candidate: tuple[RouteProposal, CollisionResult] | None = None
        self._build_actions()
        self._build_status_labels()
        self.drc_panel.runRequested.connect(self.run_geometry_check)
        self.drc_panel.violationActivated.connect(self.locate_violation)
        self.rules_panel.netSelected.connect(self._on_rules_net)
        self.canvas.objectSelected.connect(self._on_object_selected)
        self.canvas.selectionCleared.connect(self._on_selection_cleared)
        self._refresh_actions()

    # ================================================================ building UI
    def _act(self, text: str, slot: Any, tip: str, checkable: bool = False) -> QAction:
        act = QAction(text, self.w)
        act.setStatusTip(tip)
        act.setToolTip(tip)
        act.setCheckable(checkable)
        (act.toggled if checkable else act.triggered).connect(slot)
        return act

    def _build_actions(self) -> None:
        self.act_run_check = self._act(
            f"Run {CHECK_NAME}", self.run_geometry_check,
            f"Scan the board with the router's own geometry/rule engine ({CHECK_NAME}; "
            "not KiCad DRC)",
        )  # fmt: skip
        self.act_run_check.setShortcut("Ctrl+Shift+D")
        self.act_test_segment = self._act(
            "Validate Test &Segment…", self.open_test_segment,
            "Check whether a hypothetical track segment would be legal (not added to the board)",
        )  # fmt: skip
        self.act_test_via = self._act(
            "Validate Test &Via…", self.open_test_via,
            "Check whether a hypothetical via would be legal (not added to the board)",
        )  # fmt: skip
        self.act_overrides = self._act(
            "Routing Rule &Overrides…", self.open_overrides,
            "Router overrides stored in the workspace (can tighten, never weaken, rules)",
        )  # fmt: skip
        self.act_export_rules = self._act(
            "Export &Rules Snapshot…", self.export_rules_snapshot,
            "Save the resolved routing rules as rules_snapshot.json",
        )  # fmt: skip
        self.act_export_geometry = self._act(
            "Export &Geometry Summary…", self.export_geometry_summary,
            "Save counts, bounds, layers, object IDs and shape types as geometry_summary.json",
        )  # fmt: skip
        self.act_routing_grid = self._act(
            "Routing &Grid…", self.open_routing_grid,
            "Show the routing occupancy grid for a layer, net and trace width",
        )  # fmt: skip
        self.act_envelope = self._act(
            "Show &Clearance Envelope", self.toggle_clearance_envelope,
            "Show the clearance-inflated region of the selected track, pad or via",
            checkable=True,
        )  # fmt: skip
        self.act_rules_inspector = self._act(
            "Routing &Rules Inspector", self.show_rules_inspector,
            "Resolved routing rules for the selected net, with their sources",
        )  # fmt: skip
        self.act_clear_overlays = self._act(
            "Clear Engineering Overlays", self.clear_overlays,
            "Remove all Stage 3 overlays from the canvas",
        )  # fmt: skip
        self.act_diagnostics = self._act(
            "&Geometry Diagnostics…", self.show_diagnostics,
            "Engine versions, object counts, index and rule status (for bug reports)",
        )  # fmt: skip
        self.debug_actions: dict[str, QAction] = {}
        for key, title in overlays.DEBUG_OVERLAYS.items():
            act = self._act(title, lambda on, k=key: self.set_debug_overlay(k, on),
                            f"Debug overlay: {title}", checkable=True)  # fmt: skip
            self.debug_actions[key] = act

    def _build_status_labels(self) -> None:
        self.lbl_geometry = QLabel("Geometry: —")
        self.lbl_rules = QLabel("Rules: —")
        self.lbl_drc = QLabel("Internal DRC: —")
        self.lbl_routing = QLabel(ROUTING_STATUS)
        self.lbl_drc.setToolTip(f"{CHECK_NAME} (the router's own check; not KiCad DRC)")
        self.lbl_routing.setToolTip(
            "Deterministic router; candidates are previewed before any commit"
        )

    def install(self, view: QMenu, tools: QMenu, help_menu: QMenu, toolbar: QToolBar) -> None:
        """Add menus, toolbar tools, docks and status fields to the main window."""
        w = self.w
        w._add_dock(
            "drc", "Internal Geometry Check", self.drc_panel, Qt.DockWidgetArea.BottomDockWidgetArea
        )
        w._add_dock(
            "rules", "Routing Rules", self.rules_panel, Qt.DockWidgetArea.BottomDockWidgetArea
        )
        w.tabifyDockWidget(w.docks["log"], w.docks["drc"])
        w.tabifyDockWidget(w.docks["drc"], w.docks["rules"])
        w.docks["log"].raise_()

        view.addSeparator()
        for key, title in (("drc", "Internal &Geometry Check"), ("rules", "Routing &Rules")):
            act = w.docks[key].toggleViewAction()
            act.setText(title)
            view.addAction(act)
        view.addSeparator()
        view.addAction(self.act_routing_grid)
        view.addAction(self.act_envelope)
        debug = view.addMenu("&Debug Overlays")
        for act in self.debug_actions.values():
            debug.addAction(act)
        debug.addSeparator()
        debug.addAction(self.act_clear_overlays)

        tools.addSeparator()
        for act in (self.act_run_check, self.act_test_segment, self.act_test_via,
                    self.act_rules_inspector, self.act_overrides):  # fmt: skip
            tools.addAction(act)
        tools.addSeparator()
        tools.addAction(self.act_export_rules)
        tools.addAction(self.act_export_geometry)
        help_menu.addAction(self.act_diagnostics)

        toolbar.addSeparator()
        for act in (self.act_run_check, self.act_routing_grid, self.act_envelope,
                    self.act_test_segment, self.act_rules_inspector):  # fmt: skip
            toolbar.addAction(act)

        sb = w.statusBar()
        for lbl in (self.lbl_geometry, self.lbl_rules, self.lbl_drc, self.lbl_routing):
            sb.addPermanentWidget(lbl)

    # ================================================================ lifecycle
    @property
    def project(self) -> Any:
        return self.w.bus.context.project

    def on_board_changed(self) -> None:
        """Called by the window after every open/close."""
        session = self.project.session
        if session is self._session_key and session is not None:
            return
        self._session_key = session
        self._reset()
        if session is None:
            self.lbl_geometry.setText("Geometry: —")
            self.lbl_rules.setText("Rules: —")
            self._set_drc_label(None, board=False)
            return
        self.project.set_conservative_rules(self.w.settings.geometry.conservative_rules)
        self.refresh_engine()

    def shutdown(self) -> None:
        """Window closing: finish background work and close tool dialogs."""
        self.jobs.wait(5000)
        for dlg in (self._segment_dialog, self._via_dialog):
            if dlg is not None:
                dlg.close()

    def _reset(self) -> None:
        self.engine = None
        self.last_error = None
        self._selected = None
        self.last_occupancy = None
        self.last_congestion = None
        self.last_candidate = None
        for dlg in (self._segment_dialog, self._via_dialog):
            if dlg is not None:
                dlg.close()
        self._segment_dialog = self._via_dialog = None
        self.overlays.clear_all()
        for act in [self.act_envelope, *self.debug_actions.values()]:
            act.blockSignals(True)
            act.setChecked(False)
            act.blockSignals(False)
        self.drc_panel.set_result(None)
        self.rules_panel.set_engine(None)
        self._refresh_actions()

    def refresh_engine(self) -> None:
        """Adopt the project's current engine, building it in the background."""
        engine = self.project.engine
        if engine is None or engine is self.engine:
            return
        self.engine = None
        self._refresh_actions()
        self.lbl_geometry.setText("Geometry: Building…")
        self.lbl_rules.setText("Rules: Loading…")

        def build(e: BoardEngine = engine) -> BoardEngine:
            _ = (e.geometry, e.ruleset, e.resolver, e.validator, e.connectivity)
            return e

        if not self.jobs.start(f"engine-{id(engine)}", build, self._engine_ready, self._job_failed):
            return

    def _engine_ready(self, engine: object, secs: float) -> None:
        if not isinstance(engine, BoardEngine) or engine is not self.project.engine:
            log.info("geometry.build discarded (board or rules changed meanwhile)")
            self.refresh_engine()
            return
        previous_drc = self.engine.last_drc if self.engine else None
        self.engine = engine
        geo = engine.geometry
        log.info(
            "geometry.ready items=%d index=%s entries=%d build_ms=%.1f total_ms=%.1f",
            len(geo.copper), geo.hole_index.kind, geo.index_entry_count,
            geo.build_seconds * 1e3, secs * 1e3,
        )  # fmt: skip
        self.lbl_geometry.setText("Geometry: Ready")
        self.lbl_routing.setText(ROUTING_STATUS)
        self.lbl_geometry.setToolTip(
            f"{len(geo.copper)} copper objects, {len(geo.holes)} holes, {len(geo.keepouts)} "
            f"keepout areas · {geo.hole_index.kind} index · board region {geo.region.status.value}"
        )
        rs = engine.ruleset
        crit = len(rs.critical_unsupported)
        if crit:
            self.lbl_rules.setText(f"Rules: Loaded ({crit} unsupported critical)")
        elif rs.unsupported:
            self.lbl_rules.setText(f"Rules: Loaded ({len(rs.unsupported)} unsupported)")
        else:
            self.lbl_rules.setText("Rules: Loaded")
        self.lbl_rules.setToolTip(
            f"{len(rs.classes.classes)} net classes · {len(rs.custom_rules)} custom rules · "
            f"conservative handling {'ON' if engine.config.conservative else 'OFF'}"
        )
        if rs.unsupported:
            self.w.statusBar().showMessage(
                "Unsupported deterministic rule detected — routing for affected nets will be "
                "conservative until supported (see Routing Rules).", 10000,
            )  # fmt: skip
        self.rules_panel.set_engine(engine)
        self._refresh_actions()
        self.w.refresh_statistics()
        if (
            previous_drc is None
            or previous_drc.rules_digest != rs.digest
            or previous_drc.board_fingerprint != engine.board.fingerprint
        ):
            self._set_drc_label(None, board=True)
            self.drc_panel.set_result(None)
            self.overlays.clear("drc")
        if self.w.settings.geometry.check_on_open and engine.last_drc is None:
            self.run_geometry_check()
        self._refresh_debug_overlays()

    def routing_status(self) -> str:
        return ROUTING_STATUS if self.engine is not None else "Routing: —"

    def _job_failed(self, message: str, detail: str) -> None:
        self.last_error = message
        self.lbl_geometry.setText("Geometry: Error")
        self.lbl_geometry.setToolTip(message)
        self.w.statusBar().showMessage(message, 10000)
        dialogs.show_error(self.w, "Geometry engine error", message, detail)
        self._refresh_actions()

    def _refresh_actions(self) -> None:
        ready = self.engine is not None
        actions = [self.act_run_check, self.act_test_segment, self.act_test_via,
                   self.act_overrides, self.act_export_rules, self.act_export_geometry,
                   self.act_routing_grid, self.act_envelope]  # fmt: skip
        for act in [*actions, *self.debug_actions.values()]:
            act.setEnabled(ready)
        self.drc_panel.set_enabled_for_board(ready)

    def _require_engine(self) -> BoardEngine | None:
        if self.engine is None:
            msg = "Open a board first." if self.project.session is None else (
                "The geometry engine is still building…")  # fmt: skip
            self.w.statusBar().showMessage(msg, 4000)
        return self.engine

    def wait_until_ready(self, msecs: int = 30_000) -> bool:
        """Tests: finish background work and deliver its results."""
        from PySide6.QtCore import QCoreApplication, QElapsedTimer

        timer = QElapsedTimer()
        timer.start()
        while self.jobs.is_running() and timer.elapsed() < msecs:
            self.jobs.wait(50)
            QCoreApplication.processEvents()
        QCoreApplication.processEvents()
        return not self.jobs.is_running()

    # ================================================================ DRC
    def run_geometry_check(self) -> None:
        engine = self._require_engine()
        if engine is None or self.jobs.is_running("drc"):
            return
        self.drc_panel.set_running(True)
        self.lbl_drc.setText("Internal DRC: Running…")

        def done(result: object, _secs: float) -> None:
            self._drc_done(engine, result)

        self.jobs.start("drc", engine.run_drc, done, self._drc_failed)

    def _drc_done(self, engine: BoardEngine, result: object) -> None:
        self.drc_panel.set_running(False)
        if engine is not self.engine or not isinstance(result, DRCResult):
            log.info("drc.result discarded (board or rules changed meanwhile)")
            self._set_drc_label(None, board=self.engine is not None)
            return
        log.info("drc.done %s", result.summary())
        self.drc_panel.set_result(result)
        self._set_drc_label(result, board=True)
        self._draw_drc(result)
        dock = self.w.docks["drc"]
        dock.show()
        dock.raise_()
        self.w.statusBar().showMessage(result.summary(), 10000)
        self.w.refresh_statistics()

    def _drc_failed(self, message: str, detail: str) -> None:
        self.drc_panel.set_running(False)
        self._set_drc_label(None, board=True)
        dialogs.show_error(self.w, f"{CHECK_NAME} failed", message, detail)

    def _set_drc_label(self, result: DRCResult | None, board: bool) -> None:
        if not board:
            self.lbl_drc.setText("Internal DRC: —")
            self.lbl_drc.setStyleSheet("")
            return
        text, color = status_text(result)
        self.lbl_drc.setText(text)
        self.lbl_drc.setStyleSheet(f"color:{color.name()}" if color is not None else "")

    def _shapes_for(self, uid: str | None) -> tuple[Shape, ...]:
        if not uid or self.engine is None:
            return ()
        geo = self.engine.geometry
        if uid in geo.copper:
            return geo.copper[uid].shapes
        if uid in geo.holes:
            return (geo.holes[uid].shape,)
        if uid in geo.keepouts:
            return (geo.keepouts[uid].shape,)
        if uid in geo.edges:
            return (geo.edges[uid].shape,)
        return ()

    def _draw_drc(self, result: DRCResult) -> None:
        uids = {u for v in result.violations for u in (v.object_a, v.object_b) if u}
        shapes = {uid: self._shapes_for(uid) for uid in uids}
        located = [v for v in result.violations if v.severity.value != "info" or v.location]
        self.overlays.set_group("drc", overlays.violation_items(located, shapes))

    def locate_violation(self, v: DRCViolation) -> None:
        """Zoom to a violation and highlight the objects involved."""
        if self.engine is None:
            return
        shapes = [s for uid in (v.object_a, v.object_b) for s in self._shapes_for(uid)]
        self.overlays.set_group("drc_focus", overlays.violation_items([v], {
            uid: self._shapes_for(uid) for uid in (v.object_a, v.object_b) if uid}))  # fmt: skip
        rect: QRectF | None = None
        if v.location is not None:
            m = internal_to_mm(FOCUS_MARGIN_NM)
            rect = QRectF(internal_to_mm(v.location.x) - m, internal_to_mm(v.location.y) - m,
                          2 * m, 2 * m)  # fmt: skip
        elif shapes:
            box = shapes[0].bounds
            for s in shapes[1:]:
                box = box.union(s.bounds)
            rect = overlays.rect_mm(box.expanded(FOCUS_MARGIN_NM))
        geo = self.engine.geometry
        for uid in (v.object_a, v.object_b):
            if uid and uid in geo.copper and geo.copper[uid].kind in _CANVAS_KIND:
                item = geo.copper[uid]
                self.canvas.select_object(_CANVAS_KIND[item.kind], item.source_id)
                break
        if rect is not None:
            self.canvas.focus_on(rect)

    # ================================================================ rules inspector
    def show_rules_inspector(self) -> None:
        dock = self.w.docks["rules"]
        dock.show()
        dock.raise_()
        net = self._selected_net()
        if net:
            self.rules_panel.show_net(net)

    def _on_rules_net(self, net: str) -> None:
        if self.canvas.board is not None and self.w.docks["rules"].isVisible():
            self.canvas.select_object(CanvasKind.NET, net)

    def _selected_net(self) -> str | None:
        board = self.canvas.board
        if board is None or self._selected is None:
            return None
        kind, obj_id = self._selected
        if kind is CanvasKind.NET:
            return obj_id
        idx = board.index
        if kind is CanvasKind.PAD and obj_id in idx.pads_by_id:
            return idx.pads_by_id[obj_id].net_name
        if kind is CanvasKind.TRACK and obj_id in idx.tracks_by_id:
            return idx.tracks_by_id[obj_id].net_name
        if kind is CanvasKind.VIA and obj_id in idx.vias_by_id:
            return idx.vias_by_id[obj_id].net_name
        return None

    # ================================================================ selection
    def _on_object_selected(self, kind: CanvasKind, obj_id: str) -> None:
        self._selected = (kind, obj_id)
        net = self._selected_net()
        if net and self.w.docks["rules"].isVisible():
            self.rules_panel.blockSignals(True)
            self.rules_panel.show_net(net)
            self.rules_panel.blockSignals(False)
        if self.act_envelope.isChecked():
            self._draw_envelope()

    def _on_selection_cleared(self) -> None:
        self._selected = None
        self.overlays.clear("envelope")

    # ================================================================ test geometry
    def open_test_segment(self) -> TestSegmentDialog | None:
        engine = self._require_engine()
        if engine is None:
            return None
        if self._segment_dialog is None or self._segment_dialog.engine is not engine:
            dlg = TestSegmentDialog(engine, self.w, self.canvas.active_layer, self._selected_net())
            dlg.validated.connect(self._on_candidate)
            dlg.finished.connect(lambda _r: self.overlays.clear("candidate"))
            self._segment_dialog = dlg
        self._segment_dialog.show()
        self._segment_dialog.raise_()
        return self._segment_dialog

    def open_test_via(self) -> TestViaDialog | None:
        engine = self._require_engine()
        if engine is None:
            return None
        if self._via_dialog is None or self._via_dialog.engine is not engine:
            dlg = TestViaDialog(engine, self.w, self._selected_net())
            dlg.validated.connect(self._on_candidate)
            dlg.finished.connect(lambda _r: self.overlays.clear("candidate"))
            self._via_dialog = dlg
        self._via_dialog.show()
        self._via_dialog.raise_()
        return self._via_dialog

    def _on_candidate(self, proposal: object, shapes: object, result: object) -> None:
        if not isinstance(result, CollisionResult) or not isinstance(proposal, RouteProposal):
            return
        self.last_candidate = (proposal, result)
        log.info("geometry.test %s net=%s status=%s checks=%d",
                 "segment" if proposal.segments else "via", proposal.net or "-",
                 result.status.value, result.checks)  # fmt: skip
        log.debug("geometry.test.report\n%s", collision_report(result))
        items = overlays.candidate_items(list(shapes) if isinstance(shapes, list) else [],
                                         result.status)  # fmt: skip
        located = [c for c in result.collisions if c.location is not None]
        for c in located[:50]:
            items += overlays.violation_items([_as_violation(c)], {})
        self.overlays.set_group("candidate", items)

    # ================================================================ clearance envelope
    def toggle_clearance_envelope(self, checked: bool) -> None:
        if not checked:
            self.overlays.clear("envelope")
            return
        if not self._draw_envelope():
            self.w.statusBar().showMessage(
                "Select a track, pad or via to show its clearance envelope.", 5000
            )

    def _draw_envelope(self) -> bool:
        engine = self.engine
        if engine is None or self._selected is None:
            self.overlays.clear("envelope")
            return False
        kind, obj_id = self._selected
        prefix = _ENVELOPE_KINDS.get(kind)
        item = engine.geometry.copper.get(f"{prefix}:{obj_id}") if prefix else None
        if item is None:
            self.overlays.clear("envelope")
            return False
        layer = self.canvas.active_layer if self.canvas.active_layer in item.layers else None
        req = engine.resolver.resolve_clearance(
            item.net, None, item_type_of(item.kind), ItemType.TRACK,
            layer or next(iter(sorted(item.layers))), item.local_clearance, None, item.label,
        )  # fmt: skip
        if req.value is None:
            self.overlays.clear("envelope")
            self.w.statusBar().showMessage(
                f"Clearance for {item.label} is unknown — no envelope drawn "
                f"({req.source.describe()}).", 6000,
            )  # fmt: skip
            return True
        env = clearance_envelope(item, req.value)
        group = overlays.envelope_items(item.shapes, env)
        for g in group:
            g.setToolTip(f"{item.label}: clearance {req.describe()}")
        self.overlays.set_group("envelope", group)
        self.w.statusBar().showMessage(
            f"Clearance envelope of {item.label}: {req.describe()}", 6000
        )
        return True

    # ================================================================ routing grid & debug
    def open_routing_grid(self) -> None:
        engine = self._require_engine()
        if engine is None:
            return
        dlg = RoutingGridDialog(engine, self.w, self.canvas.active_layer, self._selected_net(),
                                self.w.settings.geometry.grid_resolution_mm)  # fmt: skip
        if self.w.run_dialog(dlg) != QDialog.DialogCode.Accepted:
            return
        self.show_routing_grid(*dlg.request())

    def show_routing_grid(self, layer: str, net: str | None, width: int, cell: int) -> None:
        engine = self._require_engine()
        if engine is None:
            return
        self.w.statusBar().showMessage("Building routing grid…")

        def build() -> OccupancyMap:
            return engine.occupancy(layer, net, width, cell)

        def done(occ: object, secs: float) -> None:
            self._grid_done(engine, occ, secs)

        self.jobs.start("grid", build, done, self._overlay_failed)

    def _grid_done(self, engine: BoardEngine, occ: object, secs: float) -> None:
        if engine is not self.engine or not isinstance(occ, OccupancyMap):
            return
        self.last_occupancy = occ
        self.overlays.set_group("debug:grid", [overlays.occupancy_item(occ)])
        act = self.debug_actions["grid"]
        act.blockSignals(True)
        act.setChecked(True)
        act.blockSignals(False)
        counts = occ.counts()
        log.info("occupancy.shown layer=%s cells=%d ms=%.1f counts=%s",
                 occ.spec.layer, occ.spec.cell_count, occ.elapsed_s * 1e3, counts)  # fmt: skip
        note = "" if occ.rules_complete else " · some rules unknown (orange cells)"
        self.w.statusBar().showMessage(
            f"Routing grid {occ.spec.layer}: {occ.spec.nx}×{occ.spec.ny} cells, "
            f"{occ.free_fraction:.0%} free, built in {occ.elapsed_s * 1e3:.0f} ms{note}", 10000,
        )  # fmt: skip

    def _overlay_failed(self, message: str, detail: str) -> None:
        self.w.statusBar().showMessage(message, 10000)
        dialogs.show_error(self.w, "Overlay failed", message, detail)

    def set_debug_overlay(self, key: str, on: bool) -> None:
        name = f"debug:{key}"
        if not on:
            self.overlays.clear(name)
            return
        engine = self._require_engine()
        if engine is None:
            return
        geo = engine.geometry
        layer = self.canvas.active_layer or (geo.copper_layers[0] if geo.copper_layers else "")
        net = self._selected_net()
        if key == "boundary":
            items = overlays.polygon_items((p.points for p in geo.region.loops),
                                           theme.BOUNDARY_COLOR)  # fmt: skip
        elif key == "keepouts":
            shapes = [k.shape for k in geo.keepouts.values()]
            items = overlays.outline_items(shapes, theme.KEEPOUT_COLOR, fill_alpha=40, dashed=True)
        elif key == "raw_bounds":
            box = geo.board.bounds
            boxes = [i.bounds for i in geo.copper_near(layer, box)] if box else []
            items = overlays.bounds_items(boxes + [h.bounds for h in geo.holes.values()],
                                          theme.RAW_BOUNDS_COLOR)  # fmt: skip
        elif key == "inflated":
            width = engine.resolver.resolve_trace_width(net, layer).value
            if width is None:
                self.w.statusBar().showMessage("No track-width rule: cannot inflate.", 5000)
                return
            obs = inflated_obstacles(geo, engine.resolver, layer, net, width)
            items = overlays.outline_items([s for o in obs for s in o.inflated],
                                           theme.INFLATED_COLOR, fill_alpha=30)  # fmt: skip
            self.w.statusBar().showMessage(
                f"Inflated obstacles on {layer} for net {net or '(none)'} at "
                f"{internal_to_mm(width):g} mm: {len(obs)} object(s)", 8000,
            )  # fmt: skip
        elif key == "airwires":
            items = overlays.airwire_items(engine.connectivity.airwires)
        elif key == "grid":
            g = self.w.settings.geometry
            width = engine.resolver.resolve_trace_width(net, layer).value
            from pcbrouter.domain.units import mm_to_internal

            self.show_routing_grid(layer, net, width or mm_to_internal(0.2),
                                   mm_to_internal(g.grid_resolution_mm))  # fmt: skip
            return
        elif key == "congestion":

            def build_congestion() -> CongestionMap:
                return engine.congestion(layer)

            def congestion_done(cmap: object, _secs: float) -> None:
                self._congestion_done(engine, cmap)

            self.jobs.start("congestion", build_congestion, congestion_done, self._overlay_failed)
            return
        else:
            return
        self.overlays.set_group(name, items)

    def _congestion_done(self, engine: BoardEngine, cmap: object) -> None:
        if engine is not self.engine or not isinstance(cmap, CongestionMap):
            return
        self.last_congestion = cmap
        if self.debug_actions["congestion"].isChecked():
            self.overlays.set_group("debug:congestion", [overlays.congestion_item(cmap)])
            self.w.statusBar().showMessage(
                f"{cmap.layer}: {cmap.label}, mean {cmap.mean:.2f} (reference width "
                f"{cmap.reference_width_source})", 8000,
            )  # fmt: skip

    def _refresh_debug_overlays(self) -> None:
        for key, act in self.debug_actions.items():
            if act.isChecked() and key not in ("grid",):
                self.set_debug_overlay(key, True)

    def clear_overlays(self) -> None:
        self.overlays.clear_all()
        for act in [self.act_envelope, *self.debug_actions.values()]:
            act.blockSignals(True)
            act.setChecked(False)
            act.blockSignals(False)

    # ================================================================ overrides / settings
    def open_overrides(self) -> None:
        engine = self._require_engine()
        if engine is None:
            return
        dlg = OverridesDialog(engine, self.project.manual_overrides(), self.w)
        if self.w.run_dialog(dlg) != QDialog.DialogCode.Accepted:
            return
        self.save_overrides(dlg.result_overrides())

    def save_overrides(self, overrides: RuleOverrides) -> None:
        try:
            self.project.save_manual_overrides(overrides)
        except OSError as exc:
            dialogs.show_error(self.w, "Overrides not saved", "Could not save overrides.", str(exc))
            return
        self.w.statusBar().showMessage("Routing rule overrides saved to the workspace.", 6000)
        self.refresh_engine()

    def on_ai_constraints_changed(self, overrides: RuleOverrides | None) -> None:
        """Approved/undone AI constraints feed the rule resolver (never weaken rules)."""
        if self.project.session is None:
            return
        self.project.set_ai_overrides(overrides)
        self.refresh_engine()

    def apply_settings(self) -> None:
        if self.project.session is None:
            return
        self.project.set_conservative_rules(self.w.settings.geometry.conservative_rules)
        self.refresh_engine()

    # ================================================================ exports & diagnostics
    def _save_path(self, title: str, name: str) -> Path | None:
        session = self.project.session
        base = Path(self.w.settings.last_open_directory or Path.home())
        start = str(base / (f"{session.source_path.stem}-{name}" if session else name))
        path, _ = QFileDialog.getSaveFileName(self.w, title, start, "JSON (*.json)")
        return Path(path) if path else None

    def export_rules_snapshot(self) -> None:
        path = self._save_path("Export rules snapshot", "rules_snapshot.json")
        if path is not None:
            self.write_rules_snapshot(path)

    def export_geometry_summary(self) -> None:
        path = self._save_path("Export geometry summary", "geometry_summary.json")
        if path is not None:
            self.write_geometry_summary(path)

    def write_rules_snapshot(self, path: Path) -> bool:
        engine = self._require_engine()
        if engine is None:
            return False
        return self._write_json(path, rules_snapshot(engine.resolver, engine.board))

    def write_geometry_summary(self, path: Path) -> bool:
        engine = self._require_engine()
        if engine is None:
            return False
        return self._write_json(path, geometry_summary(engine.geometry))

    def _write_json(self, path: Path, data: dict[str, Any]) -> bool:
        if path.suffix.lower().startswith(".kicad"):
            dialogs.show_error(self.w, "Export refused", "Choose a .json file name.")
            return False
        try:
            path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        except OSError as exc:
            dialogs.show_error(self.w, "Export failed", f"Could not write {path}.", str(exc))
            return False
        log.info("export.written path=%s", path)
        self.w.statusBar().showMessage(f"Exported {path.name}", 6000)
        return True

    def diagnostics(self) -> dict[str, Any]:
        extra: dict[str, Any] = {
            "geometry_status": self.lbl_geometry.text(),
            "rules_status": self.lbl_rules.text(),
            "internal_drc_status": self.lbl_drc.text(),
            "routing": ROUTING_STATUS,
        }
        if self.last_error:
            extra["last_error"] = self.last_error
        if self.last_occupancy is not None:
            o = self.last_occupancy
            extra["last_occupancy"] = {
                "layer": o.spec.layer, "cells": o.spec.cell_count,
                "cell_um": o.spec.cell // 1000, "ms": round(o.elapsed_s * 1e3, 2),
            }  # fmt: skip
        return diagnostics_data(self.engine, extra)

    def show_diagnostics(self) -> None:
        self.w.run_dialog(GeometryDiagnosticsDialog(self.diagnostics(), self.w))

    # ================================================================ statistics
    def statistics(self) -> list[tuple[str, str]]:
        """Extra rows for the project panel (spec §68)."""
        engine = self.engine
        if engine is None:
            return [("Geometry", "building…" if self.project.session else "—")]
        geo = engine.geometry
        board = engine.board
        rows: list[tuple[str, str]] = []
        box = board.bounds
        if box is not None:
            rows.append(("Board size", f"{internal_to_mm(box.width):.3f} × "
                                       f"{internal_to_mm(box.height):.3f} mm"))  # fmt: skip
        area = geo.region.area_nm2
        rows.append(("Board area", f"{area / 1e12:.2f} mm²" if area else "unknown (no outline)"))
        rows.append(("Copper layers", str(len(geo.copper_layers))))
        st = board.statistics
        rows.append(("Zones / keepouts", f"{st.zone_count} / {st.keepout_count}"))
        rows.append(("Net classes", str(len(engine.ruleset.classes.classes))))
        m = engine.connectivity.metrics()
        rows.append(("Nets fully connected", str(m.get("fully_connected", 0))))
        rows.append(("Nets partially connected", str(m.get("partially_connected", 0))))
        rows.append(("Nets unrouted", str(m.get("unrouted", 0))))
        drc = engine.last_drc
        rows.append(("Internal DRC errors", str(len(drc.errors)) if drc else "not run"))
        rows.append(("Internal DRC warnings", str(len(drc.warnings)) if drc else "not run"))
        rows.append(("Spatial-index objects", f"{geo.index_entry_count:,}"))
        return rows


def _as_violation(c: Any) -> DRCViolation:
    """A marker-only violation for a candidate collision (display helper)."""
    from pcbrouter.drc.violation import Severity, ViolationKind

    return DRCViolation(
        id="candidate", kind=ViolationKind.GEOMETRY_NOTE, severity=Severity.ERROR,
        message=c.message, location=c.location,
    )  # fmt: skip
