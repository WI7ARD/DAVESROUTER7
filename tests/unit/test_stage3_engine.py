"""Stage 3 rules, collision, connectivity, DRC, occupancy and safety tests."""

from __future__ import annotations

import hashlib
import random
from pathlib import Path

import pytest

from pcbrouter.board_engine import BoardEngine, EngineConfig
from pcbrouter.domain.geometry import BoundingBox, Point
from pcbrouter.domain.units import mm_to_internal
from pcbrouter.drc.result import DRCStatus
from pcbrouter.drc.violation import Severity, ViolationKind
from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.routing.collision import ValidationStatus, ViolationType
from pcbrouter.routing.connectivity import NetStatus
from pcbrouter.routing.occupancy import CellState
from pcbrouter.routing.proposal import RouteProposal, RouteSegment, RouteVia
from pcbrouter.rules.conditions import parse_condition
from pcbrouter.rules.model import RuleSourceKind
from pcbrouter.rules.overrides import NetOverride, RuleOverrides
from pcbrouter.spatial.grid_index import GridIndex
from pcbrouter.spatial.index import LinearIndex

BOARDS = Path(__file__).parent.parent / "fixtures" / "boards"
MM = mm_to_internal


def P(x: float, y: float) -> Point:
    return Point(MM(x), MM(y))


def engine_for(name: str, **kw: object) -> BoardEngine:
    path = BOARDS / name
    result = load_board(path)
    return BoardEngine(result.board, load_project_rules(path), **kw)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def rules_engine() -> BoardEngine:
    return engine_for("stage3_rules.kicad_pcb")


# ---------------------------------------------------------------- rules
def test_net_class_and_custom_rule_resolution(rules_engine: BoardEngine) -> None:
    r = rules_engine.resolver
    vbat = r.resolve_net_clearance("VBAT")
    assert vbat.value == MM(0.3) and vbat.source.kind is RuleSourceKind.NET_CLASS
    assert r.resolve_net_class("GND").classes == ("Power",)
    can = r.width_rules("/CAN_H")
    assert can.minimum.source.kind is RuleSourceKind.CUSTOM_RULE
    assert r.resolve_net_class("/CAN_L").classes  # pattern /CAN_*


def test_unknown_rule_is_not_fabricated() -> None:
    engine = engine_for("stage3_unsupported.kicad_pcb")
    assert engine.ruleset.critical_unsupported
    # 0.4 mm from R1 pad 1: legal under the known 0.2 mm clearance, but the
    # unsupported insideCourtyard rule may require 0.5 mm -> not provably legal.
    res = engine.validator.validate_segment("GND", "F.Cu", P(3, 8.5), P(5, 8.5), MM(0.25))
    assert res.status is ValidationStatus.RULE_UNKNOWN, res
    relaxed = engine_for("stage3_unsupported.kicad_pcb", config=EngineConfig(conservative=False))
    res2 = relaxed.validator.validate_segment("GND", "F.Cu", P(3, 8.5), P(5, 8.5), MM(0.25))
    assert res2.status is not ValidationStatus.RULE_UNKNOWN


def test_condition_parser_subset() -> None:
    cond = parse_condition("A.NetClass == 'Power' && !(B.NetName == 'GND')")
    assert cond is not None


def test_override_can_tighten_but_not_weaken(rules_engine: BoardEngine) -> None:
    tight = rules_engine.with_overrides(RuleOverrides(nets={"SIG": NetOverride(width=MM(0.5))}))
    assert tight.resolver.resolve_trace_width("SIG").value == MM(0.5)
    weak = rules_engine.with_overrides(RuleOverrides(nets={"SIG": NetOverride(width=MM(0.05))}))
    res = weak.validator.validate_segment("SIG", "F.Cu", P(2, 2), P(6, 2), MM(0.05))
    assert res.status is ValidationStatus.INVALID
    assert any(c.violation is ViolationType.MIN_WIDTH for c in res.collisions)


# ---------------------------------------------------------------- collision
def test_segment_outside_board_and_keepout(rules_engine: BoardEngine) -> None:
    v = rules_engine.validator
    out = v.validate_segment("SIG", "F.Cu", P(39.9, 1), P(45, 1), MM(0.2))
    assert out.status is ValidationStatus.INVALID
    keep = v.validate_segment("SIG", "F.Cu", P(25.5, 25), P(28.5, 25), MM(0.2))
    assert any(c.violation is ViolationType.KEEPOUT for c in keep.collisions)
    ok = v.validate_segment("SIG", "F.Cu", P(2, 2), P(6, 2), MM(0.2))
    assert ok.legal, ok.messages()
    cut = v.validate_segment("SIG", "F.Cu", P(31, 23), P(35, 23), MM(0.2))
    assert not cut.legal


