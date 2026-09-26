"""Session, proposals, history, retries, usage and the runner — using MockAIProvider."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import time
from collections.abc import Iterator
from typing import Any

import pytest

from pcbrouter.ai.anonymizer import AnonymizationOptions, EntityKind
from pcbrouter.ai.command_schema import RoutingConstraints
from pcbrouter.ai.exceptions import (
    AIAuthenticationError,
    AIProviderError,
    AIRateLimitError,
    AIRequestCancelled,
    AIServerError,
    AITimeoutError,
)
from pcbrouter.ai.proposals import CommandState, ProposalStateError
from pcbrouter.ai.requests import AIMode
from pcbrouter.ai.responses import AIResponse, FinishStatus, TokenUsage
from pcbrouter.ai.retry import RetryPolicy, call_with_retries
from pcbrouter.ai.runner import AsyncRunner, execute_request, result_or_error
from pcbrouter.ai.session import AIRuntimeConfig, AISession, InteractionKind, PreparedRequest
from pcbrouter.ai.usage import UsageRecord, UsageTracker
from pcbrouter.domain.board import Board
from pcbrouter.history import HistoryManager
from tests.support.mock_provider import (
    COMMAND_PAYLOAD,
    FAKE_KEY,
    MockAIProvider,
    Scenario,
)

NO_RETRY = RetryPolicy(max_retries=0)


@pytest.fixture
def runner() -> Iterator[AsyncRunner]:
    r = AsyncRunner()
    yield r
    r.shutdown()


def session_for(board: Board, **cfg: Any) -> AISession:
    return AISession(
        board, session_id="board-1", config=AIRuntimeConfig(**cfg), history=HistoryManager()
    )


def ask(
    session: AISession,
    scenario: Scenario,
    prompt: str = "Route CAN first",
    mode: AIMode = AIMode.COMMAND,
    **mock: Any,
) -> tuple[PreparedRequest, Any]:
    provider = MockAIProvider(scenario=scenario, **mock)
    prepared = session.prepare(prompt, mode, model="mock-model-1", provider_name="Mock")
    try:
        response = asyncio.run(execute_request(provider, prepared.request, NO_RETRY))
    except AIProviderError as exc:
        return prepared, session.record_failure(prepared, exc)
    return prepared, session.accept_response(prepared, response)


# ------------------------------------------------------------------ scenarios (item 47)
def test_successful_analysis(can_board: Board) -> None:
    s = session_for(can_board)
    _, inter = ask(s, Scenario.ANALYSIS, "Analyze this board.", AIMode.ANALYZE)
    assert inter.kind is InteractionKind.ANALYSIS
    assert inter.analysis is not None and inter.analysis.potential_issues
    assert inter.usage == TokenUsage(1200, 180)
    assert len(s.conversation.history_messages()) == 2


def test_successful_command_becomes_validated_proposal(can_board: Board) -> None:
    s = session_for(can_board)
    _, inter = ask(s, Scenario.COMMAND)
    assert inter.kind is InteractionKind.PROPOSALS
    p = s.proposals[inter.proposal_ids[0]]
    assert p.state is CommandState.VALID
    assert p.validation is not None and p.validation.status.value == "valid_with_warnings"
    assert p.original.effective_constraints.min_clearance_mm is None  # null was stripped
    assert p.board_fingerprint == can_board.fingerprint


@pytest.mark.parametrize("scenario", [Scenario.INVALID_JSON, Scenario.INVALID_SCHEMA])
def test_invalid_output_is_rejected_without_proposals(can_board: Board, scenario: Scenario) -> None:
    s = session_for(can_board)
    _, inter = ask(s, scenario)
    assert inter.kind is InteractionKind.ERROR and not inter.proposal_ids and not s.proposals
    assert s.conversation.turns == []  # a failed exchange does not pollute the conversation


def test_unknown_net_proposal_is_invalid_and_not_approvable(can_board: Board) -> None:
    s = session_for(can_board)
    _, inter = ask(s, Scenario.UNKNOWN_NET)
    p = s.proposals[inter.proposal_ids[0]]
    assert p.state is CommandState.INVALID
    with pytest.raises(ProposalStateError):
        s.approve(p.proposal_id)
    s.reject(p.proposal_id)
    assert p.state is CommandState.REJECTED


@pytest.mark.parametrize(
    ("scenario", "error"),
    [
        (Scenario.TIMEOUT, AITimeoutError),
        (Scenario.RATE_LIMIT, AIRateLimitError),
        (Scenario.AUTH_FAILURE, AIAuthenticationError),
    ],
)
def test_provider_failures(can_board: Board, scenario: Scenario, error: type[Exception]) -> None:
    s = session_for(can_board)
    provider = MockAIProvider(scenario=scenario)
    prepared = s.prepare("x", AIMode.ANALYZE, model="m", provider_name="Mock")
    with pytest.raises(error) as info:
        asyncio.run(
            execute_request(provider, prepared.request, RetryPolicy(max_retries=2, base_delay_s=0))
        )
    assert "sk-live-abcdef123456" not in str(info.value)
    inter = s.record_failure(prepared, info.value)  # type: ignore[arg-type]
    assert inter.kind is InteractionKind.ERROR and s.conversation.turns == []
    expected_calls = 3 if scenario is Scenario.TIMEOUT else 1  # only timeouts are retried
    assert provider.calls == expected_calls


def test_cancelled_request_via_runner(can_board: Board, runner: AsyncRunner) -> None:
    s = session_for(can_board)
    provider = MockAIProvider(scenario=Scenario.HANG)
    prepared = s.prepare("x", AIMode.ANALYZE, model="m", provider_name="Mock")
    future = runner.submit(execute_request(provider, prepared.request, NO_RETRY))
    deadline = time.time() + 2
    while not provider.requests and time.time() < deadline:
        time.sleep(0.01)
    start = time.perf_counter()
    assert future.cancel()
    with pytest.raises(AIRequestCancelled):
        result_or_error(future)
    assert time.perf_counter() - start < 1.0
    inter = s.record_failure(prepared, AIRequestCancelled("x"))
    assert inter.kind is InteractionKind.CANCELLED and s.conversation.turns == []


def test_runner_returns_real_results(can_board: Board, runner: AsyncRunner) -> None:
    s = session_for(can_board)
    prepared = s.prepare("x", AIMode.ANALYZE, model="m", provider_name="Mock")
    fut: concurrent.futures.Future[AIResponse] = runner.submit(
        execute_request(MockAIProvider(), prepared.request, NO_RETRY)
    )
    assert result_or_error(fut).request_id == prepared.request.request_id


# ------------------------------------------------------------------ approval workflow
def test_approve_routing_waits_for_run_and_is_undoable(can_board: Board) -> None:
    s = session_for(can_board)
    _, inter = ask(s, Scenario.COMMAND)
    pid = inter.proposal_ids[0]
    p = s.approve(pid)
    # Stage 7: approved routing commands run only when the user presses Run.
    assert p.state is CommandState.APPROVED and "deterministic router" in p.status_note
    assert s.history is not None
    entry = s.history.entries()[-1]
    assert entry.kind == "DecisionAction" and entry.metadata["decision"] == "approved"
    assert entry.metadata["prompt"] == "Route CAN first"
    assert entry.metadata["context_fingerprint"] == can_board.fingerprint
    assert FAKE_KEY not in json.dumps(dict(entry.metadata), default=str)
    assert any("APPROVED_BY_USER" in line for line in s.session_state_lines())
    s.history.undo()
    assert p.state is CommandState.VALID
    s.history.redo()
    assert p.state is CommandState.APPROVED
    with pytest.raises(ProposalStateError):
        s.reject(pid)  # decided proposals cannot be re-decided (undo first)


def test_read_only_operations_complete_on_approval(can_board: Board) -> None:
    payload = {
        **COMMAND_PAYLOAD,
        "commands": [{"operation": "analyze_net", "targets": [{"type": "net", "name": "CAN_H"}]}],
    }
    s = session_for(can_board)
    _, inter = ask(s, Scenario.CUSTOM, payload=payload)
    p = s.approve(inter.proposal_ids[0])
    assert p.state is CommandState.EXECUTED and "not changed" in p.status_note


def test_lock_approval_updates_session_locks(can_board: Board) -> None:
    payload = {
        **COMMAND_PAYLOAD,
        "commands": [{"operation": "lock_net", "targets": [{"type": "net", "name": "VBAT"}]}],
    }
    s = session_for(can_board)
    _, inter = ask(s, Scenario.CUSTOM, payload=payload)
    s.approve(inter.proposal_ids[0])
    assert s.locks.nets == {"VBAT"}
    route_vbat = {
        **COMMAND_PAYLOAD,
        "commands": [{"operation": "route_net", "targets": [{"type": "net", "name": "VBAT"}]}],
    }
    _, inter2 = ask(s, Scenario.CUSTOM, payload=route_vbat)
    assert s.proposals[inter2.proposal_ids[0]].state is CommandState.INVALID  # locked
    assert s.history is not None
    s.history.undo()  # undo is the most recent decision -> none were made for inter2
    assert s.locks.nets == set()


def test_edit_constraints_keeps_original_and_revalidates(can_board: Board) -> None:
    s = session_for(can_board)
    _, inter = ask(s, Scenario.COMMAND)
    pid = inter.proposal_ids[0]
    original = s.proposals[pid].original
    edited = original.effective_constraints.model_copy(update={"max_vias": 2})
    p = s.edit(pid, edited)
    assert p.user_modified and p.original == original
    assert p.current.effective_constraints.max_vias == 2
    assert any("max_vias: 4 → 2" in c for c in p.user_changes)
    assert p.state is CommandState.VALID
    bad = edited.model_copy(update={"preferred_layers": ["Edge.Cuts"]})
    s.edit(pid, bad)
    assert p.state is CommandState.INVALID
    with pytest.raises(ProposalStateError):
        s.approve(pid)
    s.edit(pid, RoutingConstraints(max_vias=1))
    assert p.state is CommandState.VALID
    s.approve(pid)
    assert s.history is not None
    meta = s.history.entries()[-1].metadata
    assert meta["user_modified"] is True and meta["ai_proposal"] != meta["final_command"]


# ------------------------------------------------------------------ staleness (item 30/62)
def test_response_for_a_closed_board_is_stale(can_board: Board) -> None:
    s = session_for(can_board)
    provider = MockAIProvider(scenario=Scenario.COMMAND)
    prepared = s.prepare("x", AIMode.COMMAND, model="m", provider_name="Mock")
    s.close("user opened another board")  # board switched while waiting
    response = asyncio.run(execute_request(provider, prepared.request, NO_RETRY))
    inter = s.accept_response(prepared, response)
    assert inter.kind is InteractionKind.STALE
    p = s.proposals[inter.proposal_ids[0]]
    assert p.stale and p.state is CommandState.EXPIRED
    with pytest.raises(ProposalStateError):
        s.approve(p.proposal_id)


def test_mismatched_request_id_is_discarded(can_board: Board) -> None:
    s = session_for(can_board)
    prepared = s.prepare("x", AIMode.ANALYZE, model="m", provider_name="Mock")
    bogus = AIResponse("req-other", "mock", "m", "{}", None, 0.1, FinishStatus.COMPLETE)
    inter = s.accept_response(prepared, bogus)
    assert inter.kind is InteractionKind.ERROR and not s.proposals


def test_expire_all_on_close(can_board: Board) -> None:
    s = session_for(can_board)
    _, inter = ask(s, Scenario.COMMAND)
    assert s.expire_all("board closed") == 1
    assert s.proposals[inter.proposal_ids[0]].state is CommandState.EXPIRED


def test_refusal_and_truncation(can_board: Board) -> None:
    s = session_for(can_board)
    prepared = s.prepare("x", AIMode.ANALYZE, model="m", provider_name="Mock")
    refused = AIResponse(
        prepared.request.request_id, "mock", "m", "I can't help", None, 0.1, FinishStatus.REFUSED
    )
    assert "declined" in s.accept_response(prepared, refused).message
    prepared2 = s.prepare("y", AIMode.ANALYZE, model="m", provider_name="Mock")
    cut = AIResponse(
        prepared2.request.request_id,
        "mock",
        "m",
        '{"schema_version": 2',
        None,
        0.1,
        FinishStatus.TRUNCATED,
    )
    assert "cut off" in s.accept_response(prepared2, cut).message


# ------------------------------------------------------------------ anonymised round trip
def test_anonymised_round_trip(can_board: Board) -> None:
    s = session_for(can_board, anonymization=AnonymizationOptions(net_names=True, references=True))
    t_h = s.anonymizer.out(EntityKind.NET, "CAN_H")
    t_l = s.anonymizer.out(EntityKind.NET, "CAN_L")
    payload = {
        **COMMAND_PAYLOAD,
        "message": f"Route {t_h} and {t_l}.",
        "commands": [
            {"operation": "route_group", "targets": [{"type": "net_group", "names": [t_h, t_l]}]}
        ],
    }
    provider = MockAIProvider(scenario=Scenario.CUSTOM, payload=payload)
    prepared = s.prepare("Route CAN_H and CAN_L", AIMode.COMMAND, model="m", provider_name="M")
    sent = prepared.request.messages[-1].content
    assert "CAN_H" not in sent and t_h in sent  # the model only sees tokens
    inter = s.accept_response(
        prepared, asyncio.run(execute_request(provider, prepared.request, NO_RETRY))
    )
    p = s.proposals[inter.proposal_ids[0]]
    assert p.current.targets[0].names == ["CAN_H", "CAN_L"]  # type: ignore[union-attr]
    assert inter.message == "Route CAN_H and CAN_L."  # shown to the user with real names


# ------------------------------------------------------------------ export (item 44)
def test_export_contains_session_but_no_secrets(can_board: Board) -> None:
    s = session_for(can_board)
    ask(s, Scenario.ANALYSIS, "Analyze this board.", AIMode.ANALYZE)
    _, inter = ask(s, Scenario.COMMAND)
    s.approve(inter.proposal_ids[0])
    data = s.export("can_node.kicad_pcb")
    text = json.dumps(data)
    assert data["board"]["fingerprint"] == can_board.fingerprint
    assert len(data["interactions"]) == 2 and data["proposals"][0]["state"] == "approved"
    assert data["proposals"][0]["validation"]["status"] == "valid_with_warnings"
    for forbidden in (FAKE_KEY, "sk-", "authorization", "x-api-key", "Bearer"):
        assert forbidden not in text
    assert "<pcb_context>" not in text  # full context is not exported, only its fingerprint


# ------------------------------------------------------------------ retry & usage
def test_retry_policy_rules() -> None:
    p = RetryPolicy(max_retries=3, base_delay_s=1, multiplier=2, max_delay_s=5, jitter_fraction=0)
    assert [p.delay_for(i, AITimeoutError()) for i in range(4)] == [1, 2, 4, None]
    assert p.delay_for(0, AIServerError()) == 1
    assert p.delay_for(0, AIAuthenticationError()) is None
    assert p.delay_for(0, AIRateLimitError(retry_after_s=3)) == 3
    assert p.delay_for(0, AIRateLimitError(retry_after_s=None)) is None
    assert p.delay_for(0, AIRateLimitError(retry_after_s=600)) is None
    with pytest.raises(ValueError):
        RetryPolicy(max_retries=100)


def test_call_with_retries_is_bounded() -> None:
    calls = 0
    sleeps: list[float] = []

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise AITimeoutError("t")
        return "ok"

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    policy = RetryPolicy(max_retries=2, base_delay_s=0.5, jitter_fraction=0)
    assert asyncio.run(call_with_retries(flaky, policy, sleep=fake_sleep)) == "ok"
    assert sleeps == [0.5, 1.0]

    async def always() -> str:
        raise AITimeoutError("t")

    with pytest.raises(AITimeoutError):
        asyncio.run(call_with_retries(always, policy, sleep=fake_sleep))

    async def cancelled() -> str:
        raise AIRequestCancelled("c")

    with pytest.raises(AIRequestCancelled):
        asyncio.run(call_with_retries(cancelled, policy, sleep=fake_sleep))


def test_usage_tracker_never_invents_cost() -> None:
    t = UsageTracker()
    t.add(UsageRecord("r1", "openai", "OpenAI - Main", "m", 100, 20, 1.0, True))
    t.add(UsageRecord("r2", "anthropic", "Claude - Main", "c", None, None, 1.0, True))
    t.add(UsageRecord("r3", "openai", "OpenAI - Main", "m", None, None, None, False))
    s = t.summary()
    assert (s.requests, s.failed_requests, s.input_tokens, s.output_tokens) == (3, 1, 100, 20)
    assert s.requests_without_usage == 1
    assert s.estimated_cost.startswith("Unavailable")


def test_prompts_and_context_are_not_logged_by_default(
    can_board: Board, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    caplog.set_level(logging.DEBUG, logger="pcbrouter")
    s = session_for(can_board)
    ask(s, Scenario.COMMAND, "Route CAN first please")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "Route CAN first please" not in logged and "<pcb_context>" not in logged
    assert "TCAN1042" not in logged  # board content stays out of normal logs
    assert "ai.proposal" in logged  # but the event itself is recorded
    caplog.clear()
    opted_in = session_for(can_board, log_prompts=True)  # explicit user permission
    opted_in.prepare("Route CAN first please", AIMode.COMMAND, model="m", provider_name="M")
    assert any("ai.prompt.debug" in r.getMessage() for r in caplog.records)
