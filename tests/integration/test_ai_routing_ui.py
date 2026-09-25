"""Stage 7 in the desktop UI: approved AI routing command → router → preview → accept."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from pcbrouter.ai.proposals import CommandState
from pcbrouter.ai.requests import AIMode
from pcbrouter.ai.retry import RetryPolicy
from pcbrouter.ai.runner import execute_request
from pcbrouter.routing.result import RouteStatus
from pcbrouter.settings import SettingsStore
from pcbrouter.ui.main_window import MainWindow
from tests.integration.test_routing_ui import wait
from tests.integration.test_ui import make_window
from tests.support.mock_provider import MockAIProvider, Scenario
from tests.unit.test_ai_planner import payload, route_a

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot: QtBot, tmp_path: Path) -> Iterator[MainWindow]:
    w = make_window(qtbot, SettingsStore(tmp_path / "config" / "settings.json"))
    yield w
    w.close()


def answer(window: MainWindow, data: dict[str, object]) -> None:
    session = window.ai_service.session
    assert session is not None
    prepared = session.prepare(
        "Route A with at most two vias", AIMode.COMMAND, model="mock", provider_name="Mock"
    )
    provider = MockAIProvider(scenario=Scenario.CUSTOM, payload=data)
    response = asyncio.run(execute_request(provider, prepared.request, RetryPolicy(max_retries=0)))
    inter = session.accept_response(prepared, response)
    window.ai_panel._on_interaction(inter)


def test_ai_route_command_runs_and_is_reviewed(
    window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    assert window.open_board(fixture_path("router_basic.kicad_pcb"))
    wait(window)
    answer(window, payload([route_a(2)]))
    panel = window.ai_panel
    p = panel._proposal()
    assert p is not None and p.state is CommandState.VALID
    assert not panel.run_button.isEnabled()  # approval first
    panel._approve()
    assert p.state is CommandState.APPROVED and panel.run_button.isEnabled()
    panel._run()
    wait(window)
    result = window.routing_ui.panel.result
    assert result is not None and result.status is RouteStatus.SUCCESS
    assert p.state is CommandState.EXECUTED
    working = window.bus.context.project.working
    assert not working.modified  # preview only
    assert window.routing_ui.accept(window.routing_ui.panel.current)
    wait(window)
    assert working.modified
    session = window.ai_service.session
    assert session is not None and session.board is working.board  # AI follows the board
    assert panel.revise_button.isEnabled()  # router facts can go back to the planner
