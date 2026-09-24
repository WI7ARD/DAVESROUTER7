"""Stage 5: full-board routing, ordering, rip-up, locks, optimisation, diff pairs."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from pcbrouter.domain.units import mm_to_internal
from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.routing.board_router import (
    BoardRouter,
    BoardRouterSettings,
    BoardRoutingControl,
    BoardStatus,
    RouteGroup,
    Strategy,
    TaskKind,
    diff_pairs,
    make_plan,
)
from pcbrouter.routing.connectivity import NetStatus, net_connectivity
from pcbrouter.routing.diffpair import IMPEDANCE, pair_metrics
from pcbrouter.routing.optimize import OptimizeGoal, net_metrics, optimize_nets
from pcbrouter.routing.request import RouteRequest
from pcbrouter.routing.result import RouteStatus
from pcbrouter.routing.router import Router
from pcbrouter.routing.working_board import CommitError, Provenance, WorkingBoard

BOARDS = Path(__file__).parent.parent / "fixtures" / "boards"
MM = mm_to_internal


def working(name: str) -> WorkingBoard:
    path = BOARDS / name
    return WorkingBoard(load_board(path).board, load_project_rules(path))


@pytest.fixture(scope="module")
def dense_result() -> tuple[WorkingBoard, object]:
    wb = working("router_dense.kicad_pcb")
    return wb, BoardRouter(wb, BoardRouterSettings()).run()


def test_plan_ordering_is_deterministic_and_configurable() -> None:
    wb = working("router_dense.kicad_pcb")
    crit = make_plan(wb, BoardRouterSettings(strategy=Strategy.CRITICAL_FIRST))
    kinds = [t.kind for t in crit.tasks]
    assert kinds[:2] == [TaskKind.DIFF_PAIR, TaskKind.DIFF_PAIR]  # pair kept together
    assert TaskKind.POWER in kinds and "LOCKED" not in crit.nets  # already routed
    short = make_plan(wb, BoardRouterSettings(strategy=Strategy.SHORTEST_FIRST))
    lengths = [t.airwire_length for t in short.tasks if t.kind is TaskKind.SIGNAL]
    assert lengths == sorted(lengths)
    prio = make_plan(wb, BoardRouterSettings(priorities={"S7": 100}))
    assert prio.tasks[0].net == "S7"
    grouped = make_plan(wb, BoardRouterSettings(groups=(RouteGroup("BUS", ("S1", "S6"), 50),)))
    assert {grouped.tasks[0].net, grouped.tasks[1].net} == {"S1", "S6"}
    assert make_plan(wb, BoardRouterSettings()).nets == crit.nets
    assert diff_pairs(["USB_P", "USB_N", "A+", "A-", "X"]) == [("A+", "A-"), ("USB_P", "USB_N")]


def test_full_board_route(dense_result: tuple[WorkingBoard, object]) -> None:
    wb, result = dense_result
    assert result.status is BoardStatus.FULLY_ROUTED, result.summary()
    m = result.metrics
    assert m.nets_completed == m.nets_attempted == 11 and m.completion == 1.0
    assert m.new_vias > 0 and m.total_length_nm > 0 and m.expanded_nodes > 0
    # nothing touched the live working board: the job ran on a fork
    assert not wb.modified
    # the power net used its class width, the locked source track is intact
    vbus = [t for t in result.added_tracks if t.net_name == "VBUS"]
    assert vbus and all(t.width == MM(0.6) for t in vbus)
    locked = [t for t in result.final_board.tracks if t.net_name == "LOCKED"]
    assert len(locked) == 1 and locked[0].locked and locked[0] in wb.source.tracks


def test_accept_batch_is_rule_valid_and_undoable(dense_result: tuple[WorkingBoard, object]) -> None:
    wb0, result = dense_result
    wb = working("router_dense.kicad_pcb")
    assert wb.board.fingerprint == result.base_board.fingerprint
    tracks, vias, removed = result.objects_for(None)
    commit = wb.commit_objects(tracks, vias, removed, "Route board")  # validated
    drc = wb.engine.run_drc()
    assert not drc.errors, [v.message for v in drc.errors][:5]
    for net in result.outcomes:
        assert net_connectivity(wb.engine.geometry, net).status is NetStatus.FULLY_CONNECTED
    wb.undo()
    assert wb.board is wb.source and commit.before is wb.source
    # granular: accept only the diff pair
    t2, v2, r2 = result.objects_for({"USB_P", "USB_N"})
    wb.commit_objects(t2, v2, r2, "Route USB pair")
    assert net_connectivity(wb.engine.geometry, "S0").status is NetStatus.UNROUTED
    _ = wb0


def test_failed_net_does_not_stop_the_job_and_ripup_recovers() -> None:
    wb = working("router_ripup.kicad_pcb")
    s = BoardRouterSettings(priorities={"X": 10}, allow_ripup=False)
    plan = make_plan(wb, s)
    plan.tasks = [replace(t, request=RouteRequest(t.net, candidates=1, max_vias=0))
                  if t.net == "Y" else t for t in plan.tasks]  # fmt: skip
    no_ripup = BoardRouter(wb, s).run(plan)
    assert no_ripup.status is BoardStatus.PARTIALLY_ROUTED
    assert no_ripup.outcomes["Y"].status is not RouteStatus.SUCCESS
    assert no_ripup.outcomes["Y"].reason is not None  # explained, not just "failed"
    assert no_ripup.outcomes["X"].status is RouteStatus.SUCCESS
    s2 = replace(s, allow_ripup=True)
    with_ripup = BoardRouter(wb, s2).run(plan)
    assert with_ripup.status is BoardStatus.FULLY_ROUTED
    assert with_ripup.metrics.ripups == 1
    x_vias = [v for v in with_ripup.added_vias if v.net_name == "X"]
    assert len(x_vias) == 2  # X was rerouted under the wall on B.Cu


def test_locked_and_source_copper_is_never_ripped_up() -> None:
    wb = working("router_ripup.kicad_pcb")
    x = Router(wb.engine).route_net(RouteRequest("X", candidates=1))
    commit = wb.commit_proposals([x.best.proposal], "X", Provenance.ROUTER_GENERATED)
    for t in commit.added_tracks:
        wb.locks.add(t.id)
    s = BoardRouterSettings()
    plan = make_plan(wb, s)
    plan.tasks = [
        replace(t, request=RouteRequest(t.net, candidates=1, max_vias=0)) for t in plan.tasks
    ]
    res = BoardRouter(wb, s).run(plan)
    assert res.outcomes["Y"].status is not RouteStatus.SUCCESS
    assert res.metrics.ripups == 0 and not res.removed_ids
    with pytest.raises(CommitError):
        wb.commit_objects((), (), [commit.added_tracks[0].id], "rip locked", validate=False)
    src_track = next(iter(working("router_basic.kicad_pcb").source.tracks))
    wb2 = working("router_basic.kicad_pcb")
    with pytest.raises(CommitError):
        wb2.commit_objects((), (), [src_track.id], "rip source", validate=False)


def test_pause_resume_cancel() -> None:
    wb = working("router_dense.kicad_pcb")
    control = BoardRoutingControl()
    control.cancel()
    res = BoardRouter(wb, BoardRouterSettings()).run(control=control)
    assert res.status is BoardStatus.CANCELLED and not res.added_tracks
    control2 = BoardRoutingControl()
    control2.pause()
    assert control2.paused
    control2.resume()
    assert control2.checkpoint()


def test_optimisation_improves_selected_metric_or_keeps_route() -> None:
    wb = working("router_basic.kicad_pcb")
    # a deliberately poor route for A: force vias by minimising nothing and
    # accepting the third alternative
    res = Router(wb.engine).route_net(RouteRequest("B", candidates=3))
    worst = max(res.candidates, key=lambda c: c.score.length_nm)
    wb.commit_proposals([worst.proposal], "B (long alternative)", Provenance.ROUTER_GENERATED)
    before = net_metrics(wb, "B")
    report = optimize_nets(wb, ["B"], OptimizeGoal.SHORTER)
    after = net_metrics(wb, "B")
    if report.improved:
        assert after.length_nm < before.length_nm
    else:
        assert after == before  # rolled back exactly
    assert net_connectivity(wb.engine.geometry, "B").status is NetStatus.FULLY_CONNECTED
    assert not wb.engine.run_drc().errors
    # source copper is never optimised
    rep2 = optimize_nets(wb, ["G"], OptimizeGoal.FEWER_VIAS)
    assert "G" in rep2.skipped


def test_via_reduction_pass() -> None:
    wb = working("router_basic.kicad_pcb")
    res = Router(wb.engine).route_net(RouteRequest("C", candidates=1, allowed_layers=("B.Cu",)))
    wb.commit_proposals([res.best.proposal], "C on B.Cu", Provenance.ROUTER_GENERATED)
    report = optimize_nets(wb, ["C"], OptimizeGoal.MERGE_COLLINEAR)
    assert "C" in report.improved or "C" in report.unchanged
    assert not wb.engine.run_drc().errors


def test_diff_pair_metrics(dense_result: tuple[WorkingBoard, object]) -> None:
    _wb, result = dense_result
    m = pair_metrics(result.final_board, "USB_P", "USB_N")
    assert m.impedance == IMPEDANCE == "UNKNOWN"
    assert m.length_p_nm > 0 and m.length_n_nm > 0
    assert m.skew_nm >= 0 and m.via_difference >= 0
    assert m.min_gap_nm is not None and m.min_gap_nm >= MM(0.2) - 1  # clearance kept


def test_source_unchanged_by_board_routing(tmp_path: Path) -> None:
    src = BOARDS / "router_dense.kicad_pcb"
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    wb = working("router_dense.kicad_pcb")
    s = BoardRouterSettings(max_passes=1)
    plan = make_plan(wb, s, nets=["S3", "S4"])
    BoardRouter(wb, s).run(plan)
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before