def test_via_rules(rules_engine: BoardEngine) -> None:
    v = rules_engine.validator
    bad = v.validate_via("GND", P(4, 26), "F.Cu", "B.Cu", MM(0.8), MM(0.4))
    assert bad.status is ValidationStatus.INVALID
    ok = v.validate_via("GND", P(3, 3), "F.Cu", "B.Cu", MM(0.8), MM(0.4))
    assert ok.legal, ok.messages()


def test_route_proposal_is_not_added_to_board(rules_engine: BoardEngine) -> None:
    board = rules_engine.board
    n = len(board.tracks)
    prop = RouteProposal(
        "SIG",
        segments=(RouteSegment(P(2, 2), P(6, 2), "F.Cu", MM(0.2)),),
        vias=(RouteVia(P(6, 2), "F.Cu", "B.Cu", MM(0.8), MM(0.4)),),
    )
    result = rules_engine.validator.validate_route(prop)
    assert result.elements and len(board.tracks) == n


# ---------------------------------------------------------------- connectivity / DRC
def test_connectivity_statuses(rules_engine: BoardEngine) -> None:
    c = rules_engine.connectivity
    assert c.nets["/CAN_L"].status is NetStatus.FULLY_CONNECTED
    assert c.nets["HV"].status is NetStatus.UNROUTED
    assert c.nets["GND"].status is NetStatus.PARTIALLY_CONNECTED
    assert c.estimated_remaining_connections("HV") >= 1


def test_internal_drc_finds_exactly_the_intended_errors(rules_engine: BoardEngine) -> None:
    result = rules_engine.run_drc()
    assert result.status is DRCStatus.FAIL
    kinds = [v.kind for v in result.errors]
    assert len(kinds) == 6
    assert ViolationKind.EDGE_CROSSING in kinds and ViolationKind.MIN_TRACK_WIDTH in kinds
    assert ViolationKind.TRACK_IN_KEEPOUT in kinds and ViolationKind.VIA_IN_KEEPOUT in kinds
    assert all(v.rule_source for v in result.errors if v.required_value is not None)
    again = rules_engine.run_drc()
    assert [v.id for v in again.violations] == [v.id for v in result.violations]


def test_clean_board_passes() -> None:
    result = engine_for("stage3_clean.kicad_pcb").run_drc()
    assert not result.errors, [v.message for v in result.errors]
    assert all(v.severity is not Severity.ERROR for v in result.violations)


# ---------------------------------------------------------------- occupancy / index
def test_occupancy_width_dependence(rules_engine: BoardEngine) -> None:
    thin = rules_engine.occupancy("F.Cu", "SIG", MM(0.15), MM(0.25))
    wide = rules_engine.occupancy("F.Cu", "SIG", MM(1.0), MM(0.25))
    assert thin.cells.dtype.name == "uint8"
    free_thin = int((thin.cells == CellState.FREE).sum())
    free_wide = int((wide.cells == CellState.FREE).sum())
    assert free_wide < free_thin
    assert thin.state_at(P(-5, -5)) is CellState.OUTSIDE_BOARD


def test_grid_index_matches_linear_reference() -> None:
    rnd = random.Random(7)
    grid, lin = GridIndex(MM(1)), LinearIndex()
    for i in range(400):
        x, y = rnd.randint(0, MM(50)), rnd.randint(0, MM(50))
        box = BoundingBox(x, y, x + rnd.randint(1, MM(5)), y + rnd.randint(1, MM(5)))
        grid.insert(f"o{i}", box)
        lin.insert(f"o{i}", box)
    for i in range(0, 400, 3):
        grid.remove(f"o{i}")
        lin.remove(f"o{i}")
    for _ in range(100):
        x, y = rnd.randint(0, MM(50)), rnd.randint(0, MM(50))
        q = BoundingBox(x, y, x + MM(3), y + MM(3))
        assert sorted(grid.query(q)) == sorted(lin.query(q))


def test_stage3_operations_leave_source_unchanged(tmp_path: Path) -> None:
    src = BOARDS / "stage3_rules.kicad_pcb"
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    engine = engine_for("stage3_rules.kicad_pcb")
    engine.run_drc()
    engine.occupancy("F.Cu", None, MM(0.2), MM(0.5))
    engine.validator.validate_segment("SIG", "F.Cu", P(2, 2), P(6, 2), MM(0.2))
    engine.congestion("F.Cu")
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before
