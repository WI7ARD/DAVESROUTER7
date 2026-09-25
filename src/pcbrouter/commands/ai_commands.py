"""AI proposal decisions as bus commands.

Approving/rejecting/editing a proposal changes the *planning constraint state* of
the AI session and is recorded in history. None of these commands touch board
geometry, so ``modifies_board`` stays ``False``. Approved routing commands run through
:class:`ExecuteAIProposalCommand` (router results only; the user accepts separately).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, ClassVar

from pcbrouter.ai.command_schema import RoutingConstraints
from pcbrouter.ai.proposals import CommandState, ProposalStateError
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


def approved_plan(ctx: CommandContext, proposal_id: str) -> Any:
    """The router plan of an *approved* AI routing proposal (checks autonomy, state
    and category). Raises ValueError/BridgeError with a user-facing message."""
    from pcbrouter.ai.command_schema import OperationCategory
    from pcbrouter.ai.route_bridge import AutonomyMode, plan_from_command

    session = _session(ctx)
    if session is None or ctx.project.working is None:
        raise ValueError("No AI session / board is open.")
    if session.config.autonomy == AutonomyMode.ADVISORY.value:
        raise ValueError("AI autonomy is set to Advisory: routing commands do not run.")
    p = session.proposals.get(proposal_id)
    if p is None:
        raise ValueError(f"Unknown proposal {proposal_id}.")
    if p.state is not CommandState.APPROVED:
        raise ValueError(f"Proposal is {p.state.value}; only approved commands run.")
    if p.category is not OperationCategory.ROUTING:
        raise ValueError("Only routing operations run through the router.")
    return plan_from_command(p.current)


@dataclass
class ExecuteAIProposalCommand(BaseCommand):
    """Run an *approved* AI routing command through the deterministic router.

    Produces router results only (candidates / a board batch / an optimisation
    report): nothing is committed — the user reviews and accepts separately. Safe
    to run on a worker thread: it does not mutate the AI session (the caller records
    the outcome with :meth:`AISession.record_execution` on the GUI thread)."""

    proposal_id: str
    cancel: threading.Event | None = None
    name: ClassVar[str] = "execute_ai_proposal"

    def execute(self, ctx: CommandContext) -> CommandResult:
        from pcbrouter.ai.route_bridge import BridgeError, execute_plan

        working = ctx.project.working
        try:
            plan = approved_plan(ctx, self.proposal_id)
        except (BridgeError, ValueError) as exc:
            return CommandResult.fail(str(exc))
        assert working is not None
        outcome = execute_plan(plan, working, ctx.compute, self.cancel)
        return CommandResult(True, outcome.summary(), outcome)

    def describe(self) -> str:
        return f"execute AI proposal {self.proposal_id}"
