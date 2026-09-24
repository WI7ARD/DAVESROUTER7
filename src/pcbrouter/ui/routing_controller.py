"""Stage 4+ routing in the desktop UI: route, preview, accept, reject, undo.

The router runs on a background thread (:class:`~pcbrouter.ui.workers.JobRunner`)
against the working-board engine at the moment of the request. Accepting goes
through the command bus on the GUI thread; results computed for an older working
board are re-validated by the commit (stale proposals are refused, never forced).
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QAction

from pcbrouter.board_engine import BoardEngine
from pcbrouter.commands import AcceptRouteCommand
from pcbrouter.geometry.shapes import Shape, capsule, circle
from pcbrouter.routing.backend import router_for
from pcbrouter.routing.board_router import (
    BoardRouter,
    BoardRouterSettings,
    BoardRoutingControl,
    BoardRoutingResult,
    make_plan,
)
from pcbrouter.routing.request import RouteRequest
from pcbrouter.routing.result import RouteCandidate, RouteResult
from pcbrouter.routing.working_board import Commit, WorkingBoard
from pcbrouter.ui import overlays
from pcbrouter.ui.board_job_panel import BoardJobPanel, ProgressBridge
from pcbrouter.ui.route_panel import RoutePanel
from pcbrouter.ui.workers import JobRunner

if TYPE_CHECKING:
    from pcbrouter.ui.main_window import MainWindow

log = logging.getLogger(__name__)

PREVIEW_GROUP = "route_preview"


def candidate_shapes(cand: RouteCandidate) -> list[Shape]:
    p = cand.proposal
    shapes = [capsule(s.start, s.end, s.width // 2) for s in p.segments]
    shapes += [circle(v.position, v.diameter // 2) for v in p.vias]
    return shapes


class RoutingController(QObject):
    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.w = window
        self.jobs = JobRunner(self)
        self.panel = RoutePanel()
        self.cancel_event: threading.Event | None = None
        self.last_result: RouteResult | None = None
        self._working: WorkingBoard | None = None
        self.act_route_net = QAction("Route &Selected Net", window)
        self.act_route_net.setShortcut("R")
        self.act_route_net.setStatusTip(
            "Route the selected net with the deterministic CPU router; candidates are "
            "previewed and must be accepted"
        )
        self.act_route_net.triggered.connect(self.route_selected_net)
        self.act_route_board = QAction("Route &Board", window)
        self.act_route_board.setShortcut("Ctrl+Shift+R")
        self.act_route_board.setStatusTip(
            "Route every incomplete net (ordered plan, retries, bounded rip-up) on a copy; "
            "review and accept per net"
        )
        self.act_route_board.triggered.connect(self.route_board)
        self.optimize_actions: dict[str, QAction] = {}
        from pcbrouter.routing.optimize import OptimizeGoal

        for goal, title in (
            (OptimizeGoal.SHORTER, "Shorten"),
            (OptimizeGoal.FEWER_VIAS, "Reduce Vias"),
            (OptimizeGoal.FEWER_BENDS, "Reduce Bends"),
            (OptimizeGoal.MORE_CLEARANCE, "Increase Clearance"),
            (OptimizeGoal.MERGE_COLLINEAR, "Merge Collinear Segments"),
        ):
            act = QAction(title, window)
            act.setStatusTip(f"{title}: deterministic tweak of the selected net's routed copper")
            act.triggered.connect(lambda _=False, g=goal: self.optimize_selected(g))
            self.optimize_actions[goal.value] = act
        self.board_panel = BoardJobPanel()
        self.board_control: BoardRoutingControl | None = None
        self.last_board_result: BoardRoutingResult | None = None
        self.bridge = ProgressBridge()
        self.bridge.progress.connect(self.board_panel.on_progress)
        self.board_panel.pauseRequested.connect(
            lambda: self.board_control and self.board_control.pause()
        )
        self.board_panel.resumeRequested.connect(
            lambda: self.board_control and self.board_control.resume()
        )
        self.board_panel.cancelRequested.connect(self.cancel)
        self.board_panel.acceptRequested.connect(self.accept_board)
        self.board_panel.rejectRequested.connect(self.reject_board)
        self.act_reset = QAction("Reset &Working Board…", window)
        self.act_reset.setStatusTip("Remove all routed copper (back to the source board)")
        self.act_reset.triggered.connect(self.reset_working)
        self.panel.acceptRequested.connect(self.accept)
        self.panel.rejectRequested.connect(self.reject)
        self.panel.candidateChanged.connect(self._preview)
        self.panel.cancelRequested.connect(self.cancel)

    # ------------------------------------------------------------ setup / lifecycle
    def install(self) -> None:
        w = self.w
        w._add_dock("route", "Route Review", self.panel, Qt.DockWidgetArea.RightDockWidgetArea)
        w.tabifyDockWidget(w.docks["ai"], w.docks["route"])
        w.docks["ai"].raise_()
        w._add_dock(
            "jobs", "Routing Jobs", self.board_panel, Qt.DockWidgetArea.BottomDockWidgetArea
        )
        w.tabifyDockWidget(w.docks["log"], w.docks["jobs"])
        w.docks["log"].raise_()

    @property
    def project(self) -> Any:
        return self.w.bus.context.project

    def on_board_changed(self) -> None:
        self.cancel()
        self.jobs.wait(5000)
        self.panel.set_result(None)
        self.last_result = None
        working = self.project.working
        if working is not self._working:
            self._working = working
            if working is not None:
                working.subscribe(self._on_working_changed)
        self._update_actions()

    def _update_actions(self) -> None:
        has = self.project.session is not None
        self.act_route_net.setEnabled(has)
        self.act_route_board.setEnabled(has)
        for act in self.optimize_actions.values():
            act.setEnabled(has)
        self.act_reset.setEnabled(has and bool(self._working and self._working.modified))

    def shutdown(self) -> None:
        self.cancel()
        self.jobs.wait(10_000)

    # ------------------------------------------------------------ routing
    def selected_net(self) -> str | None:
        return self.w.engine_ui._selected_net() or self.w.canvas.highlighted_net

    def route_selected_net(self) -> bool:
        net = self.selected_net()
        if net is None:
            self.w.statusBar().showMessage("Select a net (or a pad/track of it) first.", 5000)
            return False
        return self.route_net(RouteRequest(net))

    def route_net(self, request: RouteRequest) -> bool:
        engine = self.project.engine
        if engine is None:
            return False
        if self.jobs.is_running("route"):
            self.w.statusBar().showMessage("A routing job is already running.", 4000)
            return False
        self.cancel_event = threading.Event()
        cancel = self.cancel_event
        compute = self.w.compute

        def job(e: BoardEngine = engine) -> RouteResult:
            return router_for(e, compute).route_net(request, cancel=cancel)

        self.panel.set_running(
            f"Routing {request.net}… (CPU search, {request.candidates} candidate(s))"
        )
        self.overlays.clear(PREVIEW_GROUP)
        dock = self.w.docks["route"]
        dock.show()
        dock.raise_()
        self.w.engine_ui.lbl_routing.setText(f"Routing: {request.net}…")
        return self.jobs.start("route", job, self._done, self._failed)

    def cancel(self) -> None:
        if self.cancel_event is not None:
            self.cancel_event.set()
        if self.board_control is not None:
            self.board_control.cancel()

    # ------------------------------------------------------------ board routing
    def route_board(self, settings: BoardRouterSettings | None = None) -> bool:
        working = self.project.working
        if working is None or self.jobs.is_running("board"):
            return False
        control = BoardRoutingControl()
        self.board_control = control
        bridge = self.bridge
        fork_source = working

        def job() -> BoardRoutingResult:
            plan_settings = settings or BoardRouterSettings()
            plan = make_plan(fork_source, plan_settings)
            return BoardRouter(fork_source, plan_settings).run(
                plan, control, lambda info: bridge.progress.emit(info)
            )

        self.board_panel.set_running("Planning board routing…")
        dock = self.w.docks["jobs"]
        dock.show()
        dock.raise_()
        self.w.engine_ui.lbl_routing.setText("Routing: board job running…")
        return self.jobs.start("board", job, self._board_done, self._failed)

    def _board_done(self, result: object, _secs: float) -> None:
        self.w.engine_ui.lbl_routing.setText(self.w.engine_ui.routing_status())
        self.board_control = None
        if not isinstance(result, BoardRoutingResult):
            return
        self.last_board_result = result
        self.board_panel.set_result(result)
        shapes = [capsule(t.start, t.end, t.width // 2) for t in result.added_tracks]
        shapes += [circle(v.position, v.diameter // 2) for v in result.added_vias]
        from pcbrouter.routing.collision import ValidationStatus

        self.overlays.set_group(
            "board_preview", overlays.candidate_items(shapes, ValidationStatus.VALID)
        )
        self.w.statusBar().showMessage(result.summary(), 12000)

    def accept_board(self, nets: object) -> bool:
        from pcbrouter.commands import AcceptBoardRoutingCommand

        if self.last_board_result is None:
            return False
        subset = nets if isinstance(nets, set) else None
        res = self.w.bus.dispatch(AcceptBoardRoutingCommand(self.last_board_result, subset))
        self.w.statusBar().showMessage(res.message, 10000)
        if not res.success:
            self.board_panel.status.setText(f"Not accepted: {res.message}")
            return False
        self.overlays.clear("board_preview")
        self.board_panel.set_result(None)
        self.board_panel.status.setText(res.message)
        self.last_board_result = None
        self.w._update_undo_actions()
        return True

    def reject_board(self) -> None:
        self.overlays.clear("board_preview")
        self.last_board_result = None
        self.board_panel.set_result(None)
        self.board_panel.status.setText("Board routing rejected. Nothing was changed.")

    def optimize_selected(self, goal: Any) -> bool:
        from pcbrouter.commands import OptimizeNetCommand

        net = self.selected_net()
        if net is None:
            self.w.statusBar().showMessage("Select a routed net first.", 5000)
            return False
        res = self.w.bus.dispatch(OptimizeNetCommand(net, goal))
        self.w.statusBar().showMessage(res.message, 10000)
        self.w._update_undo_actions()
        return res.success

    @property
    def overlays(self) -> overlays.OverlayManager:
        return self.w.engine_ui.overlays

    def _done(self, result: object, _secs: float) -> None:
        self.w.engine_ui.lbl_routing.setText(self.w.engine_ui.routing_status())
        if not isinstance(result, RouteResult):
            return
        self.last_result = result
        self.panel.set_result(result)
        self.w.statusBar().showMessage(result.summary(), 10000)

    def _failed(self, message: str, detail: str) -> None:
        self.w.engine_ui.lbl_routing.setText(self.w.engine_ui.routing_status())
        self.panel.set_result(None)
        self.panel.status.setText(message)
        from pcbrouter.ui import dialogs

        dialogs.show_error(self.w, "Routing failed", message, detail)

    def _preview(self, cand: object) -> None:
        if not isinstance(cand, RouteCandidate):
            self.overlays.clear(PREVIEW_GROUP)
            return
        items = overlays.candidate_items(candidate_shapes(cand), cand.validation.status)
        for it in items:
            it.setToolTip(
                f"PROPOSED route {cand.proposal.net} ({cand.label}) — not on the board yet"
            )
        self.overlays.set_group(PREVIEW_GROUP, items)

    # ------------------------------------------------------------ accept / reject / undo
    def accept(self, cand: object) -> bool:
        if not isinstance(cand, RouteCandidate):
            return False
        result = self.w.bus.dispatch(AcceptRouteCommand(cand))
        if not result.success:
            self.w.statusBar().showMessage(f"Not accepted: {result.message}", 10000)
            self.panel.status.setText(f"Not accepted: {result.message}")
            return False
        self.overlays.clear(PREVIEW_GROUP)
        self.panel.set_result(None)
        self.panel.status.setText(result.message)
        self.w.statusBar().showMessage(result.message, 8000)
        self.w._update_undo_actions()
        return True

    def reject(self) -> None:
        self.overlays.clear(PREVIEW_GROUP)
        self.panel.set_result(None)
        self.panel.status.setText("Candidate rejected. Nothing was changed.")

    def reset_working(self) -> None:
        from pcbrouter.commands import ResetWorkingBoardCommand

        res = self.w.bus.dispatch(ResetWorkingBoardCommand())
        self.w.statusBar().showMessage(res.message, 8000)
        self.w._after_history_change()

    def _on_working_changed(self, working: WorkingBoard, _commit: Commit | None) -> None:
        """Called on the GUI thread after every commit/undo/redo."""
        added, removed = self.w.canvas.sync_board(working.board, frozenset(working.generated_ids()))
        log.info("canvas.sync added=%d removed=%d", added, removed)
        self.w.nets_panel.set_board(working.board)
        self.w.engine_ui.refresh_engine()
        self._update_actions()
        self.w.refresh_statistics()


__all__ = ["RoutingController", "candidate_shapes"]
