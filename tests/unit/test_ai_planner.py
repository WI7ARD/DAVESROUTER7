"""Stage 7: AI planner → approval → command bus → deterministic router → user.

All offline (MockAIProvider); no API credits are spent.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import pytest

from pcbrouter.ai.proposals import CommandState
from pcbrouter.ai.requests import AIMode
from pcbrouter.ai.retry import RetryPolicy
from pcbrouter.ai.route_bridge import ExecutionOutcome, plan_from_command, router_facts
from pcbrouter.ai.runner import execute_request
from pcbrouter.ai.service import AIService
from pcbrouter.ai.session import AIRuntimeConfig, AISession, InteractionKind
from pcbrouter.commands import (
    AcceptRouteCommand,
    ApproveProposalCommand,
    CommandBus,
    CommandContext,
    ExecuteAIProposalCommand,
)
from pcbrouter.history import HistoryManager
from pcbrouter.project.manager import ProjectManager
from pcbrouter.routing.optimize import OptimizeGoal
from pcbrouter.routing.result import FailureReason, RouteStatus
from tests.support.mock_provider import MockAIProvider, Scenario

BOARD = Path(__file__).parent.parent / "fixtures" / "boards" / "router_basic.kicad_pcb"


def payload(commands: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "mode": "command",
        "message": "Routing A first.",
        "analysis": None,
        "plan_steps": None,
        "commands": commands,
        "clarification_needed": None,
        "unsupported_request": None,
        **extra,
    }


def route_a(max_vias: int) -> dict[str, Any]:
    return {
        "operation": "route_net",
        "targets": [{"type": "net", "name": "A"}],
        "constraints": {"max_vias": max_vias, "preserve_existing_routes": True},
        "reasoning_summary": "A crosses the F.Cu keepout; route it first.",
        "confidence": "high",
    }


class Env:
    def __init__(self, tmp: Path, autonomy: str = "approval_required") -> None:
        self.project = ProjectManager(workspace_base=tmp)
        self.project.open_board(BOARD)
        self.history = HistoryManager()
        self.ai = AIService()
        wb = self.project.working
        assert wb is not None
        self.ai.start_session(
            wb.board,
            "s1",
            AIRuntimeConfig(autonomy=autonomy),
            history=self.history,
            engine_provider=lambda: self.project.engine,
        )
        self.bus = CommandBus(
            CommandContext(project=self.project, history=self.history, ai=self.ai), read_only=True
        )

    @property
    def session(self) -> AISession:
        assert self.ai.session is not None
        return self.ai.session

    def ask(self, data: dict[str, Any], prompt: str) -> Any:
        provider = MockAIProvider(scenario=Scenario.CUSTOM, payload=data)
        prepared = self.session.prepare(prompt, AIMode.COMMAND, model="mock", provider_name="Mock")
        response = asyncio.run(
            execute_request(provider, prepared.request, RetryPolicy(max_retries=0))
        )
        return self.session.accept_response(prepared, response)


def test_natural_language_to_validated_route(tmp_path: Path) -> None:
    before = hashlib.sha256(BOARD.read_bytes()).hexdigest()
    env = Env(tmp_path)
    inter = env.ask(payload([route_a(2)]), "Route A first and use no more than two vias")
    assert inter.kind is not InteractionKind.ERROR
    (pid,) = [p.proposal_id for p in env.session.proposals.values()]
    p = env.session.proposals[pid]
    assert p.state is CommandState.VALID  # schema + semantic/rule validation passed
    # not approved yet -> cannot run
    assert not env.bus.dispatch(ExecuteAIProposalCommand(pid)).success
    assert env.bus.dispatch(ApproveProposalCommand(pid)).success
    res = env.bus.dispatch(ExecuteAIProposalCommand(pid))
    assert res.success, res.message
    outcome: ExecutionOutcome = res.data
    route = outcome.route_results[0]
    assert route.status is RouteStatus.SUCCESS
    best = route.best
    assert len(best.proposal.vias) <= 2 and best.validation.legal
    env.session.record_execution(pid, router_facts(outcome), outcome.summary())
    assert p.state is CommandState.EXECUTED
    assert any(line.startswith("ROUTER_RESULT") for line in env.session.session_state_lines())
    working = env.project.working
    assert working is not None and not working.modified  # nothing applied yet
    accepted = env.bus.dispatch(AcceptRouteCommand(best))
    assert accepted.success and working.modified
    assert env.session.update_board(working.board) == 0  # follows the working board
    assert hashlib.sha256(BOARD.read_bytes()).hexdigest() == before


def test_failure_feedback_reaches_the_planner(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.ask(payload([route_a(1)]), "Route A with at most one via")
    (pid,) = list(env.session.proposals)
    env.bus.dispatch(ApproveProposalCommand(pid))
    outcome = env.bus.dispatch(ExecuteAIProposalCommand(pid)).data
    route = outcome.route_results[0]
    assert route.status is RouteStatus.NO_ROUTE and route.reason is FailureReason.VIA_LIMIT
    facts = router_facts(outcome)
    env.session.record_execution(pid, facts, outcome.summary())
    lines = "\n".join(env.session.session_state_lines())
    assert "VIA_LIMIT" in lines and "2 via" in lines  # structured facts for the next turn
    # the AI may now suggest 2 vias — as a new proposal the user must approve again
    env.ask(payload([route_a(2)]), "Try again with the router's suggestion")
    newest = [p for p in env.session.proposals.values() if p.proposal_id != pid]
    assert newest and newest[0].state is CommandState.VALID


def test_ai_cannot_inject_geometry(tmp_path: Path) -> None:
    env = Env(tmp_path)
    bad = route_a(2)
    bad["segments"] = [{"start": [0, 0], "end": [10, 10], "layer": "F.Cu"}]
    inter = env.ask(payload([bad]), "Route A")
    assert inter.kind is InteractionKind.ERROR and not env.session.proposals
    raw = payload([route_a(2)], kicad_text="(segment (start 0 0) (end 1 1))")
    assert env.ask(raw, "Route A").kind is InteractionKind.ERROR


def test_advisory_mode_never_routes(tmp_path: Path) -> None:
    env = Env(tmp_path, autonomy="advisory")
    env.ask(payload([route_a(2)]), "Route A")
    (pid,) = list(env.session.proposals)
    env.bus.dispatch(ApproveProposalCommand(pid))
    res = env.bus.dispatch(ExecuteAIProposalCommand(pid))
    assert not res.success and "Advisory" in res.message


def test_fact_requests_are_answered_deterministically(tmp_path: Path) -> None:
    env = Env(tmp_path)
    data = payload(
        None,
        fact_requests=[
            {"tool": "get_rule_info", "target": "A"},  # type: ignore[arg-type]
            {"tool": "get_connectivity", "target": None},
        ],
    )
    env.ask(data, "What limits apply to A?")
    assert len(env.session.pending_fact_requests) == 2
    answers = env.session.answer_fact_requests()
    assert answers[0]["fact"]["preferred_width_mm"] == pytest.approx(0.25)
    assert "incomplete_nets" in answers[1]["fact"]
    assert sum(1 for line in env.session.session_state_lines() if line.startswith("FACT")) == 2


def test_tweak_goal_mapping_is_deterministic() -> None:
    from pcbrouter.ai.command_schema import AICommand

    clean = AICommand.model_validate(
        {"operation": "optimize_net", "targets": [{"type": "net", "name": "A"}]}
    )
    assert plan_from_command(clean).goal is OptimizeGoal.FEWER_BENDS
    fewer = AICommand.model_validate(
        {"operation": "reduce_vias", "targets": [{"type": "net", "name": "A"}]}
    )
    assert plan_from_command(fewer).goal is OptimizeGoal.FEWER_VIAS
