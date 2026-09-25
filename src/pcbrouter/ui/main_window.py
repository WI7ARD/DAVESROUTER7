"""Main engineering-workstation window.

The window is glue: it turns user gestures into commands on the
:class:`~pcbrouter.commands.CommandBus` and forwards results to the panels. It never
parses files or touches routing internals directly.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QToolBar,
    QWidget,
)

from pcbrouter import APP_NAME, STAGE, __version__
from pcbrouter.ai.service import AIService, runtime_config_from_settings
from pcbrouter.ai.session import AISession
from pcbrouter.commands import CloseBoardCommand, CommandBus, OpenBoardCommand
from pcbrouter.compute.detection import GpuDetectionResult
from pcbrouter.compute.manager import ComputeManager
from pcbrouter.kicad.adapter import WarningSeverity
from pcbrouter.project.manager import ProjectSession
from pcbrouter.settings.settings import AppSettings, ComputeBackendChoice, SettingsStore
from pcbrouter.ui import dialogs
from pcbrouter.ui.ai_controller import AIRequestController
from pcbrouter.ui.ai_dialogs import UsageDialog
from pcbrouter.ui.ai_history_panel import AIHistoryPanel
from pcbrouter.ui.ai_panel import AIEngineeringPanel
from pcbrouter.ui.export_controller import ExportController
from pcbrouter.ui.geometry_controller import GeometryController
from pcbrouter.ui.inspector_panel import InspectorPanel
from pcbrouter.ui.layers_panel import LayersPanel
from pcbrouter.ui.log_panel import LogPanel
from pcbrouter.ui.nets_panel import NetsPanel
from pcbrouter.ui.pcb_canvas import ItemKind, PcbCanvas
from pcbrouter.ui.project_panel import ProjectPanel
from pcbrouter.ui.route_jobs import JobState, RouteJobController
from pcbrouter.ui.routing_controller import RoutingController
from pcbrouter.ui.routing_overlay import RoutingOverlay
from pcbrouter.ui.settings_dialog import SettingsDialog
from pcbrouter.ui.theme import apply_theme
from pcbrouter.ui.workbench import WorkbenchController
from pcbrouter.ui.workers import JobRunner

log = logging.getLogger(__name__)

BOARD_FILE_FILTER = "KiCad PCB (*.kicad_pcb);;All files (*)"
_PANELS = (
    "project", "layers", "inspector", "nets", "log", "ai", "ai_history", "drc", "rules", "route",
    "jobs",
)  # fmt: skip
AI_SETTINGS_TAB = 3


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        bus: CommandBus,
        compute: ComputeManager,
        settings: AppSettings,
        settings_store: SettingsStore,
        log_panel: LogPanel | None = None,
    ) -> None:
        super().__init__()
        #: GPU hardware probe, run in the background (never on the GUI thread);
        #: set first: status-bar code reads it while the window is being built
        self.gpu_probe: Any = None
        self.bus = bus
        self.compute = compute
        self.settings = settings
        self.settings_store = settings_store
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1400, 900)
        self.setDockNestingEnabled(True)

        self.canvas = PcbCanvas()
        self.setCentralWidget(self.canvas)
        self.project_panel = ProjectPanel()
        self.layers_panel = LayersPanel()
        self.inspector = InspectorPanel()
        self.nets_panel = NetsPanel()
        self.log_panel = log_panel or LogPanel()

        # AI engineering layer (no network activity until the user explicitly asks).
        ai = bus.context.ai
        self.ai_service = ai if ai is not None else AIService()
        if bus.context.ai is None:
            bus.context.ai = self.ai_service
        self.ai_controller = AIRequestController(self.ai_service, self)
        self._selected: tuple[ItemKind, str] | None = None
        self.ai_panel = AIEngineeringPanel(self.ai_controller, bus, settings, self._ai_selection)
        self.ai_history = AIHistoryPanel(self.ai_service, bus.context.history)

        self.docks: dict[str, QDockWidget] = {}
        self._add_dock(
            "project", "Project", self.project_panel, Qt.DockWidgetArea.LeftDockWidgetArea
        )
        self._add_dock("layers", "Layers", self.layers_panel, Qt.DockWidgetArea.LeftDockWidgetArea)
        self._add_dock(
            "inspector", "Inspector", self.inspector, Qt.DockWidgetArea.RightDockWidgetArea
        )
        self._add_dock("nets", "Nets", self.nets_panel, Qt.DockWidgetArea.RightDockWidgetArea)
        self._add_dock("log", "Log", self.log_panel, Qt.DockWidgetArea.BottomDockWidgetArea)
        self._add_dock("ai", "AI Engineering", self.ai_panel, Qt.DockWidgetArea.RightDockWidgetArea)
        self._add_dock(
            "ai_history", "AI History", self.ai_history, Qt.DockWidgetArea.BottomDockWidgetArea
        )
        # Right column: Inspector/Nets tabbed on top, AI Engineering below with more height.
        self.tabifyDockWidget(self.docks["inspector"], self.docks["nets"])
        self.splitDockWidget(self.docks["inspector"], self.docks["ai"], Qt.Orientation.Vertical)
        self.resizeDocks(
            [self.docks["inspector"], self.docks["ai"]], [330, 600], Qt.Orientation.Vertical
        )
        self.tabifyDockWidget(self.docks["log"], self.docks["ai_history"])
        self.docks["inspector"].raise_()
        self.docks["log"].raise_()
        self.resizeDocks(
            [self.docks["project"], self.docks["inspector"]], [300, 360], Qt.Orientation.Horizontal
        )
        self.resizeDocks([self.docks["log"]], [150], Qt.Orientation.Vertical)

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._build_status_bar()
        # Stage 3: geometry + rule engine UI (docks, tools, overlays, status fields).
        self.engine_ui = GeometryController(self)
        self.engine_ui.install(self.menu_view, self.menu_tools, self.menu_help, self.toolbar_main)
        # Routing runs in a worker process; the GUI only submits jobs and displays
        # state (see ui/route_jobs.py). The overlay shows phase, backend, progress.
        self.route_jobs = RouteJobController(self)
        self.routing_overlay = RoutingOverlay(self.canvas)
        self._bg_jobs = JobRunner(self)
        # Stage 4: routing (route, preview, accept/reject, undo via the history stack).
        self.routing_ui = RoutingController(self)
        self.routing_ui.install()
        self.act_route_net = self.routing_ui.act_route_net
        self.act_route_board = self.routing_ui.act_route_board
        self.menu_router.addAction(self.act_route_net)
        self.menu_router.addAction(self.act_route_board)
        self.menu_router.addAction(self.routing_ui.act_route_freerouting)
        tweak = self.menu_router.addMenu("&Optimize Selected Net")
        for act in self.routing_ui.optimize_actions.values():
            tweak.addAction(act)
        self.menu_router.addSeparator()
        self.menu_router.addAction(self.routing_ui.act_reset)
        # Stage 8: workbench (view modes, locks, constraints, reroute, inspector, debug).
        self.workbench = WorkbenchController(self)
        self.workbench.install(self.menu_view, self.menu_router, self.menu_edit)
        # Stage 9: export, sessions, crash recovery, diagnostic bundle.
        self.export_ui = ExportController(self)
        self.export_ui.install(self.menu_file, self.menu_help, self.act_settings)
        self.menu_view.addAction(self.docks["route"].toggleViewAction())
        self.menu_view.addAction(self.docks["jobs"].toggleViewAction())
        self.toolbar_main.addAction(self.act_route_net)
        self._connect_signals()
        self._apply_viewer_settings()
        self._restore_window_state()
        self._refresh_board_state()

    # ================================================================ layout
    def _add_dock(self, key: str, title: str, widget: QWidget, area: Qt.DockWidgetArea) -> None:
        dock = QDockWidget(title, self)
        dock.setObjectName(f"dock_{key}")
        dock.setWidget(widget)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.addDockWidget(area, dock)
        self.docks[key] = dock

    def _action(
        self,
        text: str,
        slot: object,
        shortcut: str | QKeySequence | None = None,
        tip: str | None = None,
        checkable: bool = False,
    ) -> QAction:
        act = QAction(text, self)
        if shortcut is not None:
            act.setShortcut(QKeySequence(shortcut))
        if tip:
            act.setStatusTip(tip)
            act.setToolTip(tip + (f" ({act.shortcut().toString()})" if shortcut else ""))
        act.setCheckable(checkable)
        if checkable:
            act.toggled.connect(slot)
        else:
            act.triggered.connect(slot)
        return act

    def _build_actions(self) -> None:
        self.act_open = self._action(
            "&Open Board…", self.open_board_dialog, "Ctrl+O", "Open a .kicad_pcb (read-only)"
        )
        self.act_close = self._action(
            "&Close Board",
            self.close_board,
            "Ctrl+W",
            "Close the board (the file is never modified)",
        )
        self.act_settings = self._action(
            "&Settings…", self.open_settings, "Ctrl+,", "Application settings"
        )
        self.act_exit = self._action("E&xit", self.close, "Ctrl+Q", "Quit")
        self.act_fit = self._action(
            "Zoom to &Fit", self.canvas.zoom_to_fit, "F", "Fit the whole board in view"
        )
        self.act_zoom_in = self._action(
            "Zoom &In", lambda: self.canvas.zoom_by(1.25), "Ctrl+=", "Zoom in"
        )
        self.act_zoom_out = self._action(
            "Zoom &Out", lambda: self.canvas.zoom_by(0.8), "Ctrl+-", "Zoom out"
        )
        self.act_grid = self._action(
            "&Grid", self._on_grid_toggled, "G", "Toggle the grid", checkable=True
        )
        self.act_compute = self._action(
            "&Compute Backend Information…",
            lambda: dialogs.show_compute_info(self, self.compute),
            tip="CPU/GPU detection results",
        )
        self.act_ai = self._action(
            "Configure &AI Providers…",
            self.open_ai_settings,
            tip="OpenAI, Anthropic and "
            "OpenAI-compatible provider profiles, API keys and AI preferences",
        )
        self.act_ai_panel = self._action(
            "AI &Engineering Panel", self.show_ai_panel, "Ctrl+I", "Open the AI Engineering panel"
        )
        self.act_ai_usage = self._action(
            "AI &Usage…", self.show_ai_usage, tip="Requests and token usage in this session"
        )
        self.act_ai_export = self._action(
            "&Export AI Session…",
            self.export_ai_session,
            tip="Save prompts, analyses, proposals and decisions " "as JSON (no credentials)",
        )
        self.act_undo = self._action(
            "&Undo",
            self.undo,
            "Ctrl+Z",
            "Undo the last AI proposal decision (never touches geometry)",
        )
        self.act_redo = self._action("&Redo", self.redo, "Ctrl+Shift+Z", "Redo")
        self.act_about = self._action(f"&About {APP_NAME}", lambda: dialogs.show_about(self))

    def _build_menus(self) -> None:
        mb = self.menuBar()
        file_menu = mb.addMenu("&File")
        file_menu.addAction(self.act_open)
        self.recent_menu = QMenu("Recent &Boards", self)
        file_menu.addMenu(self.recent_menu)
        file_menu.addAction(self.act_close)
        file_menu.addSeparator()
        file_menu.addAction(self.act_settings)
        self.menu_file = file_menu
        file_menu.addSeparator()
        file_menu.addAction(self.act_exit)
        self._rebuild_recent_menu()

        edit = mb.addMenu("&Edit")
        edit.addAction(self.act_undo)
        edit.addAction(self.act_redo)
        self.menu_edit = edit

        view = mb.addMenu("&View")
        view.addAction(self.act_fit)
        view.addAction(self.act_zoom_in)
        view.addAction(self.act_zoom_out)
        view.addAction(self.act_grid)
        view.addSeparator()
        for key, title in (
            ("layers", "&Layers"),
            ("nets", "&Nets"),
            ("log", "L&ogs"),
            ("project", "&Project"),
            ("inspector", "&Inspector"),
            ("ai", "&AI Engineering"),
            ("ai_history", "AI &History"),
        ):
            act = self.docks[key].toggleViewAction()
            act.setText(title)
            view.addAction(act)

        tools = mb.addMenu("&Tools")
        tools.addAction(self.act_compute)
        act_gpu_setup = QAction("Set Up &GPU…", self)
        act_gpu_setup.setStatusTip(
            "Detect your GPU, install the right library, switch routing to GPU"
        )
        act_gpu_setup.triggered.connect(self.open_gpu_setup)
        tools.addAction(act_gpu_setup)
        self.act_gpu_setup = act_gpu_setup
        act_fr_setup = QAction("Set Up &Freerouting…", self)
        act_fr_setup.setStatusTip(
            "Find KiCad and Freerouting (the open-source autorouter) and test them"
        )
        act_fr_setup.triggered.connect(self.open_freerouting_setup)
        tools.addAction(act_fr_setup)
        self.act_freerouting_setup = act_fr_setup
        self.menu_view = view
        self.menu_tools = tools
        ai = mb.addMenu("&AI")
        ai.addAction(self.act_ai_panel)
        ai.addAction(self.act_ai)
        act_ollama = QAction("Set Up &Local AI (Ollama)…", self)
        act_ollama.setStatusTip("Free AI on this computer: no API key, nothing sent online")
        act_ollama.triggered.connect(self.open_ollama_setup)
        ai.addAction(act_ollama)
        self.act_ollama = act_ollama
        ai.addSeparator()
        ai.addAction(self.act_ai_usage)
        ai.addAction(self.act_ai_export)
        router = mb.addMenu("&Router")
        self.menu_router = router
        help_menu = mb.addMenu("&Help")
        act_guides = QAction("&Guides…", self)
        act_guides.setShortcut(QKeySequence("F1"))
        act_guides.setStatusTip("Step-by-step guides for every feature")
        act_guides.triggered.connect(lambda: self.open_guides())
        help_menu.addAction(act_guides)
        self.act_guides = act_guides
        help_menu.addSeparator()
        help_menu.addAction(self.act_about)
        self.menu_help = help_menu

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setObjectName("toolbar_main")
        tb.setMovable(False)
        tb.addAction(self.act_open)
        tb.addAction(self.act_close)
        tb.addSeparator()
        tb.addAction(self.act_fit)
        tb.addAction(self.act_grid)
        tb.addSeparator()
        stage = QLabel(
            f"  Stage {STAGE} · source file read-only · routing on a working copy · "
            "every route validated and accepted by you  "
        )
        stage.setProperty("role", "muted")
        tb.addWidget(stage)
        self.addToolBar(tb)
        self.toolbar_main = tb

    def _build_status_bar(self) -> None:
        sb = self.statusBar()
        self.lbl_board = QLabel("No board")
        self.lbl_cursor = QLabel("X —  Y —")
        self.lbl_zoom = QLabel("Zoom —")
        self.lbl_layer = QLabel("Layer —")
        self.lbl_backend = QLabel("")
        self.lbl_counts = QLabel("")
        self.lbl_ai = QLabel("AI: not configured")
        self.lbl_ai.setToolTip("Last known AI provider status (no background pinging)")
        self.lbl_cursor.setMinimumWidth(170)
        self.lbl_board.setToolTip("Open board (read-only)")
        self.lbl_cursor.setToolTip("Cursor position in board millimetres (KiCad coordinates)")
        self.lbl_zoom.setToolTip("Screen pixels per board millimetre")
        for w in (
            self.lbl_ai,
            self.lbl_board,
            self.lbl_counts,
            self.lbl_layer,
            self.lbl_backend,
            self.lbl_zoom,
            self.lbl_cursor,
        ):
            sb.addPermanentWidget(w)
        self._update_backend_label()

    def _connect_signals(self) -> None:
        rj, ov = self.route_jobs, self.routing_overlay
        rj.jobStarted.connect(ov.show_starting)
        rj.progress.connect(ov.update_progress)
        rj.progress.connect(self.routing_ui.on_job_progress)
        rj.heartbeat.connect(lambda _hb: ov.set_heartbeat_age(rj.seconds_since_progress))
        rj.stateChanged.connect(self._on_route_job_state)
        rj.notice.connect(self._on_route_notice)
        ov.cancel_button.clicked.connect(self.routing_ui.cancel)
        c = self.canvas
        c.cursorMoved.connect(self._on_cursor)
        c.zoomChanged.connect(lambda z: self.lbl_zoom.setText(f"Zoom {z:.2f} px/mm"))
        c.objectSelected.connect(self._on_object_selected)
        c.selectionCleared.connect(self.inspector.clear)

        lp = self.layers_panel
        lp.layerVisibilityChanged.connect(c.set_layer_visible)
        lp.activeLayerChanged.connect(self._on_active_layer)
        lp.footprintsVisibilityChanged.connect(c.set_footprints_visible)
        lp.labelsVisibilityChanged.connect(c.set_labels_visible)

        np_ = self.nets_panel
        np_.netSelected.connect(self._on_net_selected)
        np_.hideRequested.connect(lambda n: self._net_view(c.hide_net, n))
        np_.showRequested.connect(lambda n: self._net_view(c.show_net, n))
        np_.isolateRequested.connect(lambda n: self._net_view(c.isolate_net, n))
        np_.clearIsolationRequested.connect(lambda: self._net_view(c.isolate_net, None))
        np_.showAllRequested.connect(lambda: self._net_view(lambda _n: c.show_all_nets(), None))

        self.project_panel.componentActivated.connect(self._on_component_activated)

        self.ai_panel.configureRequested.connect(self.open_ai_settings)
        self.ai_panel.runRequested.connect(self.routing_ui.execute_ai_proposals)
        self.ai_panel.targetNetsChanged.connect(self._on_ai_targets)
        self.ai_panel.historyChanged.connect(self.ai_history.refresh)
        self.ai_panel.historyChanged.connect(self._update_undo_actions)
        self.ai_panel.statusIndicatorChanged.connect(self._on_ai_indicator)
        self.ai_history.proposalActivated.connect(self.ai_panel.select_proposal)
        self.ai_controller.modelsListed.connect(self.ai_panel.on_models_listed)

    # ================================================================ board lifecycle
    def open_board_dialog(self) -> None:
        start = self.settings.last_open_directory or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, "Open KiCad board (read-only)", start, BOARD_FILE_FILTER
        )
        if path:
            self.open_board(Path(path))

    def open_board(self, path: Path) -> bool:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.statusBar().showMessage(f"Loading {path.name}…")
        try:
            result = self.bus.dispatch(OpenBoardCommand(path))
        finally:
            QApplication.restoreOverrideCursor()
        if not result.success:
            self.statusBar().showMessage(f"Failed to open {path.name}", 8000)
            dialogs.show_error(self, "Could not open board", result.message, result.error_detail)
            return False
        session: ProjectSession = result.data
        self.settings.add_recent_board(session.source_path)
        self.settings.last_open_directory = str(session.source_path.parent)
        self._rebuild_recent_menu()
        self._refresh_board_state()
        self._start_ai_session()
        notes = session.load_result.warnings
        n_warn = sum(1 for w in notes if w.severity is WarningSeverity.WARNING)
        suffix = f" — {n_warn} warning(s), see Project ▸ Load notes" if n_warn else ""
        self.statusBar().showMessage(result.message + suffix, 10000)
        return True

    def close_board(self) -> None:
        if not self.bus.context.project.is_open:
            self.statusBar().showMessage("No board is open.", 4000)
            return
        self.export_ui.clean_close()
        self._end_ai_session()
        result = self.bus.dispatch(CloseBoardCommand())
        self._refresh_board_state()
        self.statusBar().showMessage(result.message, 8000)

    def _refresh_board_state(self) -> None:
        session = self.bus.context.project.session
        board = session.board if session else None
        self.canvas.set_board(board)
        self.inspector.clear()
        self.nets_panel.set_board(board)
        v = self.settings.viewer
        self.layers_panel.set_board(
            board,
            self.canvas.active_layer,
            show_footprints=v.show_footprint_bodies,
            show_labels=v.show_reference_labels,
        )
        self.project_panel.set_session(session, self.canvas.render_stats if board else None)
        self.engine_ui.on_board_changed()
        self.routing_ui.on_board_changed()
        self.workbench.on_board_changed()
        self.export_ui.on_board_changed()
        self.refresh_statistics()
        self._apply_viewer_settings()
        has = board is not None
        self.act_close.setEnabled(has)
        self.act_fit.setEnabled(has)
        if session is None or board is None:
            self.lbl_board.setText("No board")
            self.lbl_counts.setText("")
            self.lbl_layer.setText("Layer —")
            self.lbl_zoom.setText("Zoom —")
            self.setWindowTitle(f"{APP_NAME} {__version__}")
            return
        s = board.statistics
        self.lbl_board.setText(f"{session.name} (read-only)")
        self.lbl_counts.setText(
            f"{s.footprint_count} comp · {s.pad_count} pads · "
            f"{s.net_count} nets · {s.track_count} trk · {s.via_count} via"
        )
        self.lbl_layer.setText(f"Layer {self.canvas.active_layer or '—'}")
        self.setWindowTitle(f"{session.name} — {APP_NAME} {__version__}")

    def refresh_statistics(self) -> None:
        """Stage 3 engine statistics in the project panel (once the engine is ready)."""
        if self.bus.context.project.session is not None:
            self.project_panel.set_geometry_stats(self.engine_ui.statistics())

    def _rebuild_recent_menu(self) -> None:
        self.recent_menu.clear()
        if not self.settings.recent_boards:
            empty = self.recent_menu.addAction("(no recent boards)")
            empty.setEnabled(False)
            return
        for i, path in enumerate(self.settings.recent_boards, start=1):
            act = self.recent_menu.addAction(f"&{i} {Path(path).name}")
            act.setToolTip(path)
            act.setStatusTip(path)
            act.triggered.connect(lambda _=False, p=path: self._open_recent(p))

    def _open_recent(self, path: str) -> None:
        if not Path(path).exists():
            dialogs.show_error(
                self,
                "Board not found",
                f"The recent board no longer exists:\n{path}\n\n"
                "It has been removed from the recent list.",
            )
            self.settings.remove_recent_board(path)
            self._rebuild_recent_menu()
            return
        self.open_board(Path(path))

    # ================================================================ settings
    def open_settings(self, tab: int | None = None) -> None:
        dlg = SettingsDialog(self.settings, self.compute, self, ai_controller=self.ai_controller)
        if tab is not None:
            dlg.tabs.setCurrentIndex(tab)
        if not self.run_settings_dialog(dlg):
            self.ai_panel.refresh_profiles()
            return
        new = dlg.result_settings()
        new.window = self.settings.window
        old_config = runtime_config_from_settings(self.settings.ai)
        self.settings = new
        self.ai_panel.settings = new
        self.ai_panel.refresh_profiles()
        if runtime_config_from_settings(new.ai) != old_config and self.ai_service.session:
            self._start_ai_session()
            self.statusBar().showMessage(
                "AI preferences changed: the AI conversation for this " "board was restarted.", 8000
            )
        app = QApplication.instance()
        if isinstance(app, QApplication):
            apply_theme(app, new.theme)
        self._apply_viewer_settings()
        self._rebuild_recent_menu()
        self.engine_ui.apply_settings()
        self.export_ui.apply_settings()
        self.save_settings()
        self.statusBar().showMessage("Settings saved.", 4000)

    def _apply_viewer_settings(self) -> None:
        v = self.settings.viewer
        self.canvas.set_grid(v.grid_visible, v.grid_spacing_mm)
        self.canvas.set_labels_visible(v.show_reference_labels)
        self.canvas.set_footprints_visible(v.show_footprint_bodies)
        self.act_grid.blockSignals(True)
        self.act_grid.setChecked(v.grid_visible)
        self.act_grid.blockSignals(False)

    def _on_grid_toggled(self, checked: bool) -> None:
        self.settings.viewer.grid_visible = checked
        self.canvas.set_grid(checked)

    def save_settings(self) -> None:
        w = self.settings.window
        w.geometry_b64 = base64.b64encode(bytes(self.saveGeometry().data())).decode("ascii")
        w.state_b64 = base64.b64encode(bytes(self.saveState().data())).decode("ascii")
        w.panel_visibility = {k: not d.isHidden() for k, d in self.docks.items()}
        try:
            self.settings_store.save(self.settings)
        except OSError as exc:
            log.error("settings.save_failed error=%s", exc)
            dialogs.show_error(
                self,
                "Settings not saved",
                f"Could not save settings to {self.settings_store.path}.",
                str(exc),
            )

    def _restore_window_state(self) -> None:
        w = self.settings.window
        try:
            if w.geometry_b64:
                self.restoreGeometry(QByteArray(base64.b64decode(w.geometry_b64)))
            if w.state_b64:
                self.restoreState(QByteArray(base64.b64decode(w.state_b64)))
        except (ValueError, TypeError) as exc:
            log.warning("settings.window_state_invalid error=%s", exc)
        for key, visible in w.panel_visibility.items():
            if key in self.docks:
                self.docks[key].setVisible(visible)

    # ================================================================ slots
    def _on_cursor(self, x: float, y: float) -> None:
        self.lbl_cursor.setText(f"X {x:9.4f}  Y {y:9.4f} mm")

    def _on_active_layer(self, layer: str) -> None:
        self.canvas.set_active_layer(layer)
        self.lbl_layer.setText(f"Layer {layer}")

    def _on_object_selected(self, kind: ItemKind, obj_id: str) -> None:
        self._selected = (kind, obj_id)
        board = self.canvas.board
        if board is not None:
            self.inspector.show_object(board, kind, obj_id)
            self.workbench.on_object_selected(kind, obj_id)  # Stage 8 route facts

    def _on_net_selected(self, name: str) -> None:
        board = self.canvas.board
        if board is None:
            return
        self.canvas.select_object(ItemKind.NET, name)
        self.inspector.show_object(board, ItemKind.NET, name)

    def _on_component_activated(self, comp_id: str) -> None:
        board = self.canvas.board
        if board is None:
            return
        self.canvas.select_object(ItemKind.COMPONENT, comp_id, center=True)
        self.inspector.show_object(board, ItemKind.COMPONENT, comp_id)

    def _net_view(self, op: object, name: str | None) -> None:
        if callable(op):
            op(name)
        self.nets_panel.set_view_state(self.canvas.hidden_nets, self.canvas.isolated_net)

    def on_gpu_detected(self, result: GpuDetectionResult) -> None:
        self.compute.update_gpu_detection(result)
        self._update_backend_label()
        if self.gpu_probe is None:
            self.start_gpu_probe()

    def _on_route_notice(self, message: str) -> None:
        self.statusBar().showMessage(message, 20000)
        self.routing_overlay.detail.setText(message)

    def _on_route_job_state(self, state: str) -> None:
        if state == JobState.CANCELING.value:
            self.routing_overlay.show_canceling()
        elif state == JobState.IDLE.value:
            self.routing_overlay.finish()
            self.engine_ui.lbl_routing.setText(self.engine_ui.routing_status())
        busy = state != JobState.IDLE.value
        self.routing_ui._update_actions()
        self.export_ui.update_actions()
        self.ai_panel.setProperty("routingBusy", busy)

    def open_guides(self, topic: str = "start") -> None:
        from pcbrouter.ui.guides import GuideDialog

        dlg = getattr(self, "_guide_dialog", None)
        if dlg is None:
            dlg = GuideDialog(self, topic)
            dlg.setModal(False)
            self._guide_dialog = dlg
        else:
            dlg.show_topic(topic)
        dlg.show()
        dlg.raise_()

    # guide "do it" buttons that need a board or a small precondition message
    def _guide_route_board(self) -> None:
        if self.bus.context.project.session is None:
            self.statusBar().showMessage("Open a board first (File ▸ Open Board…).", 6000)
            return
        self.routing_ui.route_board()

    def _guide_freeroute(self) -> None:
        if self.bus.context.project.session is None:
            self.statusBar().showMessage("Open a board first (File ▸ Open Board…).", 6000)
            return
        self.routing_ui.route_board_freerouting()

    def _guide_export(self) -> None:
        if self.bus.context.project.session is None:
            self.statusBar().showMessage("Open and route a board first.", 6000)
            return
        self.export_ui.export_routed()

    def _guide_bundle(self) -> None:
        self.export_ui.export_bundle()

    def open_ollama_setup(self) -> None:
        from pcbrouter.ui.ollama_dialog import OllamaDialog

        dlg = OllamaDialog(self)
        dlg.setModal(False)
        dlg.show()
        self._ollama_dialog = dlg

    def open_freerouting_setup(self) -> None:
        from pcbrouter.ui.freerouting_dialog import FreeroutingDialog

        dlg = FreeroutingDialog(self)
        dlg.setModal(False)
        dlg.show()
        self._freerouting_dialog = dlg

    def open_gpu_setup(self) -> None:
        from pcbrouter.ui.gpu_setup_dialog import GpuSetupDialog

        dlg = GpuSetupDialog(self)
        dlg.setModal(False)
        dlg.show()
        self._gpu_setup_dialog = dlg

    def start_gpu_probe(self) -> None:
        """Probe for a GPU device in the background (nvidia-smi/PowerShell can take
        seconds); the status bar says "checking…" until the answer arrives."""
        from pcbrouter.compute.probe import probe_gpu_light

        def done(result: object, _secs: float) -> None:
            self.gpu_probe = result
            self.compute.probe_result = result
            self._update_backend_label()
            self.engine_ui.lbl_routing.setText(self.engine_ui.routing_status())

        self._bg_jobs.start("gpu-probe", probe_gpu_light, done, lambda _m, _d: None)

    def _update_backend_label(self) -> None:
        det = self.compute.gpu.detection
        gpu_text = det.status.short_label
        gate_note = ""
        if self.settings.default_compute_backend is not ComputeBackendChoice.CPU:
            # a GPU mode is selected: the (background) hardware probe decides
            gate = getattr(self, "gpu_probe", None)
            if gate is None:
                gpu_text = "checking…"
                gate_note = "\nChecking for a GPU device…"
            elif not gate.available:
                gpu_text = "SKIPPED"
                gate_note = f"\nGPU routing SKIPPED ({gate.reason}); searches run on the CPU."
            else:
                gpu_text = f"{gate.library} found"
                gate_note = (
                    "\nThe GPU is initialised in the routing worker on the first job; the "
                    "routing card shows which backend each job actually used."
                )
        self.lbl_backend.setText(f"{self.compute.active.name} · GPU: {gpu_text}")
        self.lbl_backend.setToolTip(
            f"Active compute backend: {self.compute.active.name}\nGPU: {det.summary()}"
            f"{gate_note}\nDetails: Tools ▸ Compute Backend Information"
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        self.ai_controller.cancel()
        self.routing_ui.shutdown()  # stops a running routing worker; never waits
        self.routing_overlay.finish()
        self.workbench.shutdown()
        self.engine_ui.shutdown()  # finish background work, close tool dialogs
        self.save_settings()
        if self.bus.context.project.is_open:
            self.export_ui.clean_close()
            self._end_ai_session()
            self.bus.dispatch(CloseBoardCommand())
        event.accept()

    # ================================================================ AI
    def run_settings_dialog(self, dlg: SettingsDialog) -> bool:
        """Separate method so tests can drive the dialog."""
        return dlg.exec() == SettingsDialog.DialogCode.Accepted

    def run_dialog(self, dlg: QDialog) -> int:
        """Run a modal dialog (separate method so tests can drive it)."""
        return dlg.exec()

    def open_ai_settings(self) -> None:
        self.open_settings(tab=AI_SETTINGS_TAB)

    def show_ai_panel(self) -> None:
        dock = self.docks["ai"]
        dock.show()
        dock.raise_()
        self.ai_panel.prompt.setFocus()

    def _start_ai_session(self) -> None:
        self.ai_controller.cancel()
        project = self.bus.context.project.session
        if project is None:
            return
        manager = self.bus.context.project
        self.ai_service.start_session(
            project.board,
            project.session_id,
            runtime_config_from_settings(self.settings.ai),
            history=self.bus.context.history,
            engine_provider=lambda: manager.engine,
            on_constraints_changed=self.engine_ui.on_ai_constraints_changed,
        )
        self._refresh_ai()

    def _end_ai_session(self) -> None:
        self.ai_controller.cancel()
        session = self.ai_service.session
        project = self.bus.context.project.session
        if (
            session is not None
            and project is not None
            and session.interactions
            and self.settings.ai.save_conversation_history
        ):
            self._autosave_ai_session(session, project)
        self.ai_service.end_session("board closed")
        self._refresh_ai()

    def _autosave_ai_session(self, session: AISession, project: ProjectSession) -> None:
        folder = project.workspace.root / "ai_sessions"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{session.session_id}.json"
            target.write_text(json.dumps(session.export(project.name), indent=2), encoding="utf-8")
            log.info("ai.session.saved path=%s", target)
        except OSError as exc:
            log.warning("ai.session.save_failed error=%s", exc)

    def _refresh_ai(self) -> None:
        self.ai_panel.refresh_board()
        self.ai_panel.refresh_status()
        self.ai_history.refresh()
        self._update_undo_actions()

    def _ai_selection(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        board = self.canvas.board
        if board is None or self._selected is None:
            return (), ()
        kind, obj_id = self._selected
        idx = board.index
        if kind is ItemKind.NET:
            return (obj_id,), ()
        if kind is ItemKind.COMPONENT and obj_id in idx.components_by_id:
            return (), (idx.components_by_id[obj_id].reference,)
        if kind is ItemKind.PAD and obj_id in idx.pads_by_id:
            pad = idx.pads_by_id[obj_id]
            return ((pad.net_name,) if pad.net_name else ()), (pad.footprint_ref,)
        if kind is ItemKind.TRACK and obj_id in idx.tracks_by_id:
            net = idx.tracks_by_id[obj_id].net_name
            return ((net,) if net else ()), ()
        if kind is ItemKind.VIA and obj_id in idx.vias_by_id:
            net = idx.vias_by_id[obj_id].net_name
            return ((net,) if net else ()), ()
        return (), ()

    def _on_ai_targets(self, nets: object) -> None:
        """Highlight the nets an AI proposal targets (display only)."""
        names = [n for n in nets if isinstance(n, str)] if isinstance(nets, list) else []
        if names and self.canvas.board is not None:
            self.canvas.highlight_net(names[0])

    def _on_ai_indicator(self, text: str, color: str) -> None:
        self.lbl_ai.setText(f"<span style='color:{color}'>●</span> {text}")

    def show_ai_usage(self) -> None:
        UsageDialog(self.ai_service.usage, self).exec()

    def export_ai_session(self) -> None:
        session = self.ai_service.session
        project = self.bus.context.project.session
        if session is None or project is None:
            self.statusBar().showMessage("Open a board first.", 4000)
            return
        start = str(Path(self.settings.last_open_directory or Path.home())
                    / f"{project.source_path.stem}-ai-session.json")  # fmt: skip
        path, _ = QFileDialog.getSaveFileName(self, "Export AI session", start, "JSON (*.json)")
        if path:
            self.write_ai_export(Path(path))

    def write_ai_export(self, path: Path) -> bool:
        session = self.ai_service.session
        project = self.bus.context.project.session
        if session is None or project is None:
            return False
        if path.suffix.lower() == ".kicad_pcb":  # never write where a board lives by that name
            dialogs.show_error(self, "Export refused", "Choose a .json file name.")
            return False
        try:
            path.write_text(json.dumps(session.export(project.name), indent=2), encoding="utf-8")
        except OSError as exc:
            dialogs.show_error(self, "Export failed", f"Could not write {path}.", str(exc))
            return False
        self.statusBar().showMessage(f"AI session exported to {path}", 6000)
        return True

    def undo(self) -> None:
        entry = self.bus.context.history.undo()
        self.statusBar().showMessage(
            f"Undone: {entry.label}" if entry else "Nothing to undo.", 4000
        )
        self._after_history_change()

    def redo(self) -> None:
        entry = self.bus.context.history.redo()
        self.statusBar().showMessage(
            f"Redone: {entry.label}" if entry else "Nothing to redo.", 4000
        )
        self._after_history_change()

    def _after_history_change(self) -> None:
        self.ai_panel.refresh_board()
        self.ai_history.refresh()
        self._update_undo_actions()

    def _update_undo_actions(self) -> None:
        h = self.bus.context.history
        self.act_undo.setEnabled(h.can_undo)
        self.act_redo.setEnabled(h.can_redo)
        self.act_undo.setText(f"&Undo {h.undo_label}" if h.undo_label else "&Undo")
        self.act_redo.setText(f"&Redo {h.redo_label}" if h.redo_label else "&Redo")
