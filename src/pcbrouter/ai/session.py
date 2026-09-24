"""Per-board AI engineering session (no Qt).

Owns the conversation, proposals, interaction log, session locks and anonymiser for
one loaded board. Every response is checked against the session id and board
fingerprint it was requested for: a response that arrives after the board changed
is recorded as STALE and its proposals are EXPIRED, never approvable.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pcbrouter import __version__
from pcbrouter.ai.anonymizer import AnonymizationOptions, Anonymizer, EntityKind
from pcbrouter.ai.command_parser import parse_planner_response
from pcbrouter.ai.command_schema import (
    AIAnalysis,
    AICommand,
    ComponentTarget,
    NetGroupTarget,
    NetTarget,
    Operation,
    OperationCategory,
    RoutingConstraints,
)
from pcbrouter.ai.command_validator import SemanticValidator, SessionLocks, ValidationStatus
from pcbrouter.ai.context_builder import (
    BoardContext,
    BoardContextBuilder,
    ContextLevel,
    ContextLimits,
)
from pcbrouter.ai.conversation import Conversation, ConversationTurn
from pcbrouter.ai.exceptions import AIProviderError, AIRequestCancelled
from pcbrouter.ai.prompt_builder import PromptBuilder, PromptInputs
from pcbrouter.ai.proposals import CommandProposal, CommandState, ProposalStateError
from pcbrouter.ai.requests import AIMode, AIRequest
from pcbrouter.ai.responses import AIResponse, FinishStatus, TokenUsage
from pcbrouter.domain.board import Board
from pcbrouter.history.history import HistoryManager, UndoableAction

log = logging.getLogger(__name__)
EXPORT_FORMAT_VERSION = 1


@dataclass(frozen=True, slots=True)
class AIRuntimeConfig:
    context_level: ContextLevel = ContextLevel.STANDARD
    limits: ContextLimits = field(default_factory=ContextLimits)
    max_conversation_turns: int = 6
    timeout_s: float = 90.0
    max_retries: int = 2
    max_output_tokens: int = 8192
    anonymization: AnonymizationOptions = field(default_factory=AnonymizationOptions)
    log_prompts: bool = False


class InteractionKind(StrEnum):
    ANALYSIS = "analysis"
    PROPOSALS = "proposals"
    CLARIFICATION = "clarification"
    UNSUPPORTED = "unsupported"
    ERROR = "error"
    CANCELLED = "cancelled"
    STALE = "stale"


@dataclass
class Interaction:
    request_id: str
    mode: AIMode
    prompt: str
    provider: str
    model: str
    context_level: str
    context_chars: int
    context_tokens: int
    context_fingerprint: str
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    kind: InteractionKind | None = None
    message: str = ""
    analysis: AIAnalysis | None = None
    plan_steps: list[str] = field(default_factory=list)
    proposal_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error_detail: str | None = None
    usage: TokenUsage | None = None
    latency_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "mode": self.mode.value,
            "prompt": self.prompt,
            "provider": self.provider,
            "model": self.model,
            "context": {
                "level": self.context_level,
                "characters": self.context_chars,
                "estimated_tokens": self.context_tokens,
                "board_fingerprint": self.context_fingerprint,
            },
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.kind.value if self.kind else None,
            "message": self.message,
            "analysis": (
                self.analysis.model_dump(mode="json", exclude_none=True) if self.analysis else None
            ),
            "plan_steps": self.plan_steps,
            "proposal_ids": self.proposal_ids,
            "notes": self.notes,
            "usage": (
                None
                if self.usage is None
                else {
                    "input_tokens": self.usage.input_tokens,
                    "output_tokens": self.usage.output_tokens,
                }
            ),
            "latency_s": self.latency_s,
        }


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    request: AIRequest
    context: BoardContext
    display_prompt: str  # what the user typed
    sent_prompt: str  # what the model receives (anonymised if enabled)
    provider_name: str
    session_id: str

    @property
    def fingerprint(self) -> str:
        return self.context.board_fingerprint


def _map_strings(value: Any, fn: Any) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        return {k: _map_strings(v, fn) for k, v in value.items()}
    if isinstance(value, list):
        return [_map_strings(v, fn) for v in value]
    return value


class AISession:
    def __init__(
        self,
        board: Board,
        *,
        session_id: str,
        config: AIRuntimeConfig | None = None,
        history: HistoryManager | None = None,
        board_revision: int = 0,
    ) -> None:
        self.board = board
        self.session_id = session_id
        self.config = config or AIRuntimeConfig()
        self.history = history
        self.board_revision = board_revision
        self.anonymizer = Anonymizer(board, self.config.anonymization)
        self.conversation = Conversation(self.config.max_conversation_turns)
        self.proposals: dict[str, CommandProposal] = {}
        self.interactions: list[Interaction] = []
        self.locks = SessionLocks()
        self.privacy_acknowledged = False
        self.closed = False
        self.created_at = time.time()

    @property
    def fingerprint(self) -> str:
        return self.board.fingerprint

    def validator(self) -> SemanticValidator:
        return SemanticValidator(self.board, self.anonymizer, self.locks)

    def interaction(self, request_id: str) -> Interaction | None:
        return next((i for i in self.interactions if i.request_id == request_id), None)

    # ------------------------------------------------------------------ context
    def build_context(
        self,
        prompt: str = "",
        selected_nets: tuple[str, ...] = (),
        selected_components: tuple[str, ...] = (),
        level: ContextLevel | None = None,
    ) -> BoardContext:
        return BoardContextBuilder(
            self.board, session_id=self.session_id, board_revision=self.board_revision
        ).build(
            level or self.config.context_level,
            self.config.limits,
            user_prompt=prompt,
            selected_nets=selected_nets,
            selected_components=selected_components,
            anonymizer=self.anonymizer,
        )

    def session_state_lines(self) -> list[str]:
        lines = []
        for p in self.proposals.values():
            if (
                p.state in (CommandState.APPROVED, CommandState.EXECUTED)
                and p.category is not OperationCategory.READ_ONLY
            ):
                cmd = self._anonymise_command(p.current)
                lines.append(f"APPROVED_BY_USER: {json.dumps(cmd, ensure_ascii=False)}")
        if self.locks.components:
            lines.append(
                "SESSION_LOCKED_COMPONENTS: "
                + json.dumps(
                    sorted(
                        self.anonymizer.out(EntityKind.REFERENCE, r) for r in self.locks.components
                    )
                )
            )
        if self.locks.nets:
            lines.append(
                "SESSION_LOCKED_NETS: "
                + json.dumps(
                    sorted(self.anonymizer.out(EntityKind.NET, n) for n in self.locks.nets)
                )
            )
        return lines

    def _anonymise_command(self, cmd: AICommand) -> dict[str, Any]:
        data = cmd.model_dump(mode="json", exclude_none=True)
        out = self.anonymizer.out
        for t in data.get("targets", []):
            if t["type"] == "net":
                t["name"] = out(EntityKind.NET, t["name"])
            elif t["type"] == "net_group":
                t["names"] = [out(EntityKind.NET, n) for n in t["names"]]
            elif t["type"] == "component":
                t["reference"] = out(EntityKind.REFERENCE, t["reference"])
        cons = data.get("constraints", {})
        if "avoid_nets" in cons:
            cons["avoid_nets"] = [out(EntityKind.NET, n) for n in cons["avoid_nets"]]
        return data

    # ------------------------------------------------------------------ requests
    def prepare(
        self,
        prompt: str,
        mode: AIMode,
        *,
        model: str,
        provider_name: str,
        selected_nets: tuple[str, ...] = (),
        selected_components: tuple[str, ...] = (),
    ) -> PreparedRequest:
        if self.closed:
            raise ProposalStateError("the AI session for this board has been closed")
        context = self.build_context(prompt, selected_nets, selected_components)
        sent_prompt = self.anonymizer.anonymize_text(prompt.strip())
        state = [*self.session_state_lines()]
        if selected_nets or selected_components:
            state.append(
                "USER_SELECTION: "
                + json.dumps(
                    [self.anonymizer.out(EntityKind.NET, n) for n in selected_nets]
                    + [self.anonymizer.out(EntityKind.REFERENCE, r) for r in selected_components]
                )
            )
        request = PromptBuilder().build(
            PromptInputs(
                mode=mode,
                user_prompt=sent_prompt,
                context=context,
                model=model,
                session_state_lines=tuple(state),
                timeout_s=self.config.timeout_s,
                max_output_tokens=self.config.max_output_tokens,
            ),
            self.conversation,
        )
        if self.config.log_prompts:  # explicit opt-in only (Settings ▸ AI ▸ debug)
            log.debug(
                "ai.prompt.debug request_id=%s system=%r messages=%r",
                request.request_id,
                request.system_prompt,
                request.messages,
            )
        self.interactions.append(
            Interaction(
                request_id=request.request_id,
                mode=mode,
                prompt=prompt.strip(),
                provider=provider_name,
                model=model,
                context_level=context.level.value,
                context_chars=context.char_count,
                context_tokens=context.token_estimate,
                context_fingerprint=context.board_fingerprint,
            )
        )
        return PreparedRequest(
            request, context, prompt.strip(), sent_prompt, provider_name, self.session_id
        )

    def _is_current(self, prepared: PreparedRequest) -> bool:
        return (
            not self.closed
            and prepared.session_id == self.session_id
            and prepared.fingerprint == self.fingerprint
        )

    def accept_response(self, prepared: PreparedRequest, response: AIResponse) -> Interaction:
        inter = self.interaction(prepared.request.request_id)
        if inter is None:
            raise ProposalStateError("unknown request id")
        inter.finished_at = time.time()
        inter.usage, inter.latency_s = response.usage, response.latency_s
        if response.request_id != prepared.request.request_id:
            inter.kind = InteractionKind.ERROR
            inter.message = "The response did not belong to this request and was discarded."
            return inter
        display = self.anonymizer.deanonymize_text
        if response.finish_status is FinishStatus.REFUSED:
            inter.kind = InteractionKind.ERROR
            inter.message = "The model declined the request: " + display(response.content[:500])
            return inter
        try:
            parsed = parse_planner_response(response.content)
        except AIProviderError as exc:
            inter.kind = InteractionKind.ERROR
            inter.message = exc.user_message
            if response.finish_status is FinishStatus.TRUNCATED:
                inter.message += " The response was cut off at the output-token limit."
            inter.error_detail = str(exc)
            inter.notes.extend(getattr(exc, "errors", [])[:20])
            log.warning("ai.response.invalid request_id=%s error=%s", response.request_id, exc)
            return inter
        pr = parsed.response
        inter.notes.extend(parsed.notes)
        if not response.used_native_schema:
            inter.notes.append("Schema was not enforced by the provider; validated locally.")
        if pr.mode != prepared.request.mode.value:
            inter.notes.append(
                f"Model answered in mode '{pr.mode}' (asked for "
                f"'{prepared.request.mode.value}')."
            )
        inter.message = display(pr.message)
        if pr.analysis is not None:
            inter.analysis = AIAnalysis.model_validate(
                _map_strings(pr.analysis.model_dump(mode="json", exclude_none=True), display)
            )
        inter.plan_steps = [display(s) for s in pr.plan_steps or []]

        stale = not self._is_current(prepared)
        for command in pr.commands or []:
            proposal = CommandProposal(
                request_id=prepared.request.request_id,
                session_id=prepared.session_id,
                board_fingerprint=prepared.fingerprint,
                provider=prepared.provider_name,
                model=response.model,
                prompt=prepared.display_prompt,
                mode=prepared.request.mode.value,
                original=command,
            )
            if stale:
                proposal.stale = True
                proposal.transition(CommandState.EXPIRED, "stale response: board changed")
            else:
                proposal.transition(CommandState.VALIDATING, "local validation")
                report = self.validator().validate(command)
                if report.resolved is not None:
                    proposal.original = report.resolved
                proposal.apply_validation(report)
            self.proposals[proposal.proposal_id] = proposal
            inter.proposal_ids.append(proposal.proposal_id)
            log.info(
                "ai.proposal id=%s op=%s status=%s",
                proposal.proposal_id,
                command.operation.value,
                proposal.state.value,
            )

        if stale:
            inter.kind = InteractionKind.STALE
            inter.notes.append(
                "Stale response: the board changed while waiting. Nothing from "
                "it can be approved."
            )
            return inter
        if pr.clarification_needed:
            inter.kind = InteractionKind.CLARIFICATION
            inter.notes.append("Clarification needed: " + display(pr.clarification_needed))
        elif pr.unsupported_request:
            inter.kind = InteractionKind.UNSUPPORTED
            inter.notes.append("Unsupported: " + display(pr.unsupported_request))
        elif inter.proposal_ids:
            inter.kind = InteractionKind.PROPOSALS
        else:
            inter.kind = InteractionKind.ANALYSIS
        self.conversation.add(
            ConversationTurn(
                "user",
                prepared.display_prompt,
                prepared.request.mode,
                response.request_id,
                sent_text=prepared.sent_prompt,
            )
        )
        self.conversation.add(
            ConversationTurn(
                "assistant",
                inter.message,
                prepared.request.mode,
                response.request_id,
                sent_text=pr.message,
            )
        )
        return inter

    def record_failure(self, prepared: PreparedRequest, error: AIProviderError) -> Interaction:
        """Failures and cancellations leave the conversation untouched."""
        inter = self.interaction(prepared.request.request_id)
        if inter is None:
            raise ProposalStateError("unknown request id")
        inter.finished_at = time.time()
        cancelled = isinstance(error, AIRequestCancelled)
        inter.kind = InteractionKind.CANCELLED if cancelled else InteractionKind.ERROR
        inter.message = error.user_message
        inter.error_detail = None if cancelled else str(error)
        self.conversation.remove_request(prepared.request.request_id)
        return inter

    # ------------------------------------------------------------------ decisions
    def _proposal(self, proposal_id: str) -> CommandProposal:
        try:
            return self.proposals[proposal_id]
        except KeyError as exc:
            raise ProposalStateError(f"unknown proposal {proposal_id}") from exc

    def _check_approvable(self, p: CommandProposal) -> None:
        if p.stale or p.session_id != self.session_id or p.board_fingerprint != self.fingerprint:
            raise ProposalStateError("this proposal is stale (made for a different board state)")
        if p.state is CommandState.INVALID:
            raise ProposalStateError("an invalid proposal cannot be approved; edit or reject it")
        if p.state is not CommandState.VALID:
            raise ProposalStateError(f"proposal is {p.state.value}, not valid")

    def _run(self, action: UndoableAction) -> None:
        if self.history is not None:
            self.history.push(action)
        else:
            action.apply()

    def approve(self, proposal_id: str) -> CommandProposal:
        p = self._proposal(proposal_id)
        self._check_approvable(p)
        self._run(DecisionAction(self, p, approve=True))
        return p

    def reject(self, proposal_id: str) -> CommandProposal:
        p = self._proposal(proposal_id)
        if p.state not in (CommandState.VALID, CommandState.INVALID):
            raise ProposalStateError(
                f"proposal is {p.state.value}; only open proposals can be " "rejected"
            )
        self._run(DecisionAction(self, p, approve=False))
        return p

    def edit(self, proposal_id: str, constraints: RoutingConstraints) -> CommandProposal:
        p = self._proposal(proposal_id)
        if p.stale:
            raise ProposalStateError("stale proposals cannot be edited")
        p.edit_constraints(constraints)
        if p.state is CommandState.VALIDATING:
            p.apply_validation(self.validator().validate(p.current))
        return p

    def expire_all(self, reason: str) -> int:
        count = 0
        for p in self.proposals.values():
            if p.is_open:
                p.stale = True
                p.transition(CommandState.EXPIRED, reason)
                count += 1
        return count

    def close(self, reason: str = "board closed") -> None:
        self.expire_all(reason)
        self.closed = True

    # ------------------------------------------------------------------ export
    def export(self, board_name: str | None = None) -> dict[str, Any]:
        """JSON-safe record of the session. Contains no credentials or headers."""
        return {
            "format": "ai-pcb-router/ai-session",
            "format_version": EXPORT_FORMAT_VERSION,
            "app_version": __version__,
            "exported_at": time.time(),
            "board": {
                "name": board_name or self.anonymizer.board_name(),
                "fingerprint": self.fingerprint,
            },
            "session_id": self.session_id,
            "anonymization": self.config.anonymization.enabled_labels(),
            "interactions": [i.to_dict() for i in self.interactions],
            "proposals": [p.to_dict() for p in self.proposals.values()],
        }


class DecisionAction(UndoableAction):
    """Approve/reject as an undoable history action (metadata only; no geometry)."""

    def __init__(self, session: AISession, proposal: CommandProposal, *, approve: bool) -> None:
        self._session = session
        self._p = proposal
        self._approve = approve
        self._previous = proposal.state
        self._added: dict[str, set[str]] = {"components": set(), "nets": set(), "track_nets": set()}

    @property
    def label(self) -> str:
        verb = "Approve" if self._approve else "Reject"
        return f"AI {verb}: {self._p.current.operation.label}"

    @property
    def metadata(self) -> dict[str, Any]:
        v = self._p.validation
        return {
            "timestamp": time.time(),
            "decision": "approved" if self._approve else "rejected",
            "provider": self._p.provider,
            "model": self._p.model,
            "prompt": self._p.prompt,
            "context_fingerprint": self._p.board_fingerprint,
            "proposal_id": self._p.proposal_id,
            "operation": self._p.current.operation.value,
            "ai_proposal": self._p.original.model_dump(mode="json", exclude_none=True),
            "final_command": self._p.current.model_dump(mode="json", exclude_none=True),
            "user_modified": self._p.user_modified,
            "user_changes": list(self._p.user_changes),
            "validation": v.status.value if v else None,
        }

    def apply(self) -> None:
        p = self._p
        if not self._approve:
            p.transition(CommandState.REJECTED, "rejected by user")
            return
        p.transition(CommandState.APPROVED, "approved by user")
        if p.category is OperationCategory.READ_ONLY:
            p.transition(CommandState.EXECUTED, "read-only operation completed")
        self._apply_locks()

    def revert(self) -> None:
        p = self._p
        back = (
            CommandState.INVALID
            if (p.validation and p.validation.status is ValidationStatus.INVALID)
            else CommandState.VALID
        )
        p.transition(back, "undo")
        locks = self._session.locks
        locks.components -= self._added["components"]
        locks.nets -= self._added["nets"]
        locks.track_nets -= self._added["track_nets"]

    def _apply_locks(self) -> None:
        cmd = self._p.current
        locks = self._session.locks
        nets: list[str] = []
        for t in cmd.targets:
            if isinstance(t, NetTarget):
                nets.append(t.name)
            elif isinstance(t, NetGroupTarget):
                nets.extend(t.names)
        if cmd.operation is Operation.LOCK_COMPONENT:
            refs = {t.reference for t in cmd.targets if isinstance(t, ComponentTarget)}
            self._added["components"] = refs - locks.components
            locks.components |= refs
        elif cmd.operation is Operation.LOCK_NET:
            self._added["nets"] = set(nets) - locks.nets
            locks.nets |= set(nets)
        elif cmd.operation is Operation.LOCK_TRACK:
            self._added["track_nets"] = set(nets) - locks.track_nets
            locks.track_nets |= set(nets)
