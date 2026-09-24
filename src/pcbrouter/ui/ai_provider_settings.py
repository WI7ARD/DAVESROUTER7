"""AI provider profiles and assistant preferences (Settings ▸ AI Providers).

Profile fields edit the dialog's *copy* of the settings (applied on OK). API keys are
different: Save/Delete act immediately on the OS credential store, because a key is
never part of the settings. Keys saved for profiles that are then discarded (dialog
cancelled, profile removed) are deleted again by :meth:`cleanup_keys`.
"""

from __future__ import annotations

import html
from collections.abc import Callable

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pcbrouter.ai.context_builder import ContextLevel
from pcbrouter.ai.credentials import (
    CredentialLocation,
    CredentialStoreError,
    SecureStorageUnavailable,
)
from pcbrouter.ai.models import ConnectionResult
from pcbrouter.ai.profiles import (
    DEFAULT_BASE_URLS,
    ProviderKind,
    ProviderProfile,
    credential_ref_for,
    new_profile_id,
)
from pcbrouter.ai.provider_registry import KIND_INFO
from pcbrouter.ai.requests import AIMode
from pcbrouter.settings.settings import AISettings
from pcbrouter.ui.ai_controller import AIRequestController
from pcbrouter.ui.ai_panel import STATUS_COLORS

_DEFAULT_NAMES = {
    ProviderKind.OPENAI: "OpenAI - Main",
    ProviderKind.ANTHROPIC: "Claude - Main",
    ProviderKind.OPENAI_COMPATIBLE: "Local Engineering Model",
}


