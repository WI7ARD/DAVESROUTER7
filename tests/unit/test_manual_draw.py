"""Manual trace/via drawing: snapping, size resolution, track/via builders."""

from __future__ import annotations

from pathlib import Path

import pytest

from pcbrouter.board_engine import EngineConfig
from pcbrouter.domain.geometry import Point
from pcbrouter.domain.units import internal_to_mm, mm_to_internal
from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.routing.manual import (
    ManualDrawError,
    build_manual_tracks,
    build_manual_via,
    new_run_id,
    resolve_manual_via,
    resolve_manual_width,
    snap_45,
    snap_to_grid,
)
from pcbrouter.routing.working_board import WorkingBoard

BOARDS = Path(__file__).parent.parent / "fixtures" / "boards"
MM = mm_to_internal


def working(name: str = "router_basic.kicad_pcb") -> WorkingBoard:
    path = BOARDS / name
    return WorkingBoard(load_board(path).board, load_project_rules(path))


def unruled() -> WorkingBoard:
    path = BOARDS / "can_node.kicad_pcb"
    return WorkingBoard(
        load_board(path).board, load_project_rules(path), config=EngineConfig(conservative=False)
    )


def test_snap_to_grid_rounds_to_nearest() -> None:
    grid = MM(0.05)
    assert snap_to_grid(Point(MM(1.03), MM(2.07)), grid) == Point(MM(1.05), MM(2.05))
    assert snap_to_grid(Point(MM(1.0), MM(2.0)), 0) == Point(MM(1.0), MM(2.0))


def test_snap_45_keeps_axis_and_diagonal() -> None:
    s = Point(0, 0)
    assert snap_45(s, Point(MM(2.0), MM(0.0))) == Point(MM(2.0), MM(0))
    assert snap_45(s, Point(MM(0.0), MM(-2.0))) == Point(MM(0), MM(-2.0))
    diag = snap_45(s, Point(MM(2.0), MM(2.1)))
    assert diag.x == diag.y  # snapped onto the diagonal
    assert snap_45(s, s) == s  # zero-length unchanged


def test_snap_45_snaps_near_misses() -> None:
    s = Point(0, 0)
    # 26.5 degrees -> snaps to the 45-degree diagonal, length preserved
    out = snap_45(s, Point(MM(4.0), MM(2.0)))
    assert out.x == out.y
    assert out.x > 0 and out.y > 0
    # 63 degrees -> snaps to vertical
    out = snap_45(s, Point(MM(1.0), MM(4.0)))
    assert out.x == MM(0)
    assert out.y > MM(4.0) - MM(0.5)


def test_resolve_width_prefers_workbench_value() -> None:
    wb = working()
    nets = sorted({n.name for n in wb.board.nets if n.name})
    net = nets[0]
    rules = wb.engine.resolver.width_rules(net)
    assert rules.minimum.value is not None
    wb.net_constraints[net] = {"width_mm": internal_to_mm(rules.minimum.value) + 0.5}
    assert resolve_manual_width(wb, net) == MM(internal_to_mm(rules.minimum.value) + 0.5)


def test_resolve_width_falls_back_to_rules() -> None:
    wb = working()
    nets = sorted({n.name for n in wb.board.nets if n.name})
    net = nets[0]
    rules = wb.engine.resolver.width_rules(net)
    expected = rules.preferred.value or rules.minimum.value
    assert expected is not None
    assert resolve_manual_width(wb, net) == expected


def test_resolve_width_refuses_below_minimum() -> None:
    wb = working()
    nets = sorted({n.name for n in wb.board.nets if n.name})
    wb.net_constraints[nets[0]] = {"width_mm": 0.01}
    with pytest.raises(ManualDrawError, match="below the hard minimum"):
        resolve_manual_width(wb, nets[0])


def test_resolve_width_fails_fast_without_rules() -> None:
    wb = WorkingBoard(
        load_board(BOARDS / "can_node.kicad_pcb").board,
        load_project_rules(BOARDS / "can_node.kicad_pcb"),
    )
    nets = sorted({n.name for n in wb.board.nets if n.name})
    with pytest.raises(ManualDrawError, match="minimum track width"):
        resolve_manual_width(wb, nets[0])


def test_resolve_via_needs_known_sizes() -> None:
    wb = working()
    nets = sorted({n.name for n in wb.board.nets if n.name})
    sizes = resolve_manual_via(wb, nets[0])
    assert sizes.diameter > sizes.drill > 0
    unruled = WorkingBoard(
        load_board(BOARDS / "can_node.kicad_pcb").board,
        load_project_rules(BOARDS / "can_node.kicad_pcb"),
    )
    unets = sorted({n.name for n in unruled.board.nets if n.name})
    with pytest.raises(ManualDrawError, match="via size"):
        resolve_manual_via(unruled, unets[0])


def test_build_tracks_skips_zero_length_and_ids_unique() -> None:
    pts = [Point(0, 0), Point(0, 0), Point(MM(1.0), MM(0.0)), Point(MM(1.0), MM(1.0))]
    run = new_run_id()
    tracks = build_manual_tracks("GND", pts, MM(0.25), "F.Cu", run)
    assert len(tracks) == 2
    assert all(t.net_name == "GND" and t.layer == "F.Cu" and t.width == MM(0.25) for t in tracks)
    assert len({t.id for t in tracks}) == 2
    other = build_manual_tracks("GND", pts, MM(0.25), "F.Cu", new_run_id())
    assert {t.id for t in tracks}.isdisjoint({t.id for t in other})


def test_build_via_spans_layers() -> None:
    via = build_manual_via("GND", Point(0, 0), MM(0.8), MM(0.4), "F.Cu", "B.Cu", new_run_id())
    assert via.start_layer == "F.Cu" and via.end_layer == "B.Cu"
    assert via.drill == MM(0.4)


def test_unruled_expert_board_resolves_width_with_bulk_value() -> None:
    """Bulk widths (previous batch) unblock manual traces on rule-less boards."""
    from pcbrouter.ui.workbench import apply_constraints_to_all

    wb = unruled()
    nets = sorted({n.name for n in wb.board.nets if n.name})
    applied, _ = apply_constraints_to_all(wb, {"width_mm": 0.25})
    assert applied > 0
    assert resolve_manual_width(wb, nets[0]) == MM(0.25)
