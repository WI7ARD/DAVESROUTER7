"""AI Engineering dock panel.

Workflow: pick provider/model/mode → type a request → (first time per board: privacy
disclosure) → the request runs in the background with cancel → the response is
validated locally → proposals are previewed with validation results → the user
edits, approves or rejects. Approving never modifies the PCB.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pcbrouter.ai.board_summary import BoardFactService
from pcbrouter.ai.command_schema import Operation, OperationCategory
from pcbrouter.ai.models import ConnectionStatus
from pcbrouter.ai.profiles import ProviderProfile
from pcbrouter.ai.proposals import CommandProposal, CommandState
from pcbrouter.ai.requests import AIMode
from pcbrouter.ai.route_bridge import MAX_PLANNING_ROUNDS
from pcbrouter.ai.session import Interaction
from pcbrouter.ai.usage import format_token_estimate
from pcbrouter.commands import (
    ApproveProposalCommand,
    CommandBus,
    EditProposalCommand,
    RejectProposalCommand,
)
from pcbrouter.settings.settings import AppSettings
from pcbrouter.ui.ai_controller import AIRequestController
from pcbrouter.ui.ai_dialogs import (
    ConstraintEditorDialog,
    ContextPreviewDialog,
    PrivacyDisclosureDialog,
)
from pcbrouter.ui.ai_render import esc, interaction_html, proposal_html

log = logging.getLogger(__name__)

EXAMPLE_PROMPTS: dict[AIMode, tuple[str, ...]] = {
    AIMode.ANALYZE: (
        "Analyze this board.",
        "Analyze the unrouted nets.",
        "Which areas of this board look congested?",
        "Explain the CAN section.",
    ),
    AIMode.PLAN: (
        "Give me a routing order for this board.",
        "Prioritize communication buses before GPIO.",
    ),
    AIMode.COMMAND: (
        "Route CAN_H and CAN_L first.",
        "Route CAN first and minimize vias.",
        "Route UART but don't touch my existing traces.",
        "Minimize vias on SPI.",
    ),
    AIMode.EXPLAIN: ("Explain why this net may be difficult to route.",),
}

STATUS_COLORS = {
    ConnectionStatus.CONNECTED: "#3fb950",
    ConnectionStatus.MODEL_UNAVAILABLE: "#d29922",
    ConnectionStatus.AUTH_FAILED: "#f85149",
    ConnectionStatus.UNREACHABLE: "#f85149",
    ConnectionStatus.ERROR: "#f85149",
    ConnectionStatus.PACKAGE_MISSING: "#d29922",
    ConnectionStatus.NOT_CONFIGURED: "#8b949e",
    ConnectionStatus.UNKNOWN: "#8b949e",
}

SelectionProvider = Callable[[], tuple[tuple[str, ...], tuple[str, ...]]]


class AIEngineeringPanel(QWidget):
    configureRequested = Signal()
    runRequested = Signal(object)  # list[str] proposal ids (one, or a batch plan)
    targetNetsChanged = Signal(object)  # list[str]: nets of the selected proposal
    historyChanged = Signal()
    statusIndicatorChanged = Signal(str, str)  # (text, colour)

    def __init__(
        self,
        controller: AIRequestController,
        bus: CommandBus,
        settings: AppSettings,
        selection: SelectionProvider | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.service = controller.service
        self.bus = bus
        self.settings = settings
        self._selection = selection or (lambda: ((), ()))
        self._current_proposal: str | None = None

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_empty_page())
        self.stack.addWidget(self._build_main_page())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.stack)

        controller.busyChanged.connect(self._on_busy)
        controller.statusChanged.connect(self.status_label.setText)
        controller.interactionFinished.connect(self._on_interaction)
        controller.connectionTested.connect(lambda *_: self.refresh_status())
        self.refresh_profiles()
        self.refresh_board()

    # ================================================================ pages
    def _build_empty_page(self) -> QWidget:
        page = QWidget()
        title = QLabel("<h3>AI Engineering</h3>")
        text = QLabel(
            "Connect an AI provider to analyze your PCB and translate natural-language "
            "engineering instructions into validated routing constraints.<br><br>"
            "Providers: <b>OpenAI</b> · <b>Anthropic</b> · <b>Custom / Local</b> "
            "(OpenAI-compatible)<br><br>"
            "<i>The AI planner cannot directly modify your PCB.</i> Every proposal is "
            "validated locally and needs your approval; no routing is executed in this "
            "version. Board inspection works fully without AI."
        )
        text.setWordWrap(True)
        self.empty_status = QLabel("No AI provider configured.")
        self.empty_status.setProperty("role", "muted")
        button = QPushButton("Configure Provider…")
        button.clicked.connect(self.configureRequested.emit)
        self.configure_button = button
        v = QVBoxLayout(page)
        v.addWidget(title)
        v.addWidget(text)
        v.addWidget(self.empty_status)
        v.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
        v.addStretch(1)
        return page

    def _build_main_page(self) -> QWidget:
        page = QWidget()
        self.provider_combo = QComboBox()
        self.provider_combo.setToolTip("Provider profile (Settings ▸ AI Providers)")
        self.provider_combo.currentIndexChanged.connect(self._on_profile_changed)
        self.provider_status = QLabel("")
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setToolTip("Model ID. Pick from the provider's list or type any ID.")
        if (line := self.model_combo.lineEdit()) is not None:
            line.setPlaceholderText("model id")
        self.model_combo.currentTextChanged.connect(self._on_model_text)
        refresh = QToolButton()
        refresh.setText("⟳")
        refresh.setToolTip("Refresh the model list from the provider")
        refresh.clicked.connect(self._refresh_models)
        self.refresh_models_button = refresh
        self.mode_combo = QComboBox()
        for mode in AIMode:
            self.mode_combo.addItem(mode.label, mode.value)  # Qt stores enums as str
        self.mode_combo.setCurrentIndex(
            max(0, self.mode_combo.findData(AIMode(self.settings.ai.default_mode).value))
        )
        self.mode_combo.setToolTip(
            "Analyze: questions · Plan: routing strategy · Command: "
            "structured operations · Explain: selected entities"
        )
        configure = QToolButton()
        configure.setText("⚙")
        configure.setToolTip("Configure AI providers")
        configure.clicked.connect(self.configureRequested.emit)

        form = QFormLayout()
        prow = QHBoxLayout()
        prow.addWidget(self.provider_combo, 1)
        prow.addWidget(configure)
        form.addRow("Provider:", prow)
        form.addRow("", self.provider_status)
        mrow = QHBoxLayout()
        mrow.addWidget(self.model_combo, 1)
        mrow.addWidget(refresh)
        form.addRow("Model:", mrow)
        form.addRow("Mode:", self.mode_combo)

        self.conversation = QTextBrowser()
        self.conversation.setOpenLinks(False)
        self.conversation.setPlaceholderText("Conversation for this board appears here.")

        proposals = QGroupBox("Proposed PCB Command")
        self.proposal_combo = QComboBox()
        self.proposal_combo.currentIndexChanged.connect(self._on_proposal_selected)
        self.proposal_view = QTextBrowser()
        self.proposal_view.setOpenLinks(False)
        self.approve_button = QPushButton("Approve Proposal")
        self.approve_button.setToolTip("Accept into the command history. Does NOT modify the PCB.")
        self.reject_button = QPushButton("Reject")
        self.edit_button = QPushButton("Edit Constraints…")
        self.run_button = QPushButton("Run with Router")
        self.run_button.setToolTip(
            "Route this approved command with the deterministic router. Candidates are "
            "previewed; nothing is applied until you accept."
        )
        self.plan_button = QPushButton("Approve && Run Plan")
        self.plan_button.setToolTip(
            "Batch approval: approve every valid routing proposal of the latest answer and "
            "route them as one reviewable job"
        )
        self.facts_button = QPushButton("Send Requested Facts")
        self.facts_button.setToolTip(
            "The AI asked for deterministic board facts: answer them and ask it to continue"
        )
        self.revise_button = QPushButton("Ask AI to Revise")
        self.revise_button.setToolTip(
            "Send the router's results back to the AI for a revised plan (bounded rounds)"
        )
        self.run_button.clicked.connect(self._run)
        self.plan_button.clicked.connect(self._run_plan)
        self.facts_button.clicked.connect(self.send_facts)
        self.revise_button.clicked.connect(self.ask_revision)
        self.approve_button.clicked.connect(self._approve)
        self.reject_button.clicked.connect(self._reject)
        self.edit_button.clicked.connect(self._edit)
        brow = QHBoxLayout()
        for b in (self.approve_button, self.reject_button, self.edit_button, self.run_button):
            brow.addWidget(b)
        brow.addStretch(1)
        brow2 = QHBoxLayout()
        for b in (self.plan_button, self.facts_button, self.revise_button):
            brow2.addWidget(b)
        brow2.addStretch(1)
        pv = QVBoxLayout(proposals)
        pv.addWidget(self.proposal_combo)
        pv.addWidget(self.proposal_view, 1)
        pv.addLayout(brow)
        pv.addLayout(brow2)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.conversation, "Conversation")
        self.tabs.addTab(proposals, "Proposals")

        self.context_label = QLabel("")
        self.context_label.setWordWrap(True)
        self.context_label.setProperty("role", "muted")
        preview = QPushButton("Preview Context…")
        preview.setToolTip("See exactly what board context would be sent")
        preview.clicked.connect(self._preview_context)
        self.preview_button = preview
        crow = QHBoxLayout()
        crow.addWidget(self.context_label, 1)
        crow.addWidget(preview)

        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText(
            "Ask about the board or describe what you want, e.g. "
            "“Route CAN first and minimize vias.”"
        )
        self.prompt.setMaximumHeight(90)
        self.prompt.textChanged.connect(self._update_buttons)
        examples = QToolButton()
        examples.setText("Examples ▾")
        examples.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(examples)
        for mode, prompts in EXAMPLE_PROMPTS.items():
            sub = menu.addMenu(mode.label)
            for text in prompts:
                act = QAction(text, sub)
                act.triggered.connect(lambda _=False, m=mode, t=text: self._use_example(m, t))
                sub.addAction(act)
        examples.setMenu(menu)
        self.send_button = QPushButton("Send")
        self.send_button.setDefault(True)
        self.send_button.clicked.connect(self.send)
        self.cancel_button = QPushButton("Cancel Request")
        self.cancel_button.clicked.connect(self.controller.cancel)
        arow = QHBoxLayout()
        arow.addWidget(examples)
        arow.addStretch(1)
        arow.addWidget(self.cancel_button)
        arow.addWidget(self.send_button)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumHeight(6)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.status_label = QLabel("")
        self.status_label.setProperty("role", "muted")
        self.usage_label = QLabel("")
        self.usage_label.setProperty("role", "muted")

        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.addLayout(form)
        v.addWidget(self.tabs, 1)
        v.addLayout(crow)
        v.addWidget(self.prompt)
        v.addLayout(arow)
        v.addWidget(self.progress)
        v.addWidget(self.status_label)
        v.addWidget(self.usage_label)
        return page

    # ================================================================ state refresh
    def current_profile(self) -> ProviderProfile | None:
        pid = self.provider_combo.currentData()
        return self.settings.ai.profile(pid) if pid else None

    def refresh_profiles(self) -> None:
        profiles = self.settings.ai.profiles
        self.stack.setCurrentIndex(1 if profiles else 0)
        previous = self.provider_combo.currentData()
        self.provider_combo.blockSignals(True)
        self.provider_combo.clear()
        for p in profiles:
            self.provider_combo.addItem(p.label, p.profile_id)
        default = (
            previous if self.settings.ai.profile(previous) else self.settings.ai.default_profile_id
        )
        idx = self.provider_combo.findData(default)
        self.provider_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.provider_combo.blockSignals(False)
        self._on_profile_changed()

    def refresh_board(self) -> None:
        session = self.service.session
        self.conversation.clear()
        if session is not None:
            for inter in session.interactions:
                self.conversation.append(interaction_html(inter, session.proposals))
        self._refresh_proposals()
        self._update_context_label()
        self._update_buttons()
        self._update_usage()

    def refresh_status(self) -> None:
        profile = self.current_profile()
        if profile is None:
            self.provider_status.setText("")
            self.statusIndicatorChanged.emit(
                "AI: not configured", STATUS_COLORS[ConnectionStatus.NOT_CONFIGURED]
            )
            return
        st = self.service.status_of(profile)
        color = STATUS_COLORS[st.status]
        self.provider_status.setText(
            f"<span style='color:{color}'>●</span> {esc(profile.name)} — {esc(st.status.label)}"
        )
        self.provider_status.setToolTip(st.message)
        self.statusIndicatorChanged.emit(f"AI: {profile.name} — {st.status.label}", color)
        self._update_buttons()

    def _on_profile_changed(self, *_: object) -> None:
        profile = self.current_profile()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        if profile is not None:
            for m in self.service.model_cache.get(profile.profile_id, []):
                self.model_combo.addItem(m.label, m.model_id)
            self.model_combo.setEditText(profile.model_id)
        self.model_combo.blockSignals(False)
        self.refresh_status()
        self._update_context_label()

    def _on_model_text(self, text: str) -> None:
        profile = self.current_profile()
        if profile is None:
            return
        idx = self.model_combo.findText(text)
        model_id = self.model_combo.itemData(idx) if idx >= 0 else text.strip()
        if model_id and model_id != profile.model_id and len(model_id) <= 200:
            profile.model_id = str(model_id)  # persisted with settings on exit
            self.service.forget_status(profile.profile_id)
            self.refresh_status()
        self._update_buttons()

    def selected_model(self) -> str:
        idx = self.model_combo.findText(self.model_combo.currentText())
        data = self.model_combo.itemData(idx) if idx >= 0 else None
        return str(data or self.model_combo.currentText()).strip()

    def _refresh_models(self) -> None:
        profile = self.current_profile()
        if profile is None:
            return
        self.status_label.setText(f"Fetching models from {profile.name}…")
        self.controller.list_models(profile)

    def on_models_listed(self, profile_id: str, models: object, error: object) -> None:
        profile = self.current_profile()
        if profile is None or profile.profile_id != profile_id:
            return
        if error:
            self.status_label.setText(f"Model list unavailable: {error} Enter a model ID manually.")
            return
        self._on_profile_changed()
        self.status_label.setText(f"{self.model_combo.count()} model(s) listed.")

    def _update_context_label(self) -> None:
        session = self.service.session
        profile = self.current_profile()
        if session is None:
            self.context_label.setText("Open a board to use the AI assistant.")
            return
        ctx = session.build_context(self.prompt.toPlainText() if hasattr(self, "prompt") else "")
        who = profile.name if profile else "the provider"
        self.context_label.setText(
            f"Board context shared with {esc(who)}: {ctx.level.label} · "
            f"{format_token_estimate(ctx.token_estimate)} · nets {ctx.included_nets}/"
            f"{ctx.total_nets} · components {ctx.included_components}/{ctx.total_components}"
            + (f" · anonymised: {', '.join(ctx.anonymized)}" if ctx.anonymized else "")
            + " · the KiCad file itself is never uploaded"
        )

    def _update_buttons(self) -> None:
        busy = self.controller.busy
        reason = self.send_blocker()
        self.send_button.setEnabled(reason is None)
        self.send_button.setToolTip(reason or "Send the request to the selected provider")
        self.cancel_button.setEnabled(busy)
        self.preview_button.setEnabled(self.service.session is not None)
        p = self._proposal()
        open_valid = p is not None and p.state is CommandState.VALID and not p.stale
        self.approve_button.setEnabled(open_valid and not busy)
        self.reject_button.setEnabled(
            p is not None and p.state in (CommandState.VALID, CommandState.INVALID)
        )
        self.edit_button.setEnabled(p is not None and p.is_open and not p.stale)
        session = self.service.session
        autonomy = session.config.autonomy if session is not None else "approval_required"
        advisory = autonomy == "advisory"
        self.run_button.setEnabled(
            p is not None
            and p.state is CommandState.APPROVED
            and p.category is OperationCategory.ROUTING
            and not advisory
            and not busy
        )
        self.run_button.setToolTip(
            "Advisory mode: routing commands do not run" if advisory else self.run_button.toolTip()
        )
        self.plan_button.setVisible(autonomy == "batch_approval")
        self.plan_button.setEnabled(bool(self._plan_candidates()) and not busy)
        rounds_left = session is not None and session.planning_rounds < MAX_PLANNING_ROUNDS
        self.facts_button.setEnabled(
            bool(session and session.pending_fact_requests) and rounds_left and not busy
        )
        self.revise_button.setEnabled(
            bool(session and session.router_facts) and rounds_left and not busy
        )

    def send_blocker(self) -> str | None:
        """Why Send is disabled, or ``None`` if it is enabled."""
        if self.controller.busy:
            return "A request is already running (cancel it first)."
        if self.service.session is None:
            return "Open a board first."
        profile = self.current_profile()
        if profile is None:
            return "Configure an AI provider first."
        if not self.selected_model():
            return "Select or enter a model ID."
        if not self.prompt.toPlainText().strip():
            return "Type a request."
        st = self.service.status_of(profile).status
        if (
            st in (ConnectionStatus.PACKAGE_MISSING, ConnectionStatus.NOT_CONFIGURED)
            and profile.requires_api_key
        ):
            return self.service.status_of(profile).message
        return None

    def _update_usage(self) -> None:
        s = self.service.usage.summary()
        self.usage_label.setText(
            f"Session usage: {s.requests} request(s) · in {s.input_tokens:,} / "
            f"out {s.output_tokens:,} tokens · cost: unavailable"
        )

    # ================================================================ actions
    def _use_example(self, mode: AIMode, text: str) -> None:
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(mode.value))
        self.prompt.setPlainText(text)

    def _preview_context(self) -> None:
        session = self.service.session
        if session is None:
            return
        nets, comps = self._selection()
        ctx = session.build_context(self.prompt.toPlainText(), nets, comps)
        profile = self.current_profile()
        ContextPreviewDialog(ctx, profile.name if profile else "provider", self).exec()

    def send(self, followup: bool = False) -> bool:
        if self.send_blocker() is not None:
            return False
        if not followup and self.service.session is not None:
            self.service.session.planning_rounds = 0  # a new user prompt starts a new plan
        session = self.service.session
        profile = self.current_profile()
        assert session is not None and profile is not None
        prompt = self.prompt.toPlainText().strip()
        nets, comps = self._selection()
        if self.settings.ai.show_privacy_preview and not session.privacy_acknowledged:
            ctx = session.build_context(prompt, nets, comps)
            dlg = PrivacyDisclosureDialog(ctx, profile.name, self)
            if not self.confirm_privacy(dlg):
                self.status_label.setText("Request cancelled before sending.")
                return False
            if dlg.dont_show.isChecked():
                self.settings.ai.show_privacy_preview = False
        session.privacy_acknowledged = True
        mode = AIMode(self.mode_combo.currentData())
        try:
            self.controller.send(
                prompt,
                mode,
                profile,
                model=self.selected_model(),
                selected_nets=nets,
                selected_components=comps,
            )
        except (RuntimeError, ValueError) as exc:
            self.status_label.setText(str(exc))
            return False
        pending = session.interactions[-1]
        self.conversation.append(interaction_html(pending, session.proposals))
        self.prompt.clear()
        return True

    def confirm_privacy(self, dialog: PrivacyDisclosureDialog) -> bool:
        """Separate method so tests can answer the dialog."""
        return dialog.exec() == PrivacyDisclosureDialog.DialogCode.Accepted

    def _on_busy(self, busy: bool) -> None:
        self.progress.setVisible(busy)
        self._update_buttons()

    def _on_interaction(self, inter: Interaction) -> None:
        self.refresh_board()
        if inter.proposal_ids:
            self.select_proposal(inter.proposal_ids[-1])
            self.tabs.setCurrentIndex(1)  # show the new proposal preview
        self.refresh_status()
        self.historyChanged.emit()

    # ================================================================ proposals
    def _proposal(self) -> CommandProposal | None:
        session = self.service.session
        pid = self._current_proposal
        return session.proposals.get(pid) if session and pid else None

    def _refresh_proposals(self) -> None:
        session = self.service.session
        self.proposal_combo.blockSignals(True)
        self.proposal_combo.clear()
        if session is not None:
            for p in session.proposals.values():
                self.proposal_combo.addItem(
                    f"{p.current.operation.label} — {p.state.value.upper()}", p.proposal_id
                )
        count = self.proposal_combo.count()
        pending = sum(1 for p in session.proposals.values() if p.is_open) if session else 0
        self.tabs.setTabText(1, f"Proposals ({pending} open / {count})" if count else "Proposals")
        idx = self.proposal_combo.findData(self._current_proposal)
        if idx < 0 and self.proposal_combo.count():
            idx = self.proposal_combo.count() - 1
        self.proposal_combo.setCurrentIndex(idx)
        self.proposal_combo.blockSignals(False)
        self._on_proposal_selected()

    def select_proposal(self, proposal_id: str) -> None:
        self._current_proposal = proposal_id
        self._refresh_proposals()

    def _on_proposal_selected(self, *_: object) -> None:
        self._current_proposal = self.proposal_combo.currentData()
        p = self._proposal()
        session = self.service.session
        if p is None or session is None:
            self.proposal_view.setHtml(
                "<i>No proposal selected. Proposals appear here when the "
                "AI suggests a structured command.</i>"
            )
        else:
            self.proposal_view.setHtml(proposal_html(p, BoardFactService(session.board)))
            self.targetNetsChanged.emit(session._command_nets(p.current))
        self._update_buttons()

    def _dispatch_decision(self, command: object) -> None:
        result = self.bus.dispatch(command)  # type: ignore[arg-type]
        self.status_label.setText(result.message)
        self._refresh_proposals()
        self.refresh_board()
        self.historyChanged.emit()

    # ================================================================ Stage 7
    def _plan_candidates(self) -> list[str]:
        session = self.service.session
        if session is None or not session.interactions:
            return []
        last = session.interactions[-1]
        return [
            pid
            for pid in last.proposal_ids
            if (p := session.proposals.get(pid)) is not None
            and p.state is CommandState.VALID
            and not p.stale
            and p.current.operation in (Operation.ROUTE_NET, Operation.ROUTE_GROUP)
        ]

    def _run(self) -> None:
        if (p := self._proposal()) is not None:
            self.runRequested.emit([p.proposal_id])

    def _run_plan(self) -> bool:
        ids = self._plan_candidates()
        if not ids:
            return False
        for pid in ids:
            result = self.bus.dispatch(ApproveProposalCommand(pid))
            if not result.success:
                self.status_label.setText(result.message)
                return False
        self._refresh_proposals()
        self.historyChanged.emit()
        self.runRequested.emit(ids)
        return True

    def send_facts(self) -> bool:
        session = self.service.session
        if session is None or not session.pending_fact_requests:
            return False
        session.answer_fact_requests()
        return self._followup(
            "Here are the deterministic board facts you requested (FACT lines in the session "
            "state). Continue with the original request."
        )

    def ask_revision(self) -> bool:
        return self._followup(
            "The deterministic router reported the ROUTER_RESULT lines in the session state. "
            "Propose a revised plan that stays within the board rules, or explain why no "
            "change is possible."
        )

    def _followup(self, text: str) -> bool:
        session = self.service.session
        if session is None or session.planning_rounds >= MAX_PLANNING_ROUNDS:
            self.status_label.setText("Planning round limit reached; ask a new question.")
            return False
        session.planning_rounds += 1
        self.prompt.setPlainText(text)
        return self.send(followup=True)

    def _approve(self) -> None:
        if (p := self._proposal()) is not None:
            self._dispatch_decision(ApproveProposalCommand(p.proposal_id))

    def _reject(self) -> None:
        if (p := self._proposal()) is not None:
            self._dispatch_decision(RejectProposalCommand(p.proposal_id))

    def _edit(self) -> None:
        p = self._proposal()
        if p is None:
            return
        dlg = ConstraintEditorDialog(p.current.effective_constraints, self)
        if self.run_editor(dlg) and dlg.result_constraints is not None:
            self._dispatch_decision(EditProposalCommand(p.proposal_id, dlg.result_constraints))

    def run_editor(self, dialog: ConstraintEditorDialog) -> bool:
        """Separate method so tests can drive the editor."""
        return dialog.exec() == ConstraintEditorDialog.DialogCode.Accepted
