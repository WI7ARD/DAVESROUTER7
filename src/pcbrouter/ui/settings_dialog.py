"""Settings dialog. Edits a *copy* of :class:`AppSettings`; the caller persists it."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from pcbrouter.compute.manager import ComputeManager
from pcbrouter.routing.occupancy import GRID_RESOLUTIONS_MM
from pcbrouter.settings.settings import AppSettings, ComputeBackendChoice, Theme
from pcbrouter.ui.ai_controller import AIRequestController
from pcbrouter.ui.ai_provider_settings import ProviderSettingsWidget
from pcbrouter.ui.dialogs import compute_info_text


def _banner(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty("role", "banner")
    return label


class SettingsDialog(QDialog):
    def __init__(
        self,
        settings: AppSettings,
        compute: ComputeManager | None,
        parent: QWidget | None = None,
        ai_controller: AIRequestController | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(620)
        self._settings = settings.model_copy(deep=True)
        self._ai_controller = ai_controller

        tabs = QTabWidget()
        tabs.addTab(self._general_tab(), "General")
        tabs.addTab(self._viewer_tab(), "Viewer")
        tabs.addTab(self._compute_tab(compute), "Compute")
        tabs.addTab(self._ai_tab(), "AI Providers")
        tabs.addTab(self._routing_tab(), "Routing")
        tabs.addTab(self._gpu_tab(compute), "GPU")
        tabs.addTab(self._geometry_tab(), "Geometry")  # appended: keeps tab indices stable
        tabs.addTab(self._export_tab(), "Export")
        self.tabs = tabs

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------ tabs
    def _general_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.theme_combo = QComboBox()
        for t in Theme:
            self.theme_combo.addItem(t.value.capitalize(), t)
        self.theme_combo.setCurrentIndex(list(Theme).index(self._settings.theme))
        self.theme_combo.setToolTip("Widget theme. The board canvas always uses a dark workspace.")
        form.addRow("Theme:", self.theme_combo)
        self.clear_recent = QPushButton(
            f"Clear recent boards ({len(self._settings.recent_boards)})"
        )
        self.clear_recent.setEnabled(bool(self._settings.recent_boards))
        self.clear_recent.clicked.connect(self._on_clear_recent)
        form.addRow("Recent boards:", self.clear_recent)
        last_dir = QLabel(self._settings.last_open_directory or "not set")
        last_dir.setProperty("role", "muted")
        form.addRow("Last directory:", last_dir)
        return w

    def _viewer_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.grid_visible = QCheckBox("Show grid")
        self.grid_visible.setChecked(self._settings.viewer.grid_visible)
        form.addRow(self.grid_visible)
        self.grid_spacing = QDoubleSpinBox()
        self.grid_spacing.setRange(0.01, 100.0)
        self.grid_spacing.setDecimals(3)
        self.grid_spacing.setSingleStep(0.1)
        self.grid_spacing.setSuffix(" mm")
        self.grid_spacing.setValue(self._settings.viewer.grid_spacing_mm)
        self.grid_spacing.setToolTip(
            "Base grid pitch. The canvas coarsens it automatically when "
            "zoomed out so lines stay legible."
        )
        form.addRow("Grid spacing:", self.grid_spacing)
        self.show_labels = QCheckBox("Show reference labels")
        self.show_labels.setChecked(self._settings.viewer.show_reference_labels)
        form.addRow(self.show_labels)
        self.show_bodies = QCheckBox("Show footprint bodies")
        self.show_bodies.setChecked(self._settings.viewer.show_footprint_bodies)
        form.addRow(self.show_bodies)
        return w

    def _compute_tab(self, compute: ComputeManager | None) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.backend_combo = QComboBox()
        # never initialise the GPU here (GUI thread): use the background probe
        probe = getattr(compute, "probe_result", None)
        gpu_ok = bool(probe is not None and probe.available)
        gpu_name = compute.gpu.name if compute is not None else "GPU"
        self.backend_combo.addItem("CPU (reference A* search)", ComputeBackendChoice.CPU)
        self.backend_combo.addItem(
            f"{gpu_name} wavefront" + ("" if gpu_ok else " — not available on this machine"),
            ComputeBackendChoice.GPU,
        )
        self.backend_combo.addItem(
            "Auto (GPU for large grids)" + ("" if gpu_ok else " — not available"),
            ComputeBackendChoice.AUTO,
        )
        # GPU entries are selectable only when the GPU backend actually initialised.
        model = self.backend_combo.model()
        if isinstance(model, QStandardItemModel) and not gpu_ok:
            for row in (1, 2):
                if model.item(row) is not None:
                    model.item(row).setEnabled(False)
        current = self._settings.default_compute_backend
        idx = self.backend_combo.findData(current)
        self.backend_combo.setCurrentIndex(idx if idx >= 0 and (gpu_ok or idx == 0) else 0)
        form.addRow("Default backend:", self.backend_combo)
        form.addRow(
            _banner(
                "The CPU A* router is the reference and always available. A GPU "
                "(NVIDIA via CuPy, Intel via dpnp) accelerates the wavefront search when "
                "installed; every route is still checked by the CPU exact validator, and "
                "any GPU problem falls back to the CPU. Choose GPU to use it for every "
                "search; Auto uses it only for large grids (at least 2 million cells), "
                "where it is likely faster. Via-limited searches always use the CPU. "
                "Check it with Tools ▸ Test GPU on This Board. If the GPU entries are "
                "disabled, no usable device/library was found: for Intel Iris Xe/Arc, "
                "install the Intel graphics driver and 'pip install dpnp'; for NVIDIA, "
                "'pip install cupy-cuda12x'."
            )
        )
        return w

    def _ai_tab(self) -> QWidget:
        self.ai_widget = ProviderSettingsWidget(self._settings.ai, self._ai_controller)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.ai_widget)
        return scroll

    def done(self, result: int) -> None:  # accept or reject
        self.ai_widget.cleanup_keys(accepted=result == QDialog.DialogCode.Accepted)
        super().done(result)

    def _routing_tab(self) -> QWidget:
        r = self._settings.routing
        w = QWidget()
        form = QFormLayout(w)
        form.addRow(
            _banner(
                "Router preferences. Widths, clearances and via sizes always come from the "
                "board rules (Tools ▸ Routing Rules Inspector); routes change only the "
                "in-memory working copy until you export."
            )
        )
        self.route_candidates = QSpinBox()
        self.route_candidates.setRange(1, 5)
        self.route_candidates.setValue(r.candidates)
        form.addRow("Candidates per net:", self.route_candidates)
        self.route_time = QDoubleSpinBox()
        self.route_time.setRange(1.0, 600.0)
        self.route_time.setSuffix(" s")
        self.route_time.setValue(r.time_limit_s)
        form.addRow("Search time limit:", self.route_time)
        self.route_strategy = QComboBox()
        for key in ("critical_first", "most_constrained", "shortest_first", "fewest_escapes",
                    "congestion_aware"):  # fmt: skip
            self.route_strategy.addItem(key.replace("_", " ").capitalize(), key)
        self.route_strategy.setCurrentIndex(max(0, self.route_strategy.findData(r.strategy)))
        form.addRow("Board net order:", self.route_strategy)
        self.route_passes = QSpinBox()
        self.route_passes.setRange(1, 5)
        self.route_passes.setValue(r.max_passes)
        form.addRow("Board routing passes:", self.route_passes)
        self.route_ripup = QCheckBox(
            "Allow rip-up of router-generated copper (never source copper)"
        )
        self.route_ripup.setChecked(r.allow_ripup)
        form.addRow(self.route_ripup)
        self.ai_autonomy = QComboBox()
        for key, label in (
            ("advisory", "Advisory — AI analyses only, never runs the router"),
            ("approval_required", "Approval required — each routing command (default)"),
            ("batch_approval", "Batch approval — approve a bounded plan at once"),
        ):
            self.ai_autonomy.addItem(label, key)
        self.ai_autonomy.setCurrentIndex(
            max(0, self.ai_autonomy.findData(self._settings.ai.autonomy_mode))
        )
        self.ai_autonomy.setToolTip(
            "There is no fully autonomous mode: every result is previewed and accepted by you."
        )
        form.addRow("AI autonomy:", self.ai_autonomy)
        return w

    def _gpu_tab(self, compute: ComputeManager | None) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(
            _banner(
                "GPU acceleration is optional (Compute tab). The CPU router is the "
                "reference; GPU results are validated by the same CPU checks."
            )
        )
        info = QLabel(compute_info_text(compute) if compute else "Compute information unavailable")
        info.setWordWrap(True)
        info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(info)
        layout.addStretch(1)
        return w

    def _geometry_tab(self) -> QWidget:
        g = self._settings.geometry
        w = QWidget()
        form = QFormLayout(w)
        form.addRow(
            _banner(
                "Stage 3 geometry and design-rule engine. These settings affect only the "
                "application's own checks; KiCad files are never modified."
            )
        )
        self.conservative_rules = QCheckBox("Conservative Rule Handling (recommended)")
        self.conservative_rules.setChecked(g.conservative_rules)
        self.conservative_rules.setToolTip(
            "ON: when a critical rule is unknown or unsupported, route validation refuses the "
            "geometry (RULE_UNKNOWN).\nOFF (expert): unknowns are reported as warnings instead. "
            "Known violations are always INVALID either way."
        )
        self.conservative_rules.toggled.connect(self._on_conservative_toggled)
        form.addRow(self.conservative_rules)
        self.grid_resolution = QComboBox()
        for mm in GRID_RESOLUTIONS_MM:
            self.grid_resolution.addItem(f"{mm:.2f} mm", mm)
        idx = self.grid_resolution.findData(g.grid_resolution_mm)
        self.grid_resolution.setCurrentIndex(idx if idx >= 0 else 0)
        form.addRow("Routing grid resolution:", self.grid_resolution)
        self.check_on_open = QCheckBox("Run the Internal Geometry Check after opening a board")
        self.check_on_open.setChecked(g.check_on_open)
        form.addRow(self.check_on_open)
        return w

    def _export_tab(self) -> QWidget:
        e = self._settings.export
        w = QWidget()
        form = QFormLayout(w)
        form.addRow(
            _banner(
                "Routed boards are exported to a NEW file (default <name>_routed.kicad_pcb). "
                "The export only appends tracks and vias, reloads the result to check it, "
                "and is written atomically. Passing checks does not mean a board is ready "
                "for manufacturing: always review it in KiCad and against your "
                "fabricator's rules."
            )
        )
        self.allow_overwrite = QCheckBox("Allow File ▸ Overwrite Source Board (backup first)")
        self.allow_overwrite.setChecked(e.allow_overwrite_source)
        self.allow_overwrite.setToolTip("Off (default): the opened .kicad_pcb is never modified.")
        form.addRow(self.allow_overwrite)
        self.kicad_drc = QCheckBox("Run KiCad DRC on the exported file (needs kicad-cli)")
        self.kicad_drc.setChecked(e.run_kicad_drc)
        form.addRow(self.kicad_drc)
        self.autosave_recovery = QCheckBox("Keep a crash-recovery copy of the working session")
        self.autosave_recovery.setChecked(e.autosave_recovery)
        form.addRow(self.autosave_recovery)
        return w

    def _on_conservative_toggled(self, checked: bool) -> None:
        if checked or not self.isVisible():
            return
        answer = QMessageBox.warning(
            self,
            "Turn off Conservative Rule Handling?",
            "With conservative handling OFF, geometry affected by an unknown or unsupported "
            "critical rule is reported as VALID WITH WARNINGS instead of being refused.\n\n"
            "Only do this if you have checked those rules yourself. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.conservative_rules.setChecked(True)

    # ------------------------------------------------------------------ result
    def _on_clear_recent(self) -> None:
        self._settings.recent_boards = []
        self.clear_recent.setText("Clear recent boards (0)")
        self.clear_recent.setEnabled(False)

    def result_settings(self) -> AppSettings:
        s = self._settings
        s.theme = self.theme_combo.currentData()
        s.viewer.grid_visible = self.grid_visible.isChecked()
        s.viewer.grid_spacing_mm = self.grid_spacing.value()
        s.viewer.show_reference_labels = self.show_labels.isChecked()
        s.viewer.show_footprint_bodies = self.show_bodies.isChecked()
        s.default_compute_backend = self.backend_combo.currentData()
        s.routing.candidates = self.route_candidates.value()
        s.routing.time_limit_s = self.route_time.value()
        s.routing.strategy = self.route_strategy.currentData()
        s.routing.max_passes = self.route_passes.value()
        s.routing.allow_ripup = self.route_ripup.isChecked()
        s.ai.autonomy_mode = self.ai_autonomy.currentData()
        s.geometry.conservative_rules = self.conservative_rules.isChecked()
        s.geometry.grid_resolution_mm = self.grid_resolution.currentData()
        s.geometry.check_on_open = self.check_on_open.isChecked()
        s.export.allow_overwrite_source = self.allow_overwrite.isChecked()
        s.export.run_kicad_drc = self.kicad_drc.isChecked()
        s.export.autosave_recovery = self.autosave_recovery.isChecked()
        self.ai_widget.apply_preferences()
        return s
