"""Phase 3 prompt evolution: strategies, ratings, deterministic benchmark."""

from __future__ import annotations

from pathlib import Path

import pytest

from pcbrouter.ai.conversation import Conversation
from pcbrouter.ai.evolution import benchmark_all
from pcbrouter.ai.prompt_builder import PromptBuilder, PromptInputs
from pcbrouter.ai.requests import AIMode
from pcbrouter.ai.session import AIRuntimeConfig, AISession
from pcbrouter.ai.strategy import (
    PromptStrategy,
    active_strategy,
    describe_diff,
    resolve_instructions,
    snapshot_effective,
)
from pcbrouter.domain.board import Board
from pcbrouter.kicad.loader import load_board
from pcbrouter.settings.settings import AISettings

BOARDS = Path(__file__).parent.parent / "fixtures" / "boards"

#: Realistic safe override: short, but restates the safety invariants an
#: override must keep (JSON-only output, approval + validation gates).
SAFE_ANALYZE = (
    "Be brief. Reply with exactly one JSON object. "
    "Wait for user approval; commands are validated locally."
)
SAFE_PLAN = (
    "Be brief. Reply with exactly one JSON object. "
    "Wait for user approval; commands are validated locally."
)


def board() -> Board:
    return load_board(BOARDS / "can_node.kicad_pcb").board


def test_strategy_validation() -> None:
    with pytest.raises(ValueError):
        PromptStrategy(mode_instructions={"nope": "text"})
    with pytest.raises(ValueError):
        PromptStrategy(mode_instructions={"analyze": "  "})
    ok = PromptStrategy(name="v1", note="tune", mode_instructions={"analyze": "Be brief."})
    assert ok.parent_id is None


def test_resolve_and_snapshot() -> None:
    base = resolve_instructions(None)
    tuned = PromptStrategy(name="t", mode_instructions={"analyze": "Be brief."})
    resolved = resolve_instructions(tuned)
    assert resolved[AIMode.ANALYZE] == "Be brief."
    assert resolved[AIMode.PLAN] == base[AIMode.PLAN]
    child = snapshot_effective("snap", "note", tuned, resolved)
    assert child.parent_id == tuned.strategy_id
    assert child.mode_instructions[AIMode.ANALYZE.value] == "Be brief."
    assert active_strategy([tuned], tuned.strategy_id) is tuned
    assert active_strategy([tuned], None) is None
    assert active_strategy([tuned], "builtin") is None
    assert active_strategy([tuned], "missing") is None
    diff = describe_diff(base, resolved)
    assert set(diff) == {"analyze"}


def test_builder_uses_strategy_and_records_id() -> None:
    from pcbrouter.ai.context_builder import BoardContextBuilder

    context = BoardContextBuilder(board()).build(user_prompt="hi")
    tuned = PromptStrategy(name="t", mode_instructions={"analyze": SAFE_ANALYZE})
    request = PromptBuilder().build(
        PromptInputs(
            mode=AIMode.ANALYZE,
            user_prompt="hi",
            context=context,
            model="m",
            strategy=tuned,
        ),
        Conversation(),
    )
    assert SAFE_ANALYZE in request.system_prompt
    assert request.metadata["strategy_id"] == tuned.strategy_id
    plain = PromptBuilder().build(
        PromptInputs(mode=AIMode.ANALYZE, user_prompt="hi", context=context, model="m"),
        Conversation(),
    )
    assert plain.metadata["strategy_id"] == "builtin"


def test_ratings() -> None:
    session = AISession(board(), session_id="s")
    prepared = session.prepare("hi", AIMode.ANALYZE, model="m", provider_name="p")
    assert session.rate_interaction(prepared.request.request_id, 1) is True
    assert session.rate_interaction(prepared.request.request_id, -1) is True
    assert session.rate_interaction(prepared.request.request_id, None) is True
    assert session.rate_interaction("missing", 1) is False
    with pytest.raises(ValueError):
        session.rate_interaction(prepared.request.request_id, 5)
    session.rate_interaction(prepared.request.request_id, 1)
    inter = session.interaction(prepared.request.request_id)
    assert inter is not None and inter.rating == 1
    assert inter.to_dict()["rating"] == 1


def test_settings_round_trip_with_strategies() -> None:
    settings = AISettings()
    tuned = PromptStrategy(name="v1", note="n", mode_instructions={"plan": "Be brief."})
    settings.prompt_strategies.append(tuned)
    settings.active_strategy_id = tuned.strategy_id
    clone = AISettings.model_validate(settings.model_dump(mode="json"))
    assert clone.active_strategy_id == tuned.strategy_id
    assert clone.prompt_strategies[0].mode_instructions == {"plan": "Be brief."}
    with pytest.raises(ValueError):
        clone.prompt_strategies.append(tuned)
        AISettings.model_validate(clone.model_dump(mode="json"))


def test_session_config_carries_strategy() -> None:
    tuned = PromptStrategy(name="t", mode_instructions={"analyze": SAFE_ANALYZE})
    session = AISession(board(), session_id="s", config=AIRuntimeConfig(strategy=tuned))
    prepared = session.prepare("hi", AIMode.ANALYZE, model="m", provider_name="p")
    assert SAFE_ANALYZE in prepared.request.system_prompt


def test_unsafe_override_is_refused_at_build() -> None:
    from pcbrouter.ai.context_builder import BoardContextBuilder

    context = BoardContextBuilder(board()).build(user_prompt="hi")
    bare = PromptStrategy(name="bare", mode_instructions={"analyze": "Be brief."})
    with pytest.raises(ValueError, match="drops safety rules"):
        PromptBuilder().build(
            PromptInputs(
                mode=AIMode.ANALYZE,
                user_prompt="hi",
                context=context,
                model="m",
                strategy=bare,
            ),
            Conversation(),
        )


def test_deterministic_benchmark_and_comparison() -> None:
    tuned = PromptStrategy(
        name="brief", note="shorter analyze", mode_instructions={"analyze": SAFE_ANALYZE}
    )
    reports, comparison = benchmark_all([None, tuned], str(BOARDS))
    assert len(reports) == 2
    assert all(r.total > 0 and r.passed == r.total for r in reports), [
        (c.name if hasattr(c, "name") else c.case, c.detail)
        for r in reports
        for c in (*r.prompt_checks, *r.replay_checks)
        if not c.ok
    ]
    assert comparison is not None
    assert comparison["instruction_diff"]["analyze"]["after_chars"] == len(SAFE_ANALYZE)


def test_safety_issues_flags_dropped_invariants() -> None:
    from pcbrouter.ai.strategy import safety_issues

    assert safety_issues(SAFE_ANALYZE) == []
    issues = safety_issues("Be brief.")
    assert len(issues) == 3
    partial = safety_issues("Reply in JSON. Be brief.")
    assert any("approval" in i for i in partial)
