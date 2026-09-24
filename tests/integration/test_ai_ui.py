"""AI GUI behaviour (spec item 53): real widgets, MockAIProvider, in-memory keyring."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from pcbrouter.ai.credentials import CredentialService
from pcbrouter.ai.models import ConnectionStatus
from pcbrouter.ai.profiles import ProviderKind, ProviderProfile
from pcbrouter.ai.proposals import CommandState
from pcbrouter.ai.provider_registry import ProviderRegistry
from pcbrouter.ai.requests import AIMode
from pcbrouter.ai.service import AIService
from pcbrouter.app.application import build_services
from pcbrouter.settings import SettingsStore
from pcbrouter.ui.ai_dialogs import EDITABLE_FIELDS, ConstraintEditorDialog, PrivacyDisclosureDialog
from pcbrouter.ui.main_window import AI_SETTINGS_TAB, MainWindow
from pcbrouter.ui.settings_dialog import SettingsDialog
from tests.support.keyrings import MemoryKeyring
from tests.support.mock_provider import FAKE_KEY, MockAIProvider, Scenario

pytestmark = pytest.mark.gui


class Mocks:
    """Which mock scenario the next provider instance uses (per kind)."""

    def __init__(self) -> None:
        self.scenario = Scenario.COMMAND
        self.delay_s = 0.0
        self.instances: list[MockAIProvider] = []

    def factory(self, profile: ProviderProfile, _creds: CredentialService) -> MockAIProvider:
        m = MockAIProvider(profile, self.scenario, delay_s=self.delay_s)
        self.instances.append(m)
        return m


@pytest.fixture
def mocks() -> Mocks:
    return Mocks()


@pytest.fixture
def store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(tmp_path / "config" / "settings.json")


def make_window(qtbot: QtBot, store: SettingsStore, mocks: Mocks) -> MainWindow:
    settings = store.load()
    creds = CredentialService()  # OS keyring == the in-memory test backend
    registry = ProviderRegistry(creds, {k: mocks.factory for k in ProviderKind})
    services = build_services(settings, detect_gpu_now=False, ai_service=AIService(creds, registry))
    w = MainWindow(
        bus=services.bus, compute=services.compute, settings=settings, settings_store=store
    )
    qtbot.addWidget(w)
    w.resize(1500, 950)
    w.show()
    qtbot.waitExposed(w)
    return w


@pytest.fixture
def window(
    qtbot: QtBot, store: SettingsStore, mocks: Mocks, memory_keyring: MemoryKeyring
) -> Iterator[MainWindow]:
    w = make_window(qtbot, store, mocks)
    yield w
    w.ai_service.shutdown()
    w.close()


def configure_profile(
    window: MainWindow, kind: ProviderKind = ProviderKind.OPENAI, model: str = "gpt-test"
) -> ProviderProfile:
    """Drive the real provider settings widget, then accept the dialog."""
    dlg = SettingsDialog(
        window.settings, window.compute, window, ai_controller=window.ai_controller
    )
    w = dlg.ai_widget
    profile = w.add_profile(kind)
    w.name_edit.setText("OpenAI - Main")
    w.name_edit.editingFinished.emit()
    w.model_combo.setEditText(model)
    w.key_edit.setText(FAKE_KEY)
    w.save_key()
    assert w.key_edit.text() == ""  # typed key is cleared from the widget immediately
    new = dlg.result_settings()
    dlg.done(SettingsDialog.DialogCode.Accepted)
    window.settings = new
    window.ai_panel.settings = new
    window.ai_panel.refresh_profiles()
    window.save_settings()
    return profile


def open_can_board(window: MainWindow, fixture_path: Callable[[str], Path]) -> None:
    assert window.open_board(fixture_path("can_node.kicad_pcb"))


def send(
    qtbot: QtBot, window: MainWindow, prompt: str, mode: AIMode = AIMode.COMMAND, wait: bool = True
) -> None:
    panel = window.ai_panel
    setattr(panel, "confirm_privacy", lambda dlg: True)  # noqa: B010 - test hook
    panel.mode_combo.setCurrentIndex(panel.mode_combo.findData(mode.value))
    panel.prompt.setPlainText(prompt)
    assert panel.send(), panel.send_blocker()
    if wait:
        qtbot.waitUntil(lambda: not window.ai_controller.busy, timeout=5000)


# ------------------------------------------------------------------ first run / offline
def test_first_run_and_offline_mode(
    qtbot: QtBot, window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    panel = window.ai_panel
    assert panel.stack.currentIndex() == 0  # empty state
    empty_page = panel.stack.widget(0)
    assert empty_page is not None
    labels = [lbl.text() for lbl in empty_page.findChildren(type(panel.status_label))]
    assert any("cannot directly modify your PCB" in t for t in labels)
    assert "No AI provider configured" in panel.empty_status.text()
    opened: list[int | None] = []

    def fake_dialog(dlg: SettingsDialog) -> bool:
        opened.append(dlg.tabs.currentIndex())
        return False

    setattr(window, "run_settings_dialog", fake_dialog)  # noqa: B010
    panel.configure_button.click()
    assert opened == [AI_SETTINGS_TAB]
    open_can_board(window, fixture_path)  # board inspection unaffected by missing AI
    assert window.canvas.render_stats.by_kind["pad"] == 18
    assert panel.send_blocker() == "Configure an AI provider first."
    assert window.ai_service._runner is None  # no AI thread or network activity at all


# ------------------------------------------------------------------ provider configuration
def test_provider_configuration_and_key_storage(
    qtbot: QtBot, window: MainWindow, memory_keyring: MemoryKeyring, store: SettingsStore
) -> None:
    profile = configure_profile(window)
    assert window.ai_panel.stack.currentIndex() == 1
    assert window.ai_panel.provider_combo.count() == 1
    # Key is in the (test) OS credential store under the profile's reference only.
    assert memory_keyring.data[("ai-pcb-router", profile.credential_ref)] == FAKE_KEY
    raw = store.path.read_text(encoding="utf-8")
    assert FAKE_KEY not in raw and "sk-test" not in raw and profile.credential_ref in raw
    masked = window.ai_service.credentials.masked(profile.credential_ref)
    assert masked is not None and FAKE_KEY not in masked


def test_connection_test_updates_status_indicator(qtbot: QtBot, window: MainWindow) -> None:
    configure_profile(window)
    dlg = SettingsDialog(
        window.settings, window.compute, window, ai_controller=window.ai_controller
    )
    w = dlg.ai_widget
    with qtbot.waitSignal(window.ai_controller.connectionTested, timeout=5000):
        w.test_connection()
    assert "Connected" in w.test_status.text()
    dlg.done(SettingsDialog.DialogCode.Rejected)
    window.ai_panel.refresh_status()
    assert "Connected" in window.ai_panel.provider_status.text()
    assert "Connected" in window.lbl_ai.text()


def test_cancelled_settings_dialog_deletes_new_keys(
    window: MainWindow, memory_keyring: MemoryKeyring
) -> None:
    dlg = SettingsDialog(
        window.settings, window.compute, window, ai_controller=window.ai_controller
    )
    p = dlg.ai_widget.add_profile(ProviderKind.ANTHROPIC)
    dlg.ai_widget.key_edit.setText(FAKE_KEY)
    dlg.ai_widget.save_key()
    assert ("ai-pcb-router", p.credential_ref) in memory_keyring.data
    dlg.done(SettingsDialog.DialogCode.Rejected)
    assert ("ai-pcb-router", p.credential_ref) not in memory_keyring.data  # no orphan secret
    assert window.settings.ai.profiles == []


def test_compatible_profile_without_key(window: MainWindow) -> None:
    dlg = SettingsDialog(
        window.settings, window.compute, window, ai_controller=window.ai_controller
    )
    p = dlg.ai_widget.add_profile(ProviderKind.OPENAI_COMPATIBLE)
    assert p.base_url == "http://localhost:1234/v1" and not p.requires_api_key
    assert dlg.ai_widget.base_url_edit.isEnabled() and dlg.ai_widget.requires_key.isEnabled()
    dlg.done(SettingsDialog.DialogCode.Rejected)


# ------------------------------------------------------------------ send states & privacy
def test_send_button_states(
    qtbot: QtBot, window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    configure_profile(window)
    panel = window.ai_panel
    assert panel.send_blocker() == "Open a board first."
    open_can_board(window, fixture_path)
    assert panel.send_blocker() == "Type a request."
    panel.prompt.setPlainText("Analyze this board.")
    assert panel.send_blocker() is None and panel.send_button.isEnabled()
    panel.model_combo.setEditText("")
    assert panel.send_blocker() == "Select or enter a model ID."
    panel.model_combo.setEditText("gpt-test")
    assert "gpt-test" in panel.context_label.text() or "tokens" in panel.context_label.text()


def test_privacy_disclosure_once_per_board(
    qtbot: QtBot, window: MainWindow, mocks: Mocks, fixture_path: Callable[[str], Path]
) -> None:
    configure_profile(window)
    open_can_board(window, fixture_path)
    panel = window.ai_panel
    shown: list[PrivacyDisclosureDialog] = []

    def decline(dlg: PrivacyDisclosureDialog) -> bool:
        shown.append(dlg)
        return False

    setattr(panel, "confirm_privacy", decline)  # noqa: B010
    panel.prompt.setPlainText("Analyze this board.")
    assert not panel.send()
    assert len(shown) == 1 and not mocks.instances  # nothing sent
    text = shown[0].findChildren(type(panel.status_label))[0].text()
    assert "will <b>NOT</b> be uploaded" in text and "net names" in text

    def accept(dlg: PrivacyDisclosureDialog) -> bool:
        shown.append(dlg)
        return True

    setattr(panel, "confirm_privacy", accept)  # noqa: B010
    assert panel.send()
    qtbot.waitUntil(lambda: not window.ai_controller.busy, timeout=5000)
    panel.prompt.setPlainText("Again")
    assert panel.send()
    assert len(shown) == 2  # not shown for the second request on the same board


# ------------------------------------------------------------------ full workflow
def test_analyze_then_command_approve_reject_edit(
    qtbot: QtBot,
    window: MainWindow,
    mocks: Mocks,
    tmp_path: Path,
    fixture_path: Callable[[str], Path],
) -> None:
    configure_profile(window)
    (tmp_path / "project").mkdir()
    board_file = tmp_path / "project" / "can_node.kicad_pcb"
    shutil.copy2(fixture_path("can_node.kicad_pcb"), board_file)
    sha_before = hashlib.sha256(board_file.read_bytes()).hexdigest()
    assert window.open_board(board_file)
    stats_before = window.canvas.render_stats.by_kind
    fp_before = window.canvas.board.fingerprint  # type: ignore[union-attr]
    panel = window.ai_panel

    mocks.scenario = Scenario.ANALYSIS
    send(qtbot, window, "Analyze this board.", AIMode.ANALYZE)
    conv = panel.conversation.toPlainText()
    assert "CAN_L has one via" in conv and "[AI OBSERVATION]" in conv
    assert "Potential concerns (inferred, not verified)" in conv
    sent = mocks.instances[-1].requests[-1]
    assert "<pcb_context>" in sent.messages[-1].content
    assert "(kicad_pcb" not in json.dumps([m.content for m in sent.messages])  # no raw upload

    mocks.scenario = Scenario.COMMAND
    send(qtbot, window, "Route CAN first and minimize vias.")
    assert panel.tabs.currentIndex() == 1  # proposal preview shown
    html = panel.proposal_view.toPlainText()
    for needle in (
        "Route Group",
        "net group CAN_H, CAN_L",
        "Minimize vias",
        "Yes",
        "[BOARD FACT]",
        "CAN_L: 2 pads, 2 tracks, 1 vias",
        "[AI OBSERVATION]",
        "[DRC RESULT]",
        "Nothing here is a DRC result",
        "Validation: VALID WITH WARNINGS",
        "✓ Referenced nets and components exist",
        "SWITCHING_POWER",
    ):
        assert needle in html, needle
    assert panel.approve_button.isEnabled()

    # Edit constraints before approval (item 41).
    def edit(dlg: ConstraintEditorDialog) -> bool:
        dlg._widgets["max_vias"].setValue(2)  # type: ignore[attr-defined]
        dlg._accept()
        return dlg.result_constraints is not None

    setattr(panel, "run_editor", edit)  # noqa: B010
    panel.edit_button.click()
    assert "[USER CONSTRAINT]" in panel.proposal_view.toPlainText()
    assert "max_vias: 4 → 2" in panel.proposal_view.toPlainText()

    panel.approve_button.click()
    session = window.ai_service.session
    assert session is not None
    approved = [p for p in session.proposals.values() if p.state is CommandState.APPROVED]
    assert len(approved) == 1 and approved[0].user_modified
    assert "Stage 4" in panel.status_label.text()
    assert window.ai_history.decision_labels() == ["AI Approve: Route Group"]
    assert "APPROVED" in _tree_text(window)

    send(qtbot, window, "Also route UART.")
    panel.reject_button.click()
    assert any(p.state is CommandState.REJECTED for p in session.proposals.values())
    assert window.ai_history.decision_labels()[-1] == "AI Reject: Route Group"

    # Undo from the Edit menu works on decisions only.
    window.act_undo.trigger()
    assert any(p.state is CommandState.VALID for p in session.proposals.values())

    # NO geometry change, NO file change (items 22 and 72).
    assert window.canvas.board.fingerprint == fp_before  # type: ignore[union-attr]
    assert window.canvas.render_stats.by_kind == stats_before
    window.close_board()
    assert hashlib.sha256(board_file.read_bytes()).hexdigest() == sha_before
    assert sorted(p.name for p in board_file.parent.iterdir()) == ["can_node.kicad_pcb"]


def _tree_text(window: MainWindow) -> str:
    tree = window.ai_history.tree
    out = []
    for i in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(i)
        assert item is not None
        out.append(" ".join(item.text(c) for c in range(5)))
        for j in range(item.childCount()):
            child = item.child(j)
            assert child is not None
            out.append(" ".join(child.text(c) for c in range(5)))
    return "\n".join(out)


def test_invalid_proposal_cannot_be_approved(
    qtbot: QtBot, window: MainWindow, mocks: Mocks, fixture_path: Callable[[str], Path]
) -> None:
    configure_profile(window)
    open_can_board(window, fixture_path)
    mocks.scenario = Scenario.UNKNOWN_NET
    send(qtbot, window, "Route CAN_X")
    text = window.ai_panel.proposal_view.toPlainText()
    assert 'Target net "CAN_X" does not exist' in text and "Did you mean" in text
    assert "INVALID" in text
    assert not window.ai_panel.approve_button.isEnabled()
    assert window.ai_panel.reject_button.isEnabled()


def test_cancel_active_request(
    qtbot: QtBot, window: MainWindow, mocks: Mocks, fixture_path: Callable[[str], Path]
) -> None:
    configure_profile(window)
    open_can_board(window, fixture_path)
    mocks.scenario = Scenario.HANG
    panel = window.ai_panel
    send(qtbot, window, "Analyze this board.", AIMode.ANALYZE, wait=False)
    assert window.ai_controller.busy and panel.cancel_button.isEnabled()
    assert not panel.send_button.isEnabled()  # no double submission
    panel.prompt.setPlainText("second")
    assert panel.send_blocker() is not None and not panel.send()
    panel.cancel_button.click()
    assert not window.ai_controller.busy and panel.status_label.text() == "Cancelled."
    qtbot.wait(300)  # a late result must not appear
    conv = panel.conversation.toPlainText()
    assert conv.count("The request was cancelled.") == 1
    session = window.ai_service.session
    assert session is not None and session.conversation.turns == []
    mocks.scenario = Scenario.ANALYSIS
    send(qtbot, window, "Analyze this board.", AIMode.ANALYZE)  # UI usable again
    assert "CAN_L has one via" in panel.conversation.toPlainText()


def test_closing_board_during_request_attaches_nothing(
    qtbot: QtBot, window: MainWindow, mocks: Mocks, fixture_path: Callable[[str], Path]
) -> None:
    configure_profile(window)
    open_can_board(window, fixture_path)
    mocks.scenario, mocks.delay_s = Scenario.COMMAND, 0.4
    send(qtbot, window, "Route CAN first", wait=False)
    window.open_board(fixture_path("vias.kicad_pcb"))  # user switches boards mid-request
    time.sleep(0.6)
    qtbot.wait(100)
    session = window.ai_service.session
    assert session is not None and session.proposals == {}  # nothing leaked onto board B
    assert not window.ai_controller.busy


def test_provider_failure_is_friendly(
    qtbot: QtBot, window: MainWindow, mocks: Mocks, fixture_path: Callable[[str], Path]
) -> None:
    configure_profile(window)
    open_can_board(window, fixture_path)
    mocks.scenario = Scenario.AUTH_FAILURE
    send(qtbot, window, "Analyze", AIMode.ANALYZE)
    assert "Authentication failed" in window.ai_panel.conversation.toPlainText()
    assert "sk-live" not in window.ai_panel.conversation.toPlainText()
    profile = window.ai_panel.current_profile()
    assert profile is not None
    assert window.ai_service.status_of(profile).status is ConnectionStatus.AUTH_FAILED


def test_restart_retains_provider_configuration(
    qtbot: QtBot, store: SettingsStore, mocks: Mocks, memory_keyring: MemoryKeyring
) -> None:
    first = make_window(qtbot, store, mocks)
    configure_profile(first, model="gpt-keep")
    first.close()
    second = make_window(qtbot, store, mocks)
    profile = second.ai_panel.current_profile()
    assert profile is not None and profile.model_id == "gpt-keep"
    assert second.ai_service.credentials.get(profile.credential_ref) is not None
    assert FAKE_KEY not in store.path.read_text(encoding="utf-8")
    second.close()


def test_export_session(
    qtbot: QtBot,
    window: MainWindow,
    mocks: Mocks,
    tmp_path: Path,
    fixture_path: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pcbrouter.ui import dialogs

    errors: list[str] = []
    monkeypatch.setattr(dialogs, "show_error", lambda _p, title, *a, **k: errors.append(title))
    configure_profile(window)
    open_can_board(window, fixture_path)
    send(qtbot, window, "Route CAN first")
    target = tmp_path / "session.json"
    assert window.write_ai_export(target)
    text = target.read_text(encoding="utf-8")
    data = json.loads(text)
    assert data["interactions"][0]["prompt"] == "Route CAN first"
    assert FAKE_KEY not in text and "Authorization" not in text
    assert not window.write_ai_export(tmp_path / "x.kicad_pcb")  # never writes a board name
    assert errors == ["Export refused"] and not (tmp_path / "x.kicad_pcb").exists()


def test_usage_view(
    qtbot: QtBot, window: MainWindow, mocks: Mocks, fixture_path: Callable[[str], Path]
) -> None:
    from pcbrouter.ui.ai_dialogs import UsageDialog

    configure_profile(window)
    open_can_board(window, fixture_path)
    send(qtbot, window, "Analyze", AIMode.ANALYZE)
    assert "1 request(s)" in window.ai_panel.usage_label.text()
    dlg = UsageDialog(window.ai_service.usage, window)
    assert dlg.table.rowCount() == 1 and dlg.table.item(0, 2).text() == "1,200"  # type: ignore[union-attr]


def test_constraint_editor_covers_every_field() -> None:
    from pcbrouter.ai.command_schema import RoutingConstraints

    assert set(EDITABLE_FIELDS) == set(RoutingConstraints.model_fields)
    full = RoutingConstraints(
        preferred_layers=["F.Cu"],
        forbidden_layers=["B.Cu"],
        min_trace_width_mm=0.1,
        preferred_trace_width_mm=0.2,
        max_trace_width_mm=0.3,
        min_clearance_mm=0.15,
        max_vias=3,
        minimize_vias=True,
        preferred_via_type="through",
        preserve_existing_routes=True,
        allow_ripup=False,
        allow_component_movement=False,
        priority="high",
        criticality="high",
        max_length_mm=50,
        target_length_mm=40,
        length_tolerance_mm=1,
        avoid_nets=["VBAT"],
        avoid_net_classes=["POWER"],
        keep_near=[{"kind": "component", "name": "U2"}],
        keep_away_from=[{"kind": "net", "name": "SW_NODE"}],
        differential_pair={"positive_net": "CAN_H", "negative_net": "CAN_L"},
        pair_gap_mm=0.2,
        pair_skew_tolerance_mm=0.1,
        impedance_target_ohm=120,
        shielding_preference="prefer_ground_reference",
        additional_notes="note",
    )
    dlg = ConstraintEditorDialog(full)
    dlg._accept()
    assert dlg.result_constraints == full  # lossless round trip through every widget


def test_remote_plain_http_endpoint_is_flagged(window: MainWindow) -> None:
    dlg = SettingsDialog(
        window.settings, window.compute, window, ai_controller=window.ai_controller
    )
    w = dlg.ai_widget
    w.add_profile(ProviderKind.OPENAI_COMPATIBLE)
    assert w.url_warning.text() == ""  # localhost is fine
    w.base_url_edit.setText("http://192.168.1.50:1234/v1")
    w.base_url_edit.editingFinished.emit()
    assert "unencrypted" in w.url_warning.text()
    dlg.done(SettingsDialog.DialogCode.Rejected)
