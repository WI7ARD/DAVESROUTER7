"""Command pattern: every UI/CLI/AI action goes through the :class:`CommandBus`."""

from __future__ import annotations

from pcbrouter.commands.ai_commands import (
    ApproveProposalCommand,
    EditProposalCommand,
    RejectProposalCommand,
)
from pcbrouter.commands.base import BaseCommand, CommandContext, CommandResult
from pcbrouter.commands.board_commands import (
    BoardSummaryCommand,
    CloseBoardCommand,
    OpenBoardCommand,
    ValidateAICommand,
    board_summary,
)
from pcbrouter.commands.command_bus import READ_ONLY_MESSAGE, CommandBus
from pcbrouter.commands.route_commands import (
    AcceptRouteCommand,
    ResetWorkingBoardCommand,
    RouteNetCommand,
    WorkingUndoAction,
)

__all__ = [
    "READ_ONLY_MESSAGE",
    "AcceptRouteCommand",
    "ApproveProposalCommand",
    "BaseCommand",
    "BoardSummaryCommand",
    "CloseBoardCommand",
    "CommandBus",
    "CommandContext",
    "CommandResult",
    "EditProposalCommand",
    "OpenBoardCommand",
    "RejectProposalCommand",
    "ResetWorkingBoardCommand",
    "RouteNetCommand",
    "ValidateAICommand",
    "WorkingUndoAction",
    "board_summary",
]
