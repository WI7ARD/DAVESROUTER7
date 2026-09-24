"""Command pattern: every UI/CLI/AI action goes through the :class:`CommandBus`."""

from __future__ import annotations

from pcbrouter.commands.base import BaseCommand, CommandContext, CommandResult
from pcbrouter.commands.board_commands import (
    BoardSummaryCommand,
    CloseBoardCommand,
    OpenBoardCommand,
    ValidateAICommand,
    board_summary,
)
from pcbrouter.commands.command_bus import READ_ONLY_MESSAGE, CommandBus

__all__ = [
    "READ_ONLY_MESSAGE",
    "BaseCommand",
    "BoardSummaryCommand",
    "CloseBoardCommand",
    "CommandBus",
    "CommandContext",
    "CommandResult",
    "OpenBoardCommand",
    "ValidateAICommand",
    "board_summary",
]
