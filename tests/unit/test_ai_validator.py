"""Semantic validation (spec item 50): schema-valid is not PCB-valid."""

from __future__ import annotations

from typing import Any

import pytest

from pcbrouter.ai.anonymizer import AnonymizationOptions, Anonymizer
from pcbrouter.ai.command_parser import parse_command_payload
from pcbrouter.ai.command_schema import NetTarget
from pcbrouter.ai.command_validator import (
    SemanticValidator,
    SessionLocks,
    Severity,
    ValidationReport,
    ValidationStatus,
    suggest,
)
from pcbrouter.domain.board import Board


def validate(board: Board, payload: dict[str, Any], **kw: Any) -> ValidationReport:
    return SemanticValidator(board, **kw).validate(parse_command_payload(payload))


def route(name: str, **constraints: Any) -> dict[str, Any]:
    return {
        "operation": "route_net",
        "targets": [{"type": "net", "name": name}],
        "constraints": constraints or None,
    }


def _first_name(report: ValidationReport) -> str:
    assert report.resolved is not None
    target = report.resolved.targets[0]
    assert isinstance(target, NetTarget)
    return target.name


def codes(report: ValidationReport, severity: Severity | None = None) -> set[str]:
    return {i.code for i in report.issues if severity is None or i.severity is severity}


def test_existing_net_is_valid(can_board: Board) -> None:
    r = validate(can_board, route("CAN_H"))
    assert r.status is ValidationStatus.VALID
    assert all(c.passed for c in r.checks)
    assert "stage" in codes(r, Severity.INFO)  # routing needs Stage 4: info, not an error


def test_unknown_net_is_invalid_with_suggestions_but_no_substitution(can_board: Board) -> None:
    r = validate(can_board, route("CAN_X"))
    assert r.status is ValidationStatus.INVALID
    issue = next(i for i in r.issues if i.code == "unknown_net")
    assert 'Target net "CAN_X" does not exist' in issue.message
    assert set(issue.suggestions) >= {"CAN_H", "CAN_L"}
    assert r.resolved is not None and _first_name(r) == "CAN_X"  # not substituted


def test_fuzzy_suggestions_only(can_board: Board) -> None:
    r = validate(can_board, route("CANH"))
    assert r.status is ValidationStatus.INVALID
    assert suggest("CANH", [n.name for n in can_board.nets])[0] == "CAN_H"
    assert suggest("can_h", ["CAN_H"]) == ("CAN_H",)


def test_copper_and_non_copper_layers(can_board: Board) -> None:
    assert validate(can_board, route("CAN_H", preferred_layers=["F.Cu"])).is_valid
    edge = validate(can_board, route("CAN_H", preferred_layers=["Edge.Cuts"]))
    assert edge.status is ValidationStatus.INVALID and "not_copper" in codes(edge)
    missing = validate(can_board, route("CAN_H", preferred_layers=["In1.Cu"]))
    assert "unknown_layer" in codes(missing)
    both = validate(can_board, route("CAN_H", preferred_layers=["F.Cu"], forbidden_layers=["F.Cu"]))
    assert "layer_conflict" in codes(both)
    all_forbidden = validate(can_board, route("CAN_H", forbidden_layers=["F.Cu", "B.Cu"]))
    assert "all_layers_forbidden" in codes(all_forbidden)


def test_negative_max_vias_never_reaches_the_validator() -> None:
    from pcbrouter.ai.command_parser import CommandValidationError

    with pytest.raises(CommandValidationError):
        parse_command_payload(route("CAN_H", max_vias=-1))


def test_move_locked_component_is_invalid(can_board: Board) -> None:
    r = validate(
        can_board,
        {
            "operation": "analyze_component",
            "targets": [{"type": "component", "reference": "U2"}],
            "constraints": {"allow_component_movement": True},
        },
    )
    assert r.status is ValidationStatus.INVALID and "locked_component" in codes(r)
    # Nets touching the locked U2: movement allowed elsewhere, but warned.
    w = validate(can_board, route("CAN_H", allow_component_movement=True))
    assert w.status is ValidationStatus.VALID_WITH_WARNINGS and "locked_on_nets" in codes(w)


def test_locked_tracks_cannot_be_ripped_up(can_board: Board) -> None:
    r = validate(can_board, route("GND", allow_ripup=True))
    assert r.status is ValidationStatus.INVALID and "locked_tracks" in codes(r)
    assert validate(can_board, route("GND")).is_valid  # preserving routes is fine


def test_session_locks_are_enforced(can_board: Board) -> None:
    locks = SessionLocks(nets={"VBAT"})
    r = validate(can_board, route("VBAT"), locks=locks)
    assert "locked_net" in codes(r) and not r.is_valid


