"""Read-only board commands available in Stage 1."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from pcbrouter.ai.command_parser import CommandValidationError, parse_command_payload
from pcbrouter.ai.command_schema import OperationCategory
from pcbrouter.ai.command_validator import SemanticValidator
from pcbrouter.commands.base import BaseCommand, CommandContext, CommandResult
from pcbrouter.domain.board import Board
from pcbrouter.domain.units import internal_to_mm


def board_summary(board: Board) -> dict[str, Any]:
    """JSON-friendly summary used by the CLI and (future) AI context builder."""
    s = board.statistics
    md = board.metadata
    return {
        "file": str(md.source_path) if md.source_path else None,
        "format_version": md.format_version,
        "kicad_version_guess": md.kicad_major_version_guess,
        "generator": md.generator,
        "title": md.title,
        "size_mm": (
            [round(internal_to_mm(s.width), 4), round(internal_to_mm(s.height), 4)]
            if s.width is not None and s.height is not None
            else None
        ),
        "copper_layers": board.copper_layer_names,
        "counts": {
            "footprints": s.footprint_count,
            "pads": s.pad_count,
            "nets": s.net_count,
            "tracks": s.track_count,
            "arc_tracks": s.arc_track_count,
            "vias": s.via_count,
        },
        "total_track_length_mm": round(internal_to_mm(s.total_track_length), 4),
    }


@dataclass(frozen=True)
class OpenBoardCommand(BaseCommand):
    path: Path
    name: ClassVar[str] = "open_board"

    def execute(self, ctx: CommandContext) -> CommandResult:
        session = ctx.project.open_board(self.path)
        ctx.history.clear()
        s = session.board.statistics
        return CommandResult.ok(
            f"Opened {session.name}: {s.footprint_count} footprints, {s.pad_count} pads, "
            f"{s.net_count} nets, {s.track_count} tracks, {s.via_count} vias",
            session,
        )

    def describe(self) -> str:
        return f"open_board {self.path}"


@dataclass(frozen=True)
class CloseBoardCommand(BaseCommand):
    name: ClassVar[str] = "close_board"

    def execute(self, ctx: CommandContext) -> CommandResult:
        report = ctx.project.close_board()
        ctx.history.clear()
        if report is None:
            return CommandResult.ok("No board was open.")
        note = {
            True: "source file verified unchanged",
            False: "source file was changed by another program while open",
            None: "source file could not be re-read for verification",
        }[report.source_unchanged]
        return CommandResult.ok(f"Closed {report.source_path.name} ({note}).", report)


@dataclass(frozen=True)
class BoardSummaryCommand(BaseCommand):
    name: ClassVar[str] = "board_summary"

    def execute(self, ctx: CommandContext) -> CommandResult:
        board = ctx.project.board
        if board is None:
            return CommandResult.fail("No board is open.")
        return CommandResult.ok("Board summary", board_summary(board))


@dataclass(frozen=True)
class ValidateAICommand(BaseCommand):
    """Parse + validate a structured command (as an LLM would emit) WITHOUT executing it.

    Runs the same schema parser and semantic validator as the AI Engineering panel,
    so the CLI (``--check-command``) and the GUI apply identical rules.
    """

    payload: str
    name: ClassVar[str] = "validate_ai_command"

    def execute(self, ctx: CommandContext) -> CommandResult:
        try:
            command = parse_command_payload(self.payload)
        except CommandValidationError as exc:
            return CommandResult.fail("Invalid command: " + "; ".join(exc.errors))
        board = ctx.project.board
        warnings = ""
        if board is not None:
            report = SemanticValidator(board, engine=ctx.project.engine).validate(command)
            if not report.is_valid:
                message = "Command does not match the board: " + "; ".join(
                    i.text() for i in report.errors
                )
                return CommandResult(False, message, report)
            if report.warnings:
                warnings = " Warnings: " + "; ".join(i.text() for i in report.warnings)
        note = (
            " It would modify the board; execution is available in a later stage (Stage 4)."
            if command.operation.category is OperationCategory.ROUTING
            else ""
        )
        return CommandResult.ok(
            f"Valid '{command.operation.value}' command.{note}{warnings}", command
        )
