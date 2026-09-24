"""Main engineering-workstation window.

The window is glue: it turns user gestures into commands on the
:class:`~pcbrouter.commands.CommandBus` and forwards results to the panels. It never
parses files or touches routing internals directly.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QToolBar,
    QWidget,
)

from pcbrouter import APP_NAME, STAGE, __version__
from pcbrouter.commands import CloseBoardCommand, CommandBus, OpenBoardCommand
from pcbrouter.compute.detection import GpuDetectionResult
from pcbrouter.compute.manager import ComputeManager
from pcbrouter.kicad.adapter import WarningSeverity
from pcbrouter.project.manager import ProjectSession
from pcbrouter.settings.settings import AppSettings, SettingsStore
from pcbrouter.ui import dialogs
from pcbrouter.ui.inspector_panel import InspectorPanel
from pcbrouter.ui.layers_panel import LayersPanel
from pcbrouter.ui.log_panel import LogPanel
from pcbrouter.ui.nets_panel import NetsPanel
from pcbrouter.ui.pcb_canvas import ItemKind, PcbCanvas
from pcbrouter.ui.project_panel import ProjectPanel
from pcbrouter.ui.settings_dialog import SettingsDialog
from pcbrouter.ui.theme import apply_theme

log = logging.getLogger(__name__)

BOARD_FILE_FILTER = "KiCad PCB (*.kicad_pcb);;All files (*)"
_PANELS = ("project", "layers", "inspector", "nets", "log")


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
        self.resizeDocks(
            [self.docks["project"], self.docks["inspector"]], [300, 360], Qt.Orientation.Horizontal
        )
        self.resizeDocks([self.docks["log"]], [150], Qt.Orientation.Vertical)

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._build_status_bar()
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
            "Configure &AI Providers… (Available in a later stage)",
            lambda: dialogs.show_stage_unavailable(
                self, "Configure AI Providers", "Stage 5 (AI provider integration)"
            ),
            tip="Available in a later stage",
        )
        self.act_route_net = self._action(
            "Route &Selected Net (Available in a later stage)",
            lambda: dialogs.show_stage_unavailable(
                self, "Route Selected Net", "Stage 4 (CPU autorouter)"
            ),
            tip="Available in a later stage",
        )
        self.act_route_board = self._action(
            "Route &Board (Available in a later stage)",
            lambda: dialogs.show_stage_unavailable(
                self, "Route Board", "Stage 6 (board-level routing)"
            ),
            tip="Available in a later stage",
        )
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
        file_menu.addSeparator()
        file_menu.addAction(self.act_exit)
        self._rebuild_recent_menu()

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
        ):
            act = self.docks[key].toggleViewAction()
            act.setText(title)
            view.addAction(act)

        tools = mb.addMenu("&Tools")
        tools.addAction(self.act_compute)
        ai = mb.addMenu("&AI")
        ai.addAction(self.act_ai)
        router = mb.addMenu("&Router")
        router.addAction(self.act_route_net)
        router.addAction(self.act_route_board)
        help_menu = mb.addMenu("&Help")
        help_menu.addAction(self.act_about)

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
        stage = QLabel(f"  Stage {STAGE} · read-only inspector — no routing, no AI calls  ")
        stage.setProperty("role", "muted")
        tb.addWidget(stage)
        self.addToolBar(tb)

    def _build_status_bar(self) -> None:
        sb = self.statusBar()
        self.lbl_board = QLabel("No board")
        self.lbl_cursor = QLabel("X —  Y —")
        self.lbl_zoom = QLabel("Zoom —")
        self.lbl_layer = QLabel("Layer —")
        self.lbl_backend = QLabel("")
        self.lbl_counts = QLabel("")
        self.lbl_cursor.setMinimumWidth(170)
        self.lbl_board.setToolTip("Open board (read-only)")
        self.lbl_cursor.setToolTip("Cursor position in board millimetres (KiCad coordinates)")
        self.lbl_zoom.setToolTip("Screen pixels per board millimetre")
        for w in (
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
        notes = session.load_result.warnings
        n_warn = sum(1 for w in notes if w.severity is WarningSeverity.WARNING)
        suffix = f" — {n_warn} warning(s), see Project ▸ Load notes" if n_warn else ""
        self.statusBar().showMessage(result.message + suffix, 10000)
        return True

    def close_board(self) -> None:
        if not self.bus.context.project.is_open:
            self.statusBar().showMessage("No board is open.", 4000)
            return
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
    def open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self.compute, self)
        if dlg.exec() != SettingsDialog.DialogCode.Accepted:
            return
        new = dlg.result_settings()
        new.window = self.settings.window
        self.settings = new
        app = QApplication.instance()
        if isinstance(app, QApplication):
            apply_theme(app, new.theme)
        self._apply_viewer_settings()
        self._rebuild_recent_menu()
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
        board = self.canvas.board
        if board is not None:
            self.inspector.show_object(board, kind, obj_id)

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

    def _update_backend_label(self) -> None:
        det = self.compute.gpu.detection
        self.lbl_backend.setText(f"{self.compute.active.name} · GPU: {det.status.short_label}")
        self.lbl_backend.setToolTip(
            f"Active compute backend: {self.compute.active.name}\nGPU: {det.summary()}\n"
            "Details: Tools ▸ Compute Backend Information"
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        self.save_settings()
        if self.bus.context.project.is_open:
            self.bus.dispatch(CloseBoardCommand())
        event.accept()
