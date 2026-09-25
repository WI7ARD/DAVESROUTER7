"""Stage 8 model: locks, region locks, user constraints, sections, diffs, explanations."""

from __future__ import annotations

from pathlib import Path

from pcbrouter.domain.geometry import BoundingBox
from pcbrouter.domain.units import mm_to_internal
from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.routing.inspect import (
    candidate_objects,
    explain_vias,
    route_diff,
    route_info,
    section_ids,
)
from pcbrouter.routing.optimize import OptimizeGoal, optimize_nets
from pcbrouter.routing.request import RouteRequest, with_user_constraints
from pcbrouter.routing.result import RouteStatus
from pcbrouter.routing.router import Router
from pcbrouter.routing.working_board import Provenance, WorkingBoard

BOARDS = Path(__file__).parent.parent / "fixtures" / "boards"
MM = mm_to_internal


def working(name: str = "router_basic.kicad_pcb") -> WorkingBoard:
    path = BOARDS / name
    return WorkingBoard(load_board(path).board, load_project_rules(path))


def routed(wb: WorkingBoard, net: str, **kw: object) -> object:
    res = Router(wb.engine).route_net(RouteRequest(net, candidates=1, **kw))  # type: ignore[arg-type]
    assert res.best is not None, res.summary()
    return wb.commit_proposals([res.best.proposal], f"route {net}", Provenance.ROUTER_GENERATED)


def test_region_lock_blocks_new_routes_and_protects_copper() -> None:
    wb = working()
    # the only way for A across the F.Cu wall is under it; lock a band across the
    # whole board height at the wall on both layers -> no route
    band = BoundingBox(MM(13), MM(-1), MM(17), MM(21))
    req = with_user_constraints(RouteRequest("A", candidates=1), None, [band])
    assert Router(wb.engine).route_net(req).status is RouteStatus.NO_ROUTE
    commit = routed(wb, "C")
    wb.locked_regions.append(BoundingBox(MM(3), MM(15), MM(13), MM(17)))
    assert all(wb.is_locked(t.id, "C") for t in commit.added_tracks)
    rep = optimize_nets(wb, ["C"], OptimizeGoal.SHORTER)
    assert "C" in rep.skipped  # locked copper is not rerouted


def test_component_lock_protects_attached_routes() -> None:
    wb = working()
    commit = routed(wb, "B")
    wb.locks.add("comp:TP3")
    touching = [t for t in commit.added_tracks if wb.is_locked(t.id, "B")]
    assert touching  # the segment on TP3's pad is protected


def test_user_constraints_are_applied_and_still_validated() -> None:
    wb = working()
    req = with_user_constraints(
        RouteRequest("B", candidates=1),
        {"width_mm": 0.4, "max_vias": 0, "allowed_layers": ["F.Cu"], "avoid_box": [6, 1, 11, 7]},
    )
    res = Router(wb.engine).route_net(req)
    assert res.status is RouteStatus.SUCCESS
    assert all(s.width == MM(0.4) and s.layer == "F.Cu" for s in res.best.proposal.segments)
    too_thin = with_user_constraints(RouteRequest("B", candidates=1), {"width_mm": 0.05})
    assert Router(wb.engine).route_net(too_thin).status is RouteStatus.INVALID_REQUEST


def test_section_reroute_and_diff() -> None:
    wb = working()
    commit = routed(wb, "B")
    tid = commit.added_tracks[0].id
    sec = section_ids(wb, tid)
    assert tid in sec and set(sec) <= {t.id for t in commit.added_tracks}
    fork = wb.fork()
    fork.commit_objects((), (), sec, "remove section", validate=False)
    res = Router(fork.engine).route_net(RouteRequest("B", candidates=1))
    assert res.status is RouteStatus.SUCCESS
    tracks, vias = candidate_objects(wb, res.best.proposal)
    diff = route_diff(wb, tracks, vias, sec)
    assert diff.added_segments == len(tracks) and diff.removed_segments == len(sec)
    assert diff.drc_errors_before == diff.drc_errors_after == 0
    assert any("length" in line for line in diff.lines())
    c2 = wb.commit_proposals([res.best.proposal], "reroute", remove_ids=sec)  # validated
    assert c2.removed_tracks and not wb.engine.run_drc().errors
    src_track = next(
        t for t in wb.board.tracks if wb.provenance_of(t.id) is Provenance.SOURCE_EXISTING
    )
    assert section_ids(wb, src_track.id) == []  # source copper is never a reroute section


def test_route_info_and_via_explanation() -> None:
    wb = working()
    commit = routed(wb, "A")
    info = route_info(wb, commit.added_tracks[0].id)
    assert info is not None and info["net"] == "A" and info["net_vias"] == 2
    assert info["provenance"] == "router_generated" and "rule_width" in info
    facts = explain_vias(wb, "A")
    assert facts["vias"] == 2 and facts["zero_via_route"].startswith("none found")
    b = routed(wb, "B")
    assert explain_vias(wb, "B")["answer"] == "the route uses no vias"
    _ = b
