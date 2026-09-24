from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from pcbrouter.ai.command_schema import (
    MAX_COMMAND_JSON_BYTES,
    MODIFYING_OPERATIONS,
    AnalyzeBoard,
    CommandValidationError,
    LockTrack,
    ProtectArea,
    RouteNet,
    command_json_schema,
    parse_command,
    validate_against_board,
)
from pcbrouter.kicad import LoadResult

SPEC_EXAMPLE = {
    "operation": "route_net",
    "target": "CAN_H",
    "constraints": {
        "max_vias": 2,
        "preferred_layers": ["F.Cu", "B.Cu"],
        "move_components": False,
        "preserve_existing_routes": True,
    },
}

ALL_OPERATIONS = {
    "analyze_board", "route_net", "route_group", "route_board", "set_constraint",
    "lock_component", "lock_track", "protect_area", "optimize_route", "reduce_vias",
}  # fmt: skip


def test_spec_example_validates() -> None:
    cmd = parse_command(SPEC_EXAMPLE)
    assert isinstance(cmd, RouteNet)
    assert cmd.target == "CAN_H"
    assert cmd.constraints.max_vias == 2
    assert cmd.constraints.preferred_layers == ["F.Cu", "B.Cu"]
    assert cmd.constraints.move_components is False


def test_json_text_input_and_defaults() -> None:
    cmd = parse_command('{"operation": "analyze_board"}')
    assert isinstance(cmd, AnalyzeBoard) and cmd.focus == "overview"
    route = parse_command('{"operation": "route_net", "target": "GND"}')
    assert isinstance(route, RouteNet)
    assert route.constraints.move_components is False  # safe default
    assert route.constraints.preserve_existing_routes is True


@pytest.mark.parametrize(
    "payload",
    [
        {"operation": "route_net", "target": "X", "delete_everything": True},  # unknown key
        {"operation": "rm_rf", "target": "X"},  # unknown operation
        {"target": "X"},  # missing discriminator
        {"operation": "route_net"},  # missing target
        {"operation": "route_net", "target": ""},
        {"operation": "route_net", "target": "A\nB"},  # control char
        {"operation": "route_net", "target": "X", "constraints": {"max_vias": -1}},
        {"operation": "route_net", "target": "X", "constraints": {"max_vias": 1000}},
        {"operation": "route_net", "target": "X", "constraints": {"preferred_layers": ["F.SilkS"]}},
        {
            "operation": "route_net",
            "target": "X",
            "constraints": {"preferred_layers": ["F.Cu"], "avoid_layers": ["F.Cu"]},
        },
        {"operation": "route_net", "target": "X", "constraints": {"track_width_mm": 0}},
        {"operation": "route_net", "target": "X", "constraints": {"geometry": [[0, 0]]}},
        {"operation": "route_group", "targets": []},
        {"operation": "route_group", "targets": ["A", "A"]},
        {"operation": "lock_track"},
        {"operation": "lock_component", "reference": "R 1"},
        {
            "operation": "protect_area",
            "area": {"x_min_mm": 5, "y_min_mm": 0, "x_max_mm": 1, "y_max_mm": 1},
        },
        {"operation": "optimize_route", "goals": []},
        {"operation": "optimize_route", "goals": ["teleport"]},
    ],
)
def test_invalid_commands_fail(payload: dict[str, object]) -> None:
    with pytest.raises(CommandValidationError) as info:
        parse_command(payload)
    assert info.value.errors


@pytest.mark.parametrize("text", ["not json", "[1, 2]", '"route"', "null"])
def test_non_object_json_fails(text: str) -> None:
    with pytest.raises(CommandValidationError):
        parse_command(text)


def test_oversized_payload_rejected() -> None:
    with pytest.raises(CommandValidationError, match="too large"):
        parse_command(" " * (MAX_COMMAND_JSON_BYTES + 1))


def test_valid_variants() -> None:
    lock = parse_command({"operation": "lock_track", "net": "GND"})
    assert isinstance(lock, LockTrack)
    area = parse_command(
        {
            "operation": "protect_area",
            "layers": ["In1.Cu"],
            "area": {"x_min_mm": 0, "y_min_mm": 0, "x_max_mm": 10, "y_max_mm": 5},
        }
    )
    assert isinstance(area, ProtectArea)
    assert parse_command({"operation": "route_board", "exclude_nets": ["GND"]})
    assert parse_command({"operation": "reduce_vias", "max_vias_per_net": 1})
    assert parse_command({"operation": "optimize_route", "goals": ["length", "vias"]})


def test_commands_are_immutable() -> None:
    cmd = parse_command(SPEC_EXAMPLE)
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError
        cmd.target = "OTHER"  # type: ignore[misc, union-attr]


def test_json_schema_covers_every_operation() -> None:
    text = json.dumps(command_json_schema())
    for op in ALL_OPERATIONS:
        assert f'"{op}"' in text
    assert MODIFYING_OPERATIONS <= ALL_OPERATIONS


def test_semantic_validation_against_board(load_fixture: Callable[[str], LoadResult]) -> None:
    board = load_fixture("four_layer.kicad_pcb").board
    ok = parse_command(
        {
            "operation": "route_net",
            "target": "/DATA",
            "constraints": {"preferred_layers": ["In2.Cu"]},
        }
    )
    assert validate_against_board(ok, board) == []
    bad = parse_command(
        {
            "operation": "route_net",
            "target": "CAN_H",
            "constraints": {"preferred_layers": ["In5.Cu"]},
        }
    )
    problems = validate_against_board(bad, board)
    assert any("unknown net 'CAN_H'" in p for p in problems)
    assert any("In5.Cu" in p for p in problems)
    assert (
        validate_against_board(
            parse_command({"operation": "lock_component", "reference": "U1"}), board
        )
        == []
    )
    assert validate_against_board(
        parse_command({"operation": "lock_component", "reference": "U99"}), board
    )
    assert validate_against_board(
        parse_command({"operation": "lock_track", "track_ids": ["nope"]}), board
    )