class ProviderSettingsWidget(QWidget):
    def __init__(
        self, ai: AISettings, controller: AIRequestController | None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.ai = ai
        self.controller = controller
        self.service = controller.service if controller else None
        self._original_ids = {p.profile_id for p in ai.profiles}
        self._keys_saved_this_dialog: set[str] = set()
        self._removed: list[ProviderProfile] = []
        self._loading = False

        # ---------------------------------------------------------------- profile list
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._load_profile)
        add = QToolButton()
        add.setText("Add ▾")
        add.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(add)
        for kind in ProviderKind:
            action = menu.addAction(kind.display_name)
            action.triggered.connect(self._adder(kind))
        add.setMenu(menu)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self.remove_current)
        self.default_button = QPushButton("Set as Default")
        self.default_button.setToolTip("Default engineering provider for the AI panel")
        self.default_button.clicked.connect(self.set_default)
        lbuttons = QHBoxLayout()
        lbuttons.addWidget(add)
        lbuttons.addWidget(self.remove_button)
        lbuttons.addWidget(self.default_button)
        left = QVBoxLayout()
        left.addWidget(self.list, 1)
        left.addLayout(lbuttons)

        # ---------------------------------------------------------------- editor
        self.editor = QGroupBox("Profile")
        form = QFormLayout(self.editor)
        self.kind_label = QLabel("")
        self.name_edit = QLineEdit()
        self.base_url_edit = QLineEdit()
        self.org_edit = QLineEdit()
        self.project_edit = QLineEdit()
        self.requires_key = QCheckBox("Endpoint requires an API key")
        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(5, 600)
        self.timeout_spin.setSuffix(" s")
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("Paste API key, then Save Key")
        self.session_only = QCheckBox("This session only (memory)")
        self.session_only.setToolTip(
            "Keep the key in memory until the app closes; never " "written anywhere."
        )
        self.save_key_button = QPushButton("Save Key")
        self.delete_key_button = QPushButton("Delete Key")
        self.key_status = QLabel("")
        self.key_status.setWordWrap(True)
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        if (line := self.model_combo.lineEdit()) is not None:
            line.setPlaceholderText("enter a model ID or refresh the list")
        self.refresh_models_button = QPushButton("Refresh Models")
        self.model_info = QLabel("")
        self.model_info.setProperty("role", "muted")
        self.model_info.setWordWrap(True)
        self.test_button = QPushButton("Test Connection")
        self.test_status = QLabel("")
        self.test_status.setWordWrap(True)
        self.package_status = QLabel("")
        self.package_status.setWordWrap(True)
        self.package_status.setStyleSheet("color: #d29922;")
        self.url_warning = QLabel("")
        self.url_warning.setWordWrap(True)
        self.url_warning.setStyleSheet("color: #d29922;")

        key_row = QHBoxLayout()
        key_row.addWidget(self.key_edit, 1)
        key_row.addWidget(self.save_key_button)
        key_row.addWidget(self.delete_key_button)
        model_row = QHBoxLayout()
        model_row.addWidget(self.model_combo, 1)
        model_row.addWidget(self.refresh_models_button)
        form.addRow("Kind:", self.kind_label)
        form.addRow("Name:", self.name_edit)
        form.addRow("Base URL:", self.base_url_edit)
        form.addRow("", self.url_warning)
        form.addRow("Organization:", self.org_edit)
        form.addRow("Project:", self.project_edit)
        form.addRow("", self.requires_key)
        form.addRow("Timeout:", self.timeout_spin)
        form.addRow("API key:", key_row)
        form.addRow("", self.session_only)
        form.addRow("", self.key_status)
        form.addRow("Model:", model_row)
        form.addRow("", self.model_info)
        form.addRow(self.test_button, self.test_status)
        form.addRow("", self.package_status)

        for w in (self.name_edit, self.base_url_edit, self.org_edit, self.project_edit):
            w.editingFinished.connect(self._store_fields)
        self.requires_key.toggled.connect(lambda _: self._store_fields())
        self.timeout_spin.valueChanged.connect(lambda _: self._store_fields())
        self.model_combo.currentTextChanged.connect(lambda _: self._store_fields())
        self.save_key_button.clicked.connect(self.save_key)
        self.delete_key_button.clicked.connect(self.delete_key)
        self.refresh_models_button.clicked.connect(self.refresh_models)
        self.test_button.clicked.connect(self.test_connection)
        if controller is not None:
            controller.connectionTested.connect(self._on_tested)
            controller.modelsListed.connect(self._on_models)

        top = QHBoxLayout()
        top.addLayout(left, 2)
        top.addWidget(self.editor, 5)

        # ---------------------------------------------------------------- preferences
        prefs = QGroupBox("Assistant preferences")
        pf = QFormLayout(prefs)
        self.mode_combo = QComboBox()
        for m in AIMode:
            self.mode_combo.addItem(m.label, m.value)
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(AIMode(ai.default_mode).value))
        self.level_combo = QComboBox()
        for lvl in ContextLevel:
            self.level_combo.addItem(lvl.label, lvl.value)
        self.level_combo.setCurrentIndex(
            self.level_combo.findData(ContextLevel(ai.context_level).value)
        )
        self.chars_spin = _spin(2_000, 400_000, ai.max_context_chars, 1000)
        self.nets_spin = _spin(0, 5_000, ai.max_context_nets)
        self.comps_spin = _spin(0, 5_000, ai.max_context_components)
        self.turns_spin = _spin(0, 50, ai.max_conversation_turns)
        self.retries_spin = _spin(0, 5, ai.max_retries)
        self.req_timeout = QDoubleSpinBox()
        self.req_timeout.setRange(5, 600)
        self.req_timeout.setSuffix(" s")
        self.req_timeout.setValue(ai.request_timeout_s)
        self.privacy = QCheckBox("Show the privacy disclosure before the first request per board")
        self.privacy.setChecked(ai.show_privacy_preview)
        a = ai.anonymization
        self.anon_nets = QCheckBox("Anonymize net names")
        self.anon_values = QCheckBox("Anonymize component values and footprint names")
        self.anon_refs = QCheckBox("Anonymize reference designators")
        self.anon_file = QCheckBox("Anonymize board filename")
        for box, val in (
            (self.anon_nets, a.net_names),
            (self.anon_values, a.component_values),
            (self.anon_refs, a.references),
            (self.anon_file, a.board_filename),
        ):
            box.setChecked(val)
        self.save_history = QCheckBox(
            "Save AI conversation history to the board workspace " "when a board is closed"
        )
        self.save_history.setChecked(ai.save_conversation_history)
        self.debug_prompts = QCheckBox(
            "Debug: write full prompts (including board context) to " "the log"
        )
        self.debug_prompts.setToolTip("Off by default. Board context is private engineering data.")
        self.debug_prompts.setChecked(ai.debug_log_prompts)
        pf.addRow("Default mode:", self.mode_combo)
        pf.addRow("Context level:", self.level_combo)
        pf.addRow("Max context characters:", self.chars_spin)
        pf.addRow("Max nets / components sent:", _pair(self.nets_spin, self.comps_spin))
        pf.addRow("Conversation turns kept:", self.turns_spin)
        pf.addRow("Request timeout:", self.req_timeout)
        pf.addRow("Max retries (transient errors):", self.retries_spin)
        for box in (
            self.privacy,
            self.anon_nets,
            self.anon_values,
            self.anon_refs,
            self.anon_file,
            self.save_history,
            self.debug_prompts,
        ):
            pf.addRow(box)

        backend = QLabel(self._backend_text())
        backend.setWordWrap(True)
        backend.setProperty("role", "banner")
        self.backend_label = backend

        layout = QVBoxLayout(self)
        layout.addWidget(backend)
        layout.addLayout(top, 3)
        layout.addWidget(prefs, 2)
        self._reload_list()

    # ================================================================ helpers
    def _backend_text(self) -> str:
        if self.service is None:
            return "Credential storage unavailable."
        cred = self.service.credentials
        if cred.secure_available:
            return (
                f"API keys are stored in the {cred.describe_secure_backend()}, never in "
                "settings files, PCB projects or logs. Saved keys are never shown in full."
            )
        return (
            f"{cred.describe_secure_backend()}. Keys can only be kept for this session "
            "(in memory). Plaintext storage is never used."
        )

    def current(self) -> ProviderProfile | None:
        row = self.list.currentRow()
        return self.ai.profiles[row] if 0 <= row < len(self.ai.profiles) else None

    def _reload_list(self, select: int | None = None) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for p in self.ai.profiles:
            star = " ★" if p.profile_id == self.ai.default_profile_id else ""
            item = QListWidgetItem(f"{p.name}{star}")
            if self.service is not None:
                st = self.service.status_of(p)
                item.setText(f"● {p.name}{star} — {st.status.label}")
                item.setForeground(_color(STATUS_COLORS[st.status]))
                item.setToolTip(st.message)
            self.list.addItem(item)
        self.list.blockSignals(False)
        if self.ai.profiles:
            row = (
                select
                if select is not None
                else max(0, min(self.list.currentRow(), len(self.ai.profiles) - 1))
            )
            self.list.setCurrentRow(row)
            self._load_profile(row)
        else:
            self._load_profile(-1)

    def _load_profile(self, row: int) -> None:
        p = self.ai.profiles[row] if 0 <= row < len(self.ai.profiles) else None
        self.editor.setEnabled(p is not None)
        self.remove_button.setEnabled(p is not None)
        self.default_button.setEnabled(p is not None)
        if p is None:
            self.kind_label.setText("Add a provider profile to begin.")
            return
        self._loading = True
        info = KIND_INFO[p.kind]
        self.kind_label.setText(f"{p.kind.display_name} — {info.notes}")
        self.name_edit.setText(p.name)
        self.base_url_edit.setText(p.base_url or "")
        self.base_url_edit.setPlaceholderText(
            DEFAULT_BASE_URLS.get(p.kind, "http://localhost:1234/v1")
        )
        self.base_url_edit.setEnabled(p.kind is ProviderKind.OPENAI_COMPATIBLE)
        for w in (self.org_edit, self.project_edit):
            w.setEnabled(p.kind is ProviderKind.OPENAI)
        self.org_edit.setText(p.organization or "")
        self.project_edit.setText(p.project or "")
        self.requires_key.setEnabled(p.kind is ProviderKind.OPENAI_COMPATIBLE)
        self.requires_key.setChecked(p.requires_api_key)
        self.timeout_spin.setValue(p.timeout_s)
        self.model_combo.clear()
        if self.service is not None:
            for m in self.service.model_cache.get(p.profile_id, []):
                self.model_combo.addItem(m.label, m.model_id)
        self.model_combo.setEditText(p.model_id)
        self.key_edit.clear()
        self._loading = False
        self._update_key_status()
        self._update_model_info()
        missing = self.service is not None and not self.service.registry.package_available(p.kind)
        self.package_status.setText(
            f"{p.kind.display_name} support package is not installed. Install it with: "
            f'pip install "ai-pcb-router[{info.install_extra}]"'
            if missing
            else ""
        )
        self.test_status.setText(self.service.status_of(p).message if self.service else "")
        self._update_url_warning()

    def _update_url_warning(self) -> None:
        p = self.current()
        insecure = p is not None and p.is_insecure_remote_http
        self.url_warning.setText(
            "⚠ Plain http:// to another machine: requests and any API key travel unencrypted."
            if insecure
            else ""
        )

    def _store_fields(self) -> None:
        p = self.current()
        if p is None or self._loading:
            return
        try:
            p.name = self.name_edit.text().strip() or p.name
            if p.kind is ProviderKind.OPENAI_COMPATIBLE:
                p.base_url = self.base_url_edit.text().strip() or p.base_url
                p.requires_api_key = self.requires_key.isChecked()
            if p.kind is ProviderKind.OPENAI:
                p.organization = self.org_edit.text().strip() or None
                p.project = self.project_edit.text().strip() or None
            p.timeout_s = self.timeout_spin.value()
            idx = self.model_combo.findText(self.model_combo.currentText())
            model = self.model_combo.itemData(idx) if idx >= 0 else None
            p.model_id = str(model or self.model_combo.currentText()).strip()
            self.test_status.setText("")
        except ValueError as exc:
            self.test_status.setText(f"<span style='color:#f85149'>Invalid value: {exc}</span>")
            return
        if self.service is not None:
            self.service.forget_status(p.profile_id)
        self._update_model_info()
        self._update_url_warning()

    def _update_key_status(self) -> None:
        p = self.current()
        if p is None or self.service is None:
            return
        cred = self.service.credentials
        loc = cred.location(p.credential_ref)
        masked = cred.masked(p.credential_ref)
        if loc is CredentialLocation.SECURE_STORE:
            text = f"Saved in the OS credential store ({masked})."
        elif loc is CredentialLocation.SESSION_ONLY:
            text = f"Held in memory for this session only ({masked})."
        elif not p.requires_api_key:
            text = "No key saved (this endpoint does not require one)."
        else:
            text = "No key saved. Not configured."
        if not cred.secure_available:
            text += " Secure OS storage is unavailable on this system."
            self.session_only.setChecked(True)
        self.key_status.setText(text)
        self.delete_key_button.setEnabled(loc is not None)

    def _update_model_info(self) -> None:
        p = self.current()
        if p is None or self.service is None:
            return
        info = next(
            (m for m in self.service.model_cache.get(p.profile_id, []) if m.model_id == p.model_id),
            None,
        )
        if info is None:
            self.model_info.setText(
                "Model details unknown (refresh the list, or the provider " "does not report them)."
            )
            return

        def val(v: object) -> str:
            return (
                "unknown"
                if v is None
                else (f"{v:,}" if isinstance(v, int) and not isinstance(v, bool) else str(v))
            )

        self.model_info.setText(
            f"Context window: {val(info.context_window)} · max output: "
            f"{val(info.max_output_tokens)} · structured output: "
            f"{val(info.supports_structured_output)} · pricing: not tracked"
        )

    # ================================================================ actions
    def add_profile(self, kind: ProviderKind) -> ProviderProfile:
        base = "http://localhost:1234/v1" if kind is ProviderKind.OPENAI_COMPATIBLE else None
        profile = ProviderProfile(
            profile_id=new_profile_id(kind),
            name=_DEFAULT_NAMES[kind],
            kind=kind,
            base_url=base,
            requires_api_key=kind is not ProviderKind.OPENAI_COMPATIBLE,
        )
        self.ai.profiles = [*self.ai.profiles, profile]
        if self.ai.default_profile_id is None:
            self.ai.default_profile_id = profile.profile_id
        self._reload_list(len(self.ai.profiles) - 1)
        return profile

    def _adder(self, kind: ProviderKind) -> Callable[[], None]:
        def add() -> None:
            self.add_profile(kind)

        return add

    def remove_current(self) -> None:
        p = self.current()
        if p is None:
            return
        self._removed.append(p)
        self.ai.profiles = [q for q in self.ai.profiles if q.profile_id != p.profile_id]
        if self.ai.default_profile_id == p.profile_id:
            self.ai.default_profile_id = (
                self.ai.profiles[0].profile_id if self.ai.profiles else None
            )
        self._reload_list()

    def set_default(self) -> None:
        p = self.current()
        if p is not None:
            self.ai.default_profile_id = p.profile_id
            self._reload_list(self.list.currentRow())

    def save_key(self) -> None:
        p = self.current()
        if p is None or self.service is None:
            return
        raw = self.key_edit.text()
        try:
            loc = self.service.credentials.save(
                p.credential_ref, raw, session_only=self.session_only.isChecked()
            )
        except SecureStorageUnavailable as exc:
            self.key_status.setText(
                f"<span style='color:#f85149'>Not saved: {exc}. Tick "
                "“This session only” to keep it in memory.</span>"
            )
            return
        except (ValueError, CredentialStoreError) as exc:
            self.key_status.setText(f"<span style='color:#f85149'>Not saved: {exc}</span>")
            return
        finally:
            self.key_edit.clear()  # never keep the typed key in the widget
        self._keys_saved_this_dialog.add(p.profile_id)
        self.service.forget_status(p.profile_id)
        self._reload_list(self.list.currentRow())
        self._update_key_status()
        self.test_status.setText(
            "Key saved "
            + (
                "in memory for this session."
                if loc is CredentialLocation.SESSION_ONLY
                else "to the OS credential store."
            )
            + " Use Test Connection to verify it."
        )

    def delete_key(self) -> None:
        p = self.current()
        if p is None or self.service is None:
            return
        self.service.credentials.delete(p.credential_ref)
        self.service.forget_status(p.profile_id)
        self._update_key_status()
        self._reload_list(self.list.currentRow())

    def refresh_models(self) -> None:
        p = self.current()
        if p is None or self.controller is None:
            return
        self._store_fields()
        self.model_info.setText("Fetching model list…")
        self.controller.list_models(p.model_copy())

    def test_connection(self) -> None:
        p = self.current()
        if p is None or self.controller is None:
            return
        self._store_fields()
        self.test_status.setText("Testing (no board data is sent)…")
        self.test_button.setEnabled(False)
        self.controller.test_connection(p.model_copy())

    def _on_tested(self, profile_id: str, result: ConnectionResult) -> None:
        self.test_button.setEnabled(True)
        p = self.current()
        if p is None or p.profile_id != profile_id:
            self._reload_list(self.list.currentRow())
            return
        color = STATUS_COLORS[result.status]
        latency = f" ({result.latency_s:.1f} s)" if result.latency_s else ""
        self._reload_list(self.list.currentRow())
        self.test_status.setText(
            f"<span style='color:{color}'>● {result.status.label}</span>"
            f"{latency}<br>{_esc(result.message)}"
        )

    def _on_models(self, profile_id: str, models: object, error: object) -> None:
        p = self.current()
        if p is None or p.profile_id != profile_id:
            return
        if error:
            self.model_info.setText(f"{error}")
            return
        keep = p.model_id
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for m in models or []:  # type: ignore[attr-defined]
            self.model_combo.addItem(m.label, m.model_id)
        self.model_combo.setEditText(keep)
        self.model_combo.blockSignals(False)
        self.model_info.setText(
            f"{self.model_combo.count()} model(s) listed. Choose one or type " "an ID."
        )

    # ================================================================ results
    def apply_preferences(self) -> None:
        a = self.ai
        a.default_mode = AIMode(self.mode_combo.currentData())
        a.context_level = ContextLevel(self.level_combo.currentData())
        a.max_context_chars = self.chars_spin.value()
        a.max_context_nets = self.nets_spin.value()
        a.max_context_components = self.comps_spin.value()
        a.max_conversation_turns = self.turns_spin.value()
        a.request_timeout_s = self.req_timeout.value()
        a.max_retries = self.retries_spin.value()
        a.show_privacy_preview = self.privacy.isChecked()
        an = a.anonymization
        an.net_names = self.anon_nets.isChecked()
        an.component_values = self.anon_values.isChecked()
        an.references = self.anon_refs.isChecked()
        an.board_filename = self.anon_file.isChecked()
        a.save_conversation_history = self.save_history.isChecked()
        a.debug_log_prompts = self.debug_prompts.isChecked()

    def cleanup_keys(self, accepted: bool) -> None:
        """Delete keys of profiles that will not exist after this dialog closes."""
        if self.service is None:
            return
        surviving = {p.profile_id for p in self.ai.profiles} if accepted else self._original_ids
        doomed = {p.credential_ref for p in self._removed if p.profile_id not in surviving}
        doomed |= {credential_ref_for(pid) for pid in self._keys_saved_this_dialog - surviving}
        for ref in sorted(doomed):
            self.service.credentials.delete(ref)


def _spin(lo: int, hi: int, value: int, step: int = 1) -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setValue(value)
    return s


def _pair(a: QWidget, b: QWidget) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.addWidget(a)
    h.addWidget(b)
    return w


def _color(hex_color: str) -> QColor:
    return QColor(hex_color)


def _esc(text: str) -> str:
    return html.escape(text)
