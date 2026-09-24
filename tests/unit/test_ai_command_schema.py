"""Schema-level tests for the v2 command envelope (replaces Stage 1's v1 schema tests)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from pcbrouter.ai.command_parser import (
    CommandValidationError,
    parse_command_payload,
    parse_planner_response,
)
from pcbrouter.ai.command_schema import (
    AICommand,
    NetGroupTarget,
    NetTarget,
    Operation,
    OperationCategory,
    PlannerResponse,
    RoutingConstraints,
)
from pcbrouter.ai.exceptions import AIInvalidResponseError, AISchemaValidationError


def cmd(**kw: Any) -> dict[str, Any]:
    return {"operation": "route_net", "targets": [{"type": "net", "name": "CAN_H"}], **kw}


def test_valid_route_net() -> None:
    c = parse_command_payload(cmd(constraints={"max_vias": 2, "minimize_vias": True}))
    assert c.operation is Operation.ROUTE_NET
    assert c.targets == [NetTarget(type="net", name="CAN_H")]
    assert c.effective_constraints.max_vias == 2


def test_valid_route_group_and_analyze_board() -> None:
    group = parse_command_payload(
        {
            "operation": "route_group",
            "targets": [{"type": "net_group", "names": ["CAN_H", "CAN_L"]}],
            "constraints": {"allow_component_movement": False, "priority": "critical"},
        }
    )
    assert isinstance(group.targets[0], NetGroupTarget)
    analyze = parse_command_payload({"operation": "analyze_board", "targets": [{"type": "board"}]})
    assert analyze.operation.category is OperationCategory.READ_ONLY


def test_spec_item1_example_with_plain_string_targets() -> None:
    c = parse_command_payload(
        {
            "operation": "route_group",
            "targets": ["CAN_H", "CAN_L"],
            "constraints": {
                "preserve_existing_routes": True,
                "allow_component_movement": False,
                "minimize_vias": True,
                "avoid_net_classes": ["SWITCHING_POWER"],
            },
        }
    )
    assert c.targets == [NetGroupTarget(type="net_group", names=["CAN_H", "CAN_L"])]
    assert c.effective_constraints.avoid_net_classes == ["SWITCHING_POWER"]


def test_stage1_compact_form_still_accepted() -> None:
    c = parse_command_payload(
        '{"operation": "route_net", "target": "GND", '
        '"constraints": {"move_components": false, "avoid_layers": ["B.Cu"]}}'
    )
    assert c.targets == [NetTarget(type="net", name="GND")]
    assert c.effective_constraints.allow_component_movement is False
    assert c.effective_constraints.forbidden_layers == ["B.Cu"]
    board = parse_command_payload({"operation": "route_board"})
    assert board.targets == []  # v2: route_board with no targets means the whole board


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        (cmd(constraints={"min_trace_width_mm": -0.1}), "min_trace_width_mm"),
        (cmd(constraints={"preferred_trace_width_mm": 0}), "preferred_trace_width_mm"),
        (cmd(constraints={"min_clearance_mm": -1}), "min_clearance_mm"),
        (cmd(constraints={"max_vias": -1}), "max_vias"),
        (cmd(constraints={"max_vias": 10_000}), "max_vias"),
        ({"targets": [{"type": "net", "name": "X"}]}, "operation"),
        (cmd(targets=[{"type": "wire", "name": "X"}]), "targets"),
        (cmd(targets=[{"type": "net", "reference": "U1"}]), "targets"),
        (cmd(targets=[{"type": "net", "name": "A"}, {"type": "net", "name": "A"}]), "duplicates"),
        (
            {"operation": "route_group", "targets": [{"type": "net_group", "names": ["A", "A"]}]},
            "duplicate",
        ),
        (cmd(constraints={"preferred_layers": ["F.Cu", "F.Cu"]}), "duplicates"),
        (cmd(delete_all=True), "delete_all"),
        (cmd(constraints={"geometry": [[0, 0], [1, 1]]}), "geometry"),
        (cmd(targets=[{"type": "net", "name": "A\nB"}]), "targets"),
        (
            cmd(
                targets=[
                    {"type": "area", "x_min_mm": 5, "y_min_mm": 0, "x_max_mm": 1, "y_max_mm": 1}
                ]
            ),
            "area",
        ),
        (
            cmd(constraints={"differential_pair": {"positive_net": "A", "negative_net": "A"}}),
            "different",
        ),
        ({"operation": "execute_shell", "command": "rm -rf /"}, "operation"),
        (cmd(confidence="certain"), "confidence"),
    ],
)
def test_invalid_commands_fail_schema(payload: dict[str, Any], fragment: str) -> None:
    with pytest.raises(CommandValidationError) as info:
        parse_command_payload(json.dumps(payload))
    assert fragment in str(info.value)


@pytest.mark.parametrize("text", ["not json", "[1,2]", '"route"', "", "{", "null"])
def test_malformed_json(text: str) -> None:
    with pytest.raises(CommandValidationError):
        parse_command_payload(text)


def test_malicious_operation_fails_completely_in_planner_response() -> None:
    body = {
        "schema_version": 2,
        "mode": "command",
        "message": "ok",
        "commands": [{"operation": "execute_shell", "command": "rm -rf /"}],
    }
    with pytest.raises(AISchemaValidationError) as info:
        parse_planner_response(json.dumps(body))
    assert any("operation" in e for e in info.value.errors)


def test_planner_response_rejects_unknown_top_level_keys_and_versions() -> None:
    base = {"schema_version": 2, "mode": "analyze", "message": "hi"}
    assert parse_planner_response(json.dumps(base)).response.message == "hi"
    for bad in (
        {**base, "python": "import os"},
        {**base, "schema_version": 1},
        {**base, "mode": "execute"},
    ):
        with pytest.raises(AISchemaValidationError):
            parse_planner_response(json.dumps(bad))
    with pytest.raises(AIInvalidResponseError):
        parse_planner_response("Here you go: " + json.dumps(base))


def test_confidence_does_not_bypass_schema() -> None:
    with pytest.raises(CommandValidationError):
        parse_command_payload(cmd(confidence="high", constraints={"max_vias": -5}))


def test_models_are_frozen_and_safe_defaults() -> None:
    c = parse_command_payload(cmd())
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        c.operation = Operation.ROUTE_BOARD  # type: ignore[misc]
    rc = RoutingConstraints()
    assert rc.effective_preserve_existing_routes is True
    assert rc.effective_allow_ripup is False
    assert rc.effective_allow_component_movement is False
    assert rc.is_empty()


def test_every_operation_has_a_category() -> None:
    for op in Operation:
        assert isinstance(op.category, OperationCategory)
    assert Operation.ROUTE_BOARD.category is OperationCategory.ROUTING
    assert Operation.LOCK_NET.category is OperationCategory.CONSTRAINT


def test_planner_response_command_limit() -> None:
    one = {"operation": "analyze_board", "targets": [{"type": "board"}]}
    body = {"schema_version": 2, "mode": "command", "message": "x", "commands": [one] * 11}
    with pytest.raises(AISchemaValidationError):
        parse_planner_response(json.dumps(body))
    ten = {**body, "commands": [one] * 10}
    assert len(PlannerResponse.model_validate(ten).commands or []) == 10
    assert AICommand.model_validate(one).targets[0].type == "board"
