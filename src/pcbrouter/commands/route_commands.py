"""Routing commands (Stage 4+). Every route and every commit goes through the bus.

* :class:`RouteNetCommand` runs the deterministic router and returns candidates.
  It changes nothing.
* :class:`AcceptRouteCommand` commits one validated candidate to the *working
  board* (never the source file) and records an undoable history entry.
* :class:`WorkingUndoAction` makes commits part of the global undo/redo stack.

``modifies_board`` stays False: these commands change the in-memory working
board only. Writing a KiCad file is a separate, explicit export (Stage 9).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from pcbrouter.commands.base import BaseCommand, CommandContext, CommandResult
from pcbrouter.history.history import UndoableAction
from pcbrouter.routing.request import RouteRequest
from pcbrouter.routing.result import RouteCandidate
from pcbrouter.routing.working_board import Commit, CommitError, Provenance, WorkingBoard


class WorkingUndoAction(UndoableAction):
    """History entry for a working-board commit (apply = redo, revert = undo)."""

    def __init__(self, working: WorkingBoard, commit: Commit) -> None:
        self.working = working
        self.commit = commit

    @property
    def label(self) -> str:
        return self.commit.label

    @property
    def kind(self) -> str:
        return "working_board"

    @property
    def metadata(self) -> Mapping[str, Any]:
        return {
            "commit": self.commit.commit_id,
            "diff": self.commit.diff_summary(),
            "nets": self.commit.nets,
        }

    def apply(self) -> None:
        self.working.redo(self.commit)

    def revert(self) -> None:
        if self.working.commits and self.working.commits[-1] is self.commit:
            self.working.undo()
        else:
            raise CommitError("working board history is out of order")


def _working(ctx: CommandContext) -> WorkingBoard | None:
    return ctx.project.working


@dataclass
class RouteNetCommand(BaseCommand):
    request: RouteRequest
    cancel: threading.Event | None = None
    name: ClassVar[str] = "route_net"

    def execute(self, ctx: CommandContext) -> CommandResult:
        engine = ctx.project.engine
        if engine is None:
            return CommandResult.fail("Open a board first.")
        from pcbrouter.routing.backend import router_for

        router = router_for(engine, ctx.compute)
        result = router.route_net(self.request, cancel=self.cancel)
        return CommandResult(result.success, result.summary(), result)

    def describe(self) -> str:
        return f"route net {self.request.net}"


@dataclass
class AcceptRouteCommand(BaseCommand):
    candidate: RouteCandidate
    label: str = ""
    provenance: Provenance = Provenance.USER_ACCEPTED
    name: ClassVar[str] = "accept_route"

    def execute(self, ctx: CommandContext) -> CommandResult:
        working = _working(ctx)
        if working is None:
            return CommandResult.fail("Open a board first.")
        if not self.candidate.legal:
            return CommandResult.fail("Only validated candidates can be accepted.")
        label = self.label or f"Route accepted: {self.candidate.proposal.net}"
        try:
            commit = working.commit_proposals(
                [self.candidate.proposal],
                label,
                self.provenance,
                metadata={
                    "score": self.candidate.score.to_dict(),
                    "proposal": self.candidate.proposal.proposal_id,
                },
            )
        except CommitError as exc:
            return CommandResult.fail(str(exc))
        ctx.history.push(WorkingUndoAction(working, commit), already_applied=True)
        return CommandResult.ok(f"{label} ({commit.diff_summary()})", commit)

    def describe(self) -> str:
        return f"accept route {self.candidate.proposal.net}"


@dataclass
class ResetWorkingBoardCommand(BaseCommand):
    name: ClassVar[str] = "reset_working_board"

    def execute(self, ctx: CommandContext) -> CommandResult:
        working = _working(ctx)
        if working is None:
            return CommandResult.fail("Open a board first.")
        n = len(working.commits)
        working.reset()
        ctx.history.clear()
        return CommandResult.ok(f"Working board reset to the source ({n} commit(s) removed).")
