from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest

from pcbrouter.commands import (
    READ_ONLY_MESSAGE,
    BaseCommand,
    BoardSummaryCommand,
    CloseBoardCommand,
    CommandBus,
    CommandContext,
    CommandResult,
    OpenBoardCommand,
    ValidateAICommand,
)
from pcbrouter.history import HistoryManager, MetadataAction
from pcbrouter.project import ProjectManager


@pytest.fixture
def bus() -> CommandBus:
    return CommandBus(CommandContext(project=ProjectManager(), history=HistoryManager()))


@dataclass(frozen=True)
class Echo(BaseCommand):
    text: str
    name: ClassVar[str] = "echo"

    def execute(self, ctx: CommandContext) -> CommandResult:
        return CommandResult.ok(self.text)


class Explodes(BaseCommand):
    name = "explodes"

    def execute(self, ctx: CommandContext) -> CommandResult:
        raise ZeroDivisionError("boom")


class Modifies(BaseCommand):
    name = "modifies"
    modifies_board = True
    ran = False

    def execute(self, ctx: CommandContext) -> CommandResult:
        Modifies.ran = True
        return CommandResult.ok("modified")


def test_dispatch_and_listeners(bus: CommandBus) -> None:
    seen: list[tuple[str, bool]] = []
    bus.subscribe(lambda cmd, res: seen.append((cmd.name, res.success)))
    result = bus.dispatch(Echo("hi"))
    assert result.success and result.message == "hi" and result.duration_s >= 0
    assert seen == [("echo", True)]


def test_exceptions_become_failed_results(bus: CommandBus) -> None:
    result = bus.dispatch(Explodes())
    assert not result.success
    assert "boom" in result.message and result.error_detail


def test_read_only_mode_blocks_modifying_commands(bus: CommandBus) -> None:
    result = bus.dispatch(Modifies())
    assert not result.success and result.message == READ_ONLY_MESSAGE
    assert Modifies.ran is False


def test_board_commands(bus: CommandBus, fixture_path: Callable[[str], Path]) -> None:
    assert not bus.dispatch(BoardSummaryCommand()).success  # nothing open
    bus.context.history.push(MetadataAction("stale"))
    opened = bus.dispatch(OpenBoardCommand(fixture_path("vias.kicad_pcb")))
    assert opened.success and "3 vias" in opened.message
    assert not bus.context.history.can_undo  # history reset per board
    summary = bus.dispatch(BoardSummaryCommand())
    assert summary.data["counts"]["vias"] == 3
    assert summary.data["size_mm"] == [30.0, 20.0]
    closed = bus.dispatch(CloseBoardCommand())
    assert closed.success and "verified unchanged" in closed.message
    assert bus.dispatch(CloseBoardCommand()).message == "No board was open."


def test_load_errors_surface_user_message(bus: CommandBus, tmp_path: Path) -> None:
    result = bus.dispatch(OpenBoardCommand(tmp_path / "missing.kicad_pcb"))
    assert not result.success
    assert result.message.startswith("File not found")
    assert result.error_detail and "does not exist" in result.error_detail


def test_validate_ai_command(bus: CommandBus, fixture_path: Callable[[str], Path]) -> None:
    good = '{"operation": "route_net", "target": "GND"}'
    assert bus.dispatch(ValidateAICommand(good)).success  # syntactic only: no board
    bus.dispatch(OpenBoardCommand(fixture_path("four_layer.kicad_pcb")))
    ok = bus.dispatch(ValidateAICommand(good))
    assert ok.success and "later stage" in ok.message
    unknown = bus.dispatch(ValidateAICommand('{"operation": "route_net", "target": "CAN_H"}'))
    assert not unknown.success and "CAN_H" in unknown.message
    invalid = bus.dispatch(ValidateAICommand('{"operation": "route_net", "target": "GND", "x": 1}'))
    assert not invalid.success and "Invalid command" in invalid.message
    analysis = bus.dispatch(ValidateAICommand('{"operation": "analyze_board"}'))
    assert analysis.success and "later stage" not in analysis.message
