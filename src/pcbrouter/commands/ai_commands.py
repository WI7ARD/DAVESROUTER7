"""AI proposal decisions as bus commands.

Approving/rejecting/editing a proposal changes the *planning constraint state* of
the AI session and is recorded in history. None of these commands touch board
geometry, so ``modifies_board`` stays ``False``; routing execution is Stage 4+.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from pcbrouter.ai.command_schema import RoutingConstraints
from pcbrouter.ai.proposals import ProposalStateError
from pcbrouter.ai.session import AISession
from pcbrouter.commands.base import BaseCommand, CommandContext, CommandResult


def _session(ctx: CommandContext) -> AISession | None:
    return ctx.ai.session if ctx.ai is not None else None


@dataclass(frozen=True)
class ApproveProposalCommand(BaseCommand):
    proposal_id: str
    name: ClassVar[str] = "ai_approve_proposal"

    def execute(self, ctx: CommandContext) -> CommandResult:
        session = _session(ctx)
        if session is None:
            return CommandResult.fail("No AI session is active (open a board first).")
        try:
            p = session.approve(self.proposal_id)
        except ProposalStateError as exc:
            return CommandResult.fail(f"Cannot approve: {exc}")
        return CommandResult.ok(p.status_note, p)


@dataclass(frozen=True)
class RejectProposalCommand(BaseCommand):
    proposal_id: str
    name: ClassVar[str] = "ai_reject_proposal"

    def execute(self, ctx: CommandContext) -> CommandResult:
        session = _session(ctx)
        if session is None:
            return CommandResult.fail("No AI session is active.")
        try:
            p = session.reject(self.proposal_id)
        except ProposalStateError as exc:
            return CommandResult.fail(f"Cannot reject: {exc}")
        return CommandResult.ok("Proposal rejected.", p)


@dataclass(frozen=True)
class EditProposalCommand(BaseCommand):
    proposal_id: str
    constraints: RoutingConstraints
    name: ClassVar[str] = "ai_edit_proposal"

    def execute(self, ctx: CommandContext) -> CommandResult:
        session = _session(ctx)
        if session is None:
            return CommandResult.fail("No AI session is active.")
        try:
            p = session.edit(self.proposal_id, self.constraints)
        except ProposalStateError as exc:
            return CommandResult.fail(f"Cannot edit: {exc}")
        status = p.validation.status.label if p.validation else p.state.value
        return CommandResult.ok(f"Constraints updated; re-validated: {status}.", p)
