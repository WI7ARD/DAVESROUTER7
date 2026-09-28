"""ERC (electrical) DRC checks: single-pin nets and floating copper islands."""

from __future__ import annotations

from pathlib import Path

from pcbrouter.domain.geometry import Point
from pcbrouter.domain.units import mm_to_internal
from pcbrouter.drc.engine import run_geometry_check
from pcbrouter.drc.result import DRCResult
from pcbrouter.drc.violation import ERC_KINDS, ViolationKind
from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.routing.manual import build_manual_tracks, new_run_id
from pcbrouter.routing.working_board import WorkingBoard

BOARDS = Path(__file__).parent.parent / "fixtures" / "boards"
MM = mm_to_internal


def check(name: str) -> DRCResult:
    path = BOARDS / name
    wb = WorkingBoard(load_board(path).board, load_project_rules(path))
    engine = wb.engine
    return run_geometry_check(engine.geometry, engine.resolver, engine.connectivity)


def kinds(result: DRCResult) -> set[ViolationKind]:
    return {v.kind for v in result.violations}


def test_single_pin_net_flagged_informational() -> None:
    """can_node's SW_NODE has one pad: nothing to connect (INFO, not an error)."""
    result = check("can_node.kicad_pcb")
    singles = [v for v in result.violations if v.kind is ViolationKind.SINGLE_PIN_NET]
    assert [v.net_a for v in singles] == ["SW_NODE"]
    assert all(v.severity.value == "info" for v in singles)
    assert all(v.location is not None for v in singles)  # marked on the ERC layer
    assert not result.errors  # single-pin nets never fail a board


def test_floating_copper_flagged_warning() -> None:
    """A track touching no pad is dead copper (WARNING with a marker)."""
    path = BOARDS / "router_basic.kicad_pcb"
    wb = WorkingBoard(load_board(path).board, load_project_rules(path))
    tracks = build_manual_tracks(
        "B",
        [Point(MM(20.0), MM(10.0)), Point(MM(23.0), MM(10.0))],
        MM(0.25),
        "F.Cu",
        new_run_id(),
    )
    wb.commit_objects(tracks, (), (), "floating test track")
    engine = wb.engine
    result = run_geometry_check(engine.geometry, engine.resolver, engine.connectivity)
    floats = [v for v in result.violations if v.kind is ViolationKind.FLOATING_COPPER]
    assert len(floats) == 1
    assert floats[0].severity.value == "warning"
    assert floats[0].location is not None
    assert floats[0].object_a is not None
    # deterministic: same board, same finding, same id
    again = run_geometry_check(engine.geometry, engine.resolver, engine.connectivity)
    assert [v.id for v in again.violations] == [v.id for v in result.violations]


def test_erc_kinds_are_electrical_only() -> None:
    assert (
        frozenset(
            {
                ViolationKind.UNCONNECTED,
                ViolationKind.SINGLE_PIN_NET,
                ViolationKind.FLOATING_COPPER,
            }
        )
        == ERC_KINDS
    )
    assert kinds(check("router_basic.kicad_pcb")) >= {ViolationKind.UNCONNECTED}