def test_operation_target_compatibility(can_board: Board) -> None:
    r = validate(
        can_board, {"operation": "lock_component", "targets": [{"type": "net", "name": "CAN_H"}]}
    )
    assert "target_type" in codes(r)
    r = validate(
        can_board,
        {
            "operation": "route_net",
            "targets": [{"type": "net", "name": "CAN_H"}, {"type": "net", "name": "CAN_L"}],
        },
    )
    assert "target_count" in codes(r)
    r = validate(
        can_board, {"operation": "route_group", "targets": [{"type": "net", "name": "CAN_H"}]}
    )
    assert "group_size" in codes(r)
    r = validate(
        can_board,
        {"operation": "set_routing_priority", "targets": [{"type": "net", "name": "CAN_H"}]},
    )
    assert "no_priority" in codes(r)
    r = validate(
        can_board,
        {"operation": "set_net_constraint", "targets": [{"type": "net", "name": "CAN_H"}]},
    )
    assert "no_constraints" in codes(r)


def test_duplicate_nets_across_targets(can_board: Board) -> None:
    r = validate(
        can_board,
        {
            "operation": "route_group",
            "targets": [
                {"type": "net", "name": "CAN_H"},
                {"type": "net_group", "names": ["CAN_H", "CAN_L"]},
            ],
        },
    )
    assert "duplicate_net" in codes(r)


def test_constraint_consistency(can_board: Board) -> None:
    assert "width_order" in codes(
        validate(can_board, route("CAN_H", min_trace_width_mm=0.5, max_trace_width_mm=0.2))
    )
    assert "width_order" in codes(
        validate(can_board, route("CAN_H", min_trace_width_mm=0.2, preferred_trace_width_mm=0.1))
    )
    assert "length_order" in codes(
        validate(
            can_board, route("CAN_H", target_length_mm=50, max_length_mm=20, length_tolerance_mm=1)
        )
    )
    assert "tolerance" in codes(
        validate(can_board, route("CAN_H", target_length_mm=5, length_tolerance_mm=5))
    )
    assert "ripup_conflict" in codes(
        validate(can_board, route("CAN_H", preserve_existing_routes=True, allow_ripup=True))
    )
    assert "via_type" in codes(validate(can_board, route("CAN_H", preferred_via_type="micro")))
    assert "avoid_target" in codes(validate(can_board, route("CAN_H", avoid_nets=["CAN_H"])))
    assert "unknown_net" in codes(validate(can_board, route("CAN_H", avoid_nets=["NOPE"])))
    near_away = validate(
        can_board,
        route(
            "CAN_H",
            keep_near=[{"kind": "component", "name": "U2"}],
            keep_away_from=[{"kind": "component", "name": "U2"}],
        ),
    )
    assert "near_away" in codes(near_away)


def test_unverifiable_constraints_are_warnings(can_board: Board) -> None:
    r = validate(
        can_board,
        {
            "operation": "route_group",
            "targets": [{"type": "net_group", "names": ["CAN_H", "CAN_L"]}],
            "constraints": {
                "avoid_net_classes": ["SWITCHING_POWER"],
                "differential_pair": {"positive_net": "CAN_H", "negative_net": "CAN_L"},
                "impedance_target_ohm": 120,
            },
        },
    )
    assert r.status is ValidationStatus.VALID_WITH_WARNINGS
    assert {"net_classes_unknown", "pair_rules_unavailable", "impedance"} <= codes(r)


def test_high_confidence_does_not_override_validation(can_board: Board) -> None:
    payload = {**route("CAN_X"), "confidence": "high"}
    assert validate(can_board, payload).status is ValidationStatus.INVALID
    low = validate(can_board, {**route("CAN_H"), "confidence": "low"})
    assert "low_confidence" in codes(low, Severity.WARNING)


def test_nothing_to_route_warnings(can_board: Board) -> None:
    r = validate(can_board, route("CAN_L", preserve_existing_routes=False))
    assert "replace_routes" in codes(r)


def test_anonymised_identifiers_map_back_exactly(can_board: Board) -> None:
    anon = Anonymizer(can_board, AnonymizationOptions(net_names=True, references=True))
    token = anon.out(anon_kind("net"), "CAN_H")
    ref = anon.out(anon_kind("reference"), "U2")
    r = SemanticValidator(can_board, anon).validate(
        parse_command_payload(
            {
                "operation": "route_net",
                "targets": [{"type": "net", "name": token}],
                "constraints": {"keep_near": [{"kind": "component", "name": ref}]},
            }
        )
    )
    assert r.is_valid
    assert r.resolved is not None and _first_name(r) == "CAN_H"
    kn = r.resolved.effective_constraints.keep_near or []
    assert kn[0].name == "U2"
    unknown = SemanticValidator(can_board, anon).validate(parse_command_payload(route("NET_999")))
    assert not unknown.is_valid


def anon_kind(name: str):  # type: ignore[no-untyped-def]
    from pcbrouter.ai.anonymizer import EntityKind

    return EntityKind(name)
