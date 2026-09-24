"""Command proposals and their approval workflow.

States::

    PROPOSED ─▶ VALIDATING ─▶ VALID ─────▶ APPROVED ─▶ EXECUTED (read-only ops only)
                    │           │   ╲──▶ REJECTED
                    ▼           ▼
                 INVALID ──▶ REJECTED        any open state ─▶ EXPIRED (board changed)

* An INVALID proposal can never be approved (validation always wins over AI
  confidence). Editing re-validates.
* Routing operations stop at APPROVED ("waiting for routing engine support"):
  Stage 2 never executes them. Read-only operations (analysis/explanation) are
  EXECUTED on approval because they change nothing.
* Undo (via :class:`~pcbrouter.history.HistoryManager`) may move APPROVED/EXECUTED/
  REJECTED back to their validated state.
* ``original`` is exactly what the AI proposed (after identifier resolution);
  ``final`` is the user-edited command. Both are kept for traceability.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pcbrouter.ai.command_schema import AICommand, OperationCategory, RoutingConstraints
from pcbrouter.ai.command_validator import ValidationReport, ValidationStatus


class CommandState(StrEnum):
    PROPOSED = "proposed"
    VALIDATING = "validating"
    VALID = "valid"
    INVALID = "invalid"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"


_S = CommandState
TRANSITIONS: dict[CommandState, frozenset[CommandState]] = {
    _S.PROPOSED: frozenset({_S.VALIDATING, _S.EXPIRED}),
    _S.VALIDATING: frozenset({_S.VALID, _S.INVALID, _S.EXPIRED}),
    _S.VALID: frozenset({_S.APPROVED, _S.REJECTED, _S.VALIDATING, _S.EXPIRED}),
    _S.INVALID: frozenset({_S.REJECTED, _S.VALIDATING, _S.EXPIRED}),
    _S.APPROVED: frozenset({_S.EXECUTED, _S.VALID, _S.EXPIRED}),  # VALID = undo
    _S.REJECTED: frozenset({_S.VALID, _S.INVALID}),  # undo
    _S.EXECUTED: frozenset({_S.VALID}),  # undo (read-only operations only)
    _S.EXPIRED: frozenset(),
}
OPEN_STATES = frozenset({_S.PROPOSED, _S.VALIDATING, _S.VALID, _S.INVALID})


class ProposalStateError(RuntimeError):
    """An illegal proposal state transition was requested."""


def new_proposal_id() -> str:
    return f"prop-{uuid.uuid4().hex[:10]}"


@dataclass
class CommandProposal:
    request_id: str
    session_id: str
    board_fingerprint: str
    provider: str
    model: str
    prompt: str
    mode: str
    original: AICommand
    proposal_id: str = field(default_factory=new_proposal_id)
    created_at: float = field(default_factory=time.time)
    final: AICommand | None = None
    validation: ValidationReport | None = None
    state: CommandState = CommandState.PROPOSED
    stale: bool = False
    state_log: list[tuple[str, float, str]] = field(default_factory=list)
    user_changes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.state_log.append((self.state.value, self.created_at, "proposed by AI"))

    @property
    def current(self) -> AICommand:
        return self.final or self.original

    @property
    def user_modified(self) -> bool:
        return self.final is not None

    @property
    def category(self) -> OperationCategory:
        return self.current.operation.category

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def status_note(self) -> str:
        if self.state is CommandState.APPROVED and self.category is OperationCategory.ROUTING:
            return "Approved. Waiting for routing engine support (Stage 4)."
        if self.state is CommandState.APPROVED:
            return "Approved. Recorded in the session's constraint set (no geometry change)."
        if self.state is CommandState.EXECUTED:
            return "Completed. Read-only operation; the board was not changed."
        if self.state is CommandState.EXPIRED:
            return "Expired: the board changed or was closed; this proposal is stale."
        return self.state.value.replace("_", " ").capitalize()

    def transition(self, new: CommandState, note: str = "") -> None:
        if new not in TRANSITIONS[self.state]:
            raise ProposalStateError(f"cannot move proposal from {self.state.value} to {new.value}")
        self.state = new
        self.state_log.append((new.value, time.time(), note))

    def apply_validation(self, report: ValidationReport) -> None:
        self.validation = report
        target = (
            CommandState.INVALID
            if report.status is ValidationStatus.INVALID
            else CommandState.VALID
        )
        self.transition(target, f"validation: {report.status.label}")

    def edit_constraints(self, constraints: RoutingConstraints) -> None:
        if not self.is_open:
            raise ProposalStateError(f"cannot edit a proposal that is {self.state.value}")
        before = self.current.effective_constraints.specified_fields()
        after = constraints.specified_fields()
        changes = []
        for key in sorted(set(before) | set(after)):
            if before.get(key) != after.get(key):
                changes.append(f"{key}: {before.get(key, 'unset')} → {after.get(key, 'unset')}")
        if not changes:
            return
        self.final = self.current.model_copy(
            update={"constraints": None if constraints.is_empty() else constraints}
        )
        self.user_changes.extend(changes)
        self.transition(CommandState.VALIDATING, "user edited constraints")

    def to_dict(self) -> dict[str, Any]:
        v = self.validation
        return {
            "proposal_id": self.proposal_id,
            "request_id": self.request_id,
            "created_at": self.created_at,
            "provider": self.provider,
            "model": self.model,
            "mode": self.mode,
            "prompt": self.prompt,
            "board_fingerprint": self.board_fingerprint,
            "state": self.state.value,
            "stale": self.stale,
            "original_ai_proposal": self.original.model_dump(mode="json", exclude_none=True),
            "user_modified_final": (
                self.final.model_dump(mode="json", exclude_none=True) if self.final else None
            ),
            "user_changes": list(self.user_changes),
            "validation": (
                None
                if v is None
                else {
                    "status": v.status.value,
                    "issues": [
                        {"severity": i.severity.value, "code": i.code, "message": i.text()}
                        for i in v.issues
                    ],
                }
            ),
            "state_log": [{"state": s, "at": t, "note": n} for s, t, n in self.state_log],
        }
