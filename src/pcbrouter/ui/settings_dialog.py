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
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from pcbrouter.ai.provider import STAGE_UNAVAILABLE_MESSAGE
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
        tabs.addTab(self._compute_tab(), "Compute")
        tabs.addTab(self._ai_tab(), "AI Providers")
        tabs.addTab(self._routing_tab(), "Routing")
        tabs.addTab(self._gpu_tab(compute), "GPU")
        tabs.addTab(self._geometry_tab(), "Geometry")  # appended: keeps tab indices stable
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

    def _compute_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.backend_combo = QComboBox()
        self.backend_combo.addItem("CPU", ComputeBackendChoice.CPU)
        self.backend_combo.addItem(
            f"GPU (CUDA) — {STAGE_UNAVAILABLE_MESSAGE}", ComputeBackendChoice.GPU
        )
        # Disable the GPU entry: selectable only once a GPU backend exists.
        model = self.backend_combo.model()
        if isinstance(model, QStandardItemModel) and model.item(1) is not None:
            model.item(1).setEnabled(False)
        self.backend_combo.setCurrentIndex(0)
        form.addRow("Default backend:", self.backend_combo)
        form.addRow(
            _banner(
                "This version always uses the CPU. The compute backend is not used for "
                "any heavy work yet — routing arrives in a later stage."
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
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(
            _banner(
                f"Routing — {STAGE_UNAVAILABLE_MESSAGE}.\n\nThis version performs no autorouting "
                "and never modifies boards. Stage 3 validates hypothetical geometry only (see "
                "the Geometry tab and the Tools menu). Router settings (costs, layer "
                "preferences, rip-up) will appear here when the router is implemented."
            )
        )
        layout.addStretch(1)
        return w

    def _gpu_tab(self, compute: ComputeManager | None) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(
            _banner(
                f"GPU acceleration — {STAGE_UNAVAILABLE_MESSAGE}. "
                "Detection results are shown for information only."
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
        s.geometry.conservative_rules = self.conservative_rules.isChecked()
        s.geometry.grid_resolution_mm = self.grid_resolution.currentData()
        s.geometry.check_on_open = self.check_on_open.isChecked()
        self.ai_widget.apply_preferences()
        return s
