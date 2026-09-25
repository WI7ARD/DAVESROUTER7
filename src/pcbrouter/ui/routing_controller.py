"""Stage 4+ routing in the desktop UI: route, preview, accept, reject, undo.

Every routing computation (single net, whole board, AI plans, optimisation, GPU
check) runs in the routing **worker process** through
:class:`~pcbrouter.ui.route_jobs.RouteJobController`: the GUI sends a snapshot of
the working board and receives progress and the result asynchronously. The Qt
thread never routes and never waits for the worker. Accepting goes through the
command bus on the GUI thread; results computed for an older working board are
re-validated by the commit (stale proposals are refused, never forced).
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QAction

from pcbrouter.commands import AcceptRouteCommand
from pcbrouter.geometry.shapes import Shape, capsule, circle
from pcbrouter.jobs.protocol import (
    AIPlanJob,
    GpuCheckJob,
    JobDone,
    JobStatus,
    OptimizationPlan,
    OptimizeJob,
    RouteBoardJob,
    RouteNetJob,
    WorkingSnapshot,
)
from pcbrouter.routing.board_router import (
    BoardRouterSettings,
    BoardRoutingControl,
    BoardRoutingResult,
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
#: board jobs: the watchdog cancels at the routing budget plus this margin
BOARD_TIMEOUT_GRACE_S = 60.0


def optimization_between(base: Any, final: Any, report: Any) -> OptimizationPlan:
    """The copper difference base → final (an optimised fork) as one plan."""
    base_ids = {t.id for t in base.tracks} | {v.id for v in base.vias}
    final_ids = {t.id for t in final.tracks} | {v.id for v in final.vias}
    return OptimizationPlan(
        base.fingerprint,
        ", ".join(sorted(report.improved)),
        report.goal,
        [t for t in final.tracks if t.id not in base_ids],
        [v for v in final.vias if v.id not in base_ids],
        sorted(base_ids - final_ids),
        report,
        bool(report.improved),
    )


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
        self.pending_remove_ids: tuple[str, ...] = ()
        #: debug: record the cells a failed search explored (heat map / playback)
        self.record_search = False
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
        self.board_panel.pauseRequested.connect(lambda: self.route_jobs.pause())
        self.board_panel.resumeRequested.connect(lambda: self.route_jobs.resume())
        self.board_panel.cancelRequested.connect(self.cancel)
        self.board_panel.acceptRequested.connect(self.accept_board)
        self.board_panel.rejectRequested.connect(self.reject_board)
        self.act_gpu_check = QAction("Test &GPU on This Board…", window)
        self.act_gpu_check.setStatusTip(
            "Route a few incomplete nets with the CPU and with the GPU, validate every "
            "route, and compare (nothing is added to the board)"
        )
        self.act_gpu_check.triggered.connect(self.test_gpu)
        self.last_gpu_check: Any = None
        self.last_optimize: Any = None
        self.gpu_box: Any = None
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
        w.menu_tools.addAction(self.act_gpu_check)

    @property
    def project(self) -> Any:
        return self.w.bus.context.project

    def on_board_changed(self) -> None:
        self.cancel()  # a result for the previous board is discarded on arrival
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
        idle = has and not self.route_jobs.busy  # one routing job at a time
        self.act_route_net.setEnabled(idle)
        self.act_route_board.setEnabled(idle)
        self.act_gpu_check.setEnabled(idle)
        for act in self.optimize_actions.values():
            act.setEnabled(idle)
        self.act_reset.setEnabled(idle and bool(self._working and self._working.modified))

    def shutdown(self) -> None:
        self.route_jobs.shutdown()  # never waits: a running worker is stopped
        if self.gpu_box is not None:
            self.gpu_box.close()

    # ------------------------------------------------------------ worker jobs
    @property
    def route_jobs(self) -> Any:
        return self.w.route_jobs

    def _mode(self) -> str:
        return str(self.search_mode().value)

    def _submit(self, job: Any, handler: Any) -> bool:
        """Submit to the worker; the handler runs on the GUI thread when the result
        arrives, and only if the same board is still open."""
        token = self.project.working

        def on_done(done: JobDone) -> None:
            if self.project.working is not token:
                log.info("[route:%d] result discarded: the board was closed", done.job_id)
                return
            handler(done)

        if self.route_jobs.submit(job, on_done) is None:
            self.w.statusBar().showMessage(
                "A routing job is already running: wait for it or cancel it.", 5000
            )
            return False
        return True

    def _job_failed(self, done: JobDone, title: str = "Routing failed") -> None:
        self.w.engine_ui.lbl_routing.setText(self.w.engine_ui.routing_status())
        self.panel.set_result(None)
        if done.status == JobStatus.CANCELED.value:
            self.panel.status.setText("Routing canceled. Nothing was changed.")
            self.w.statusBar().showMessage("Routing canceled. Nothing was changed.", 8000)
            return
        message = done.error or f"Routing {done.status.lower()}"
        if done.status == JobStatus.TIMED_OUT.value:
            message = f"Timed out: {message}"
        self._failed(message, done.traceback)

    # ------------------------------------------------------------ routing
    def selected_net(self) -> str | None:
        return self.w.engine_ui._selected_net() or self.w.canvas.highlighted_net

    def route_selected_net(self) -> bool:
        net = self.selected_net()
        if net is None:
            self.w.statusBar().showMessage("Select a net (or a pad/track of it) first.", 5000)
            return False
        return self.route_net(self.request_for(net))

    def request_for(self, net: str) -> RouteRequest:
        """A request built from the user's routing settings (rules still decide values)."""
        from pcbrouter.domain.units import mm_to_internal
        from pcbrouter.routing.request import with_user_constraints

        st = self.w.settings
        req = RouteRequest(
            net,
            candidates=st.routing.candidates,
            time_limit_s=st.routing.time_limit_s,
            grid_resolution=mm_to_internal(st.geometry.grid_resolution_mm),
        )
        wb = self.project.working
        if wb is None:
            return req
        return with_user_constraints(
            req, wb.net_constraints.get(net), wb.locked_regions, wb.corridors
        )

    def search_mode(self) -> Any:
        from pcbrouter.routing.backend import mode_from_settings

        return mode_from_settings(self.w.compute, self.w.settings.default_compute_backend)

    def board_settings(self) -> BoardRouterSettings:
        from dataclasses import replace

        from pcbrouter.routing.board_router import Strategy

        st = self.w.settings.routing
        base = replace(self.request_for(""), candidates=1)
        return BoardRouterSettings(
            strategy=Strategy(st.strategy),
            max_passes=st.max_passes,
            allow_ripup=st.allow_ripup,
            base_request=base,
        )

    def route_net(self, request: RouteRequest, remove_ids: tuple[str, ...] = ()) -> bool:
        """Route one net in the worker. ``remove_ids``: local reroute (that generated
        copper is removed in the worker's copy before routing; applied on accept)."""
        wb = self.project.working
        if wb is None:
            return False
        if wb.is_locked("", request.net):
            self.w.statusBar().showMessage(f"Net {request.net} is locked.", 5000)
            return False
        job = RouteNetJob(
            WorkingSnapshot.from_working(wb),
            request,
            record_explored=self.record_search,
            remove_ids=tuple(remove_ids),
            mode=self._mode(),
        )
        if not self._submit(job, self._route_done):
            return False
        self.pending_remove_ids = tuple(remove_ids)
        self.panel.set_running(
            f"Routing {request.net}… ({job.mode.upper()} requested, "
            f"{request.candidates} candidate(s))"
        )
        self.overlays.clear(PREVIEW_GROUP)
        dock = self.w.docks["route"]
        dock.show()
        dock.raise_()
        self.w.engine_ui.lbl_routing.setText(f"Routing: {request.net}…")
        return True

    def _route_done(self, done: JobDone) -> None:
        if isinstance(done.value, RouteResult):
            self._done(done.value, done.elapsed_s)
        else:
            self._job_failed(done)

    def test_gpu(self) -> bool:
        """Tools ▸ Test GPU: gated CPU-vs-GPU comparison on the open board, run in
        the worker (the GPU is initialised there, never in the GUI process)."""
        from pcbrouter.routing.connectivity import NetStatus

        wb = self.project.working
        if wb is None or self.route_jobs.busy:
            return False
        nets = sorted(
            name
            for name, c in wb.engine.connectivity.nets.items()
            if c.status in (NetStatus.UNROUTED, NetStatus.PARTIALLY_CONNECTED)
        )
        if not nets:
            self.w.statusBar().showMessage("GPU check: no incomplete nets to route.", 6000)
            return False
        job = GpuCheckJob(
            WorkingSnapshot.from_working(wb), nets, self.request_for(nets[0]), mode="gpu"
        )

        def done(d: JobDone) -> None:
            if d.value is None:
                self._job_failed(d, "GPU check failed")
                return
            self._gpu_check_done(d.value, d.elapsed_s)

        if not self._submit(job, done):
            return False
        self.w.statusBar().showMessage("GPU check running…")
        return True

    def _gpu_check_done(self, result: Any, _secs: float) -> None:
        self.last_gpu_check = result
        self.w.statusBar().showMessage(result.verdict, 15000)
        self.w._update_backend_label()
        from PySide6.QtWidgets import QMessageBox

        if self.gpu_box is not None:
            self.gpu_box.close()
        box = QMessageBox(self.w)  # non-modal: routing stays usable
        box.setWindowTitle("GPU Check")
        box.setText(result.verdict)
        box.setDetailedText(result.text())
        box.setModal(False)
        box.show()
        self.gpu_box = box

    def cancel(self) -> None:
        self.route_jobs.cancel()

    # ------------------------------------------------------------ board routing
    def route_board(self, settings: BoardRouterSettings | None = None) -> bool:
        working = self.project.working
        if working is None:
            return False
        plan_settings = settings or self.board_settings()
        job = RouteBoardJob(
            WorkingSnapshot.from_working(working),
            plan_settings,
            mode=self._mode(),
            timeout_s=plan_settings.budget_s + BOARD_TIMEOUT_GRACE_S,
        )
        if not self._submit(job, self._board_job_done):
            return False
        self.board_panel.set_running("Planning board routing…")
        dock = self.w.docks["jobs"]
        dock.show()
        dock.raise_()
        self.w.engine_ui.lbl_routing.setText("Routing: board job running…")
        return True

    def _board_job_done(self, done: JobDone) -> None:
        if isinstance(done.value, BoardRoutingResult):
            self._board_done(done.value, done.elapsed_s)
            return
        self.w.engine_ui.lbl_routing.setText(self.w.engine_ui.routing_status())
        self.board_panel.set_result(None)
        if done.status == JobStatus.CANCELED.value:
            self.board_panel.status.setText("Board routing canceled. Nothing was changed.")
            return
        self.board_panel.status.setText(f"Board routing {done.status.lower()}: {done.error}")
        self._job_failed(done)

    def on_job_progress(self, progress: Any) -> None:
        """Worker progress (already throttled) → the Routing Jobs panel."""
        if progress.board_info:
            self.board_panel.on_progress(progress.board_info)

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

    # ------------------------------------------------------------ AI commands (Stage 7)
    def execute_ai_proposals(self, proposal_ids: object) -> bool:
        """Run approved AI routing command(s) through the router in the worker.
        One command → Route Review (or an optimisation); several (batch approval) →
        one board-routing job in Routing Jobs. Nothing is committed here."""
        from pcbrouter.ai.route_bridge import BridgeError, plan_from_commands
        from pcbrouter.commands.ai_commands import approved_plan

        ids = list(proposal_ids) if isinstance(proposal_ids, (list, tuple)) else []
        session = self.w.ai_service.session
        working = self.project.working
        if not ids or session is None or working is None or self.route_jobs.busy:
            return False
        try:
            if len(ids) == 1:
                plan = approved_plan(self.w.bus.context, ids[0])
            else:
                plan = plan_from_commands([session.proposals[i].current for i in ids])
        except (BridgeError, KeyError, ValueError) as exc:
            self.panel.set_result(None)
            self.panel.status.setText(f"Not run: {exc}")
            self.w.statusBar().showMessage(str(exc), 8000)
            return False
        snapshot = WorkingSnapshot.from_working(working)
        job = AIPlanJob(snapshot, plan, mode=self._mode())
        if plan.kind == "route_board":
            job.timeout_s = BoardRouterSettings().budget_s + BOARD_TIMEOUT_GRACE_S

        def done(d: JobDone) -> None:
            if d.value is None:
                self._job_failed(d)
                return
            self._ai_done(ids, d.value, snapshot.board)

        if not self._submit(job, done):
            return False
        self.panel.set_running("Running the approved AI command with the deterministic router…")
        self.w.engine_ui.lbl_routing.setText("Routing: AI command running…")
        return True

    def _ai_done(self, ids: list[str], result: object, base_board: Any = None) -> None:
        from pcbrouter.ai.route_bridge import ExecutionOutcome, router_facts
        from pcbrouter.commands.route_commands import ApplyOptimizationCommand

        self.w.engine_ui.lbl_routing.setText(self.w.engine_ui.routing_status())
        session = self.w.ai_service.session
        if not isinstance(result, ExecutionOutcome) or session is None:
            return
        facts = router_facts(result)
        for pid in ids:
            session.record_execution(pid, facts, result.summary())
        if result.board_result is not None:
            self.last_board_result = result.board_result
            self.board_panel.set_result(result.board_result)
            self.panel.set_result(None)
            self.panel.status.setText(result.board_result.summary())
            dock = self.w.docks["jobs"]
            dock.show()
            dock.raise_()
        elif result.route_results:
            best = next((r for r in result.route_results if r.best), result.route_results[0])
            self.last_result = best
            self.panel.set_result(best)
        elif result.optimize_reports:
            # the user approved this tweak: apply it as one validated, undoable commit
            rep = result.optimize_reports[0]
            if rep.improved and base_board is not None and result.optimized_board is not None:
                plan = optimization_between(base_board, result.optimized_board, rep)
                res = self.w.bus.dispatch(ApplyOptimizationCommand(plan))
                self.w.statusBar().showMessage(res.message, 10000)
                self.w._update_undo_actions()
            self.panel.set_result(None)
            self.panel.status.setText(
                f"AI tweak ({rep.goal.value}): {len(rep.improved)} improved, "
                f"{len(rep.unchanged)} unchanged, {len(rep.skipped)} skipped"
            )
        self.w.ai_panel._refresh_proposals()
        self.w.ai_history.refresh()

    def optimize_selected(self, goal: Any) -> bool:
        """Optimise the selected net in the worker; applied as one validated commit
        when the result arrives (refused if the board changed meanwhile)."""
        net = self.selected_net()
        wb = self.project.working
        if net is None or wb is None:
            self.w.statusBar().showMessage("Select a routed net first.", 5000)
            return False
        job = OptimizeJob(WorkingSnapshot.from_working(wb), net, goal, mode=self._mode())
        if not self._submit(job, self._optimize_done):
            return False
        self.w.statusBar().showMessage(f"Optimising {net}…")
        return True

    def _optimize_done(self, done: JobDone) -> None:
        from pcbrouter.commands.route_commands import ApplyOptimizationCommand

        if not isinstance(done.value, OptimizationPlan):
            self._job_failed(done, "Optimisation failed")
            return
        res = self.w.bus.dispatch(ApplyOptimizationCommand(done.value))
        self.last_optimize = res
        self.w.statusBar().showMessage(res.message, 10000)
        self.w._update_undo_actions()

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
        if result.explored is not None:
            self.w.workbench.show_explored(result.explored)
        if result.best is not None:
            self.w.workbench.compute_diff(result.best, self.pending_remove_ids)

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
        result = self.w.bus.dispatch(AcceptRouteCommand(cand, remove_ids=self.pending_remove_ids))
        if result.success:
            self.pending_remove_ids = ()
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
        self.pending_remove_ids = ()
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
        session = self.w.ai_service.session
        if session is not None:
            expired = session.update_board(working.board)
            if expired:
                self.w.statusBar().showMessage(
                    f"{expired} open AI proposal(s) expired: the working board changed.", 6000
                )
            self.w.ai_panel.refresh_board()
        self.w.engine_ui.refresh_engine()
        self._update_actions()
        self.w.refresh_statistics()
        self.w.workbench.on_working_changed()


__all__ = ["RoutingController", "candidate_shapes"]
