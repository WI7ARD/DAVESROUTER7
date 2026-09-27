"""Live board-routing snapshots: the worker streams what is routed so far."""

from __future__ import annotations

import itertools
import pickle
from pathlib import Path

from pcbrouter.jobs.protocol import BoardPartial, JobPartial
from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.routing.board_router import BoardRouter, BoardRouterSettings, make_plan
from pcbrouter.routing.request import RouteRequest
from pcbrouter.routing.router import Router
from pcbrouter.routing.working_board import WorkingBoard

BOARDS = Path(__file__).parent.parent / "fixtures" / "boards"


def working(name: str) -> WorkingBoard:
    path = BOARDS / name
    return WorkingBoard(load_board(path).board, load_project_rules(path))


def test_partial_snapshots_stream_routed_copper() -> None:
    wb = working("router_basic.kicad_pcb")
    seen: list[BoardPartial] = []
    settings = BoardRouterSettings()
    plan = make_plan(wb, settings)
    assert plan.tasks, "fixture should have nets to route"
    result = BoardRouter(wb, settings).run(plan, on_partial=seen.append)
    assert seen, "no partial snapshot was emitted"
    assert seen[0].total_nets == len(plan.tasks)
    copper = [len(p.added_tracks) + len(p.added_vias) for p in seen]
    assert copper[-1] == len(result.added_tracks) + len(result.added_vias)
    assert all(b >= a for a, b in itertools.pairwise(copper)), "snapshots must only grow"
    assert seen[-1].succeeded_nets == result.metrics.nets_completed
    assert seen[-1].completed_nets >= seen[-1].succeeded_nets


def test_phase_timings_split_elapsed() -> None:
    """R1: grid/search/geometry/validate phases are timed and add up."""
    wb = working("router_basic.kicad_pcb")
    result = Router(wb.engine).route_net(RouteRequest("A", candidates=1))
    assert result.best is not None
    m = result.metrics
    phases = m.grid_s + m.search_s + m.geometry_s + m.validate_s
    assert phases > 0
    assert phases <= m.elapsed_s + 0.05  # timers nest inside the total
    assert m.search_s > 0 and m.grid_s >= 0


def test_board_metrics_absorb_phases() -> None:
    wb = working("router_basic.kicad_pcb")
    result = BoardRouter(wb, BoardRouterSettings()).run()
    m = result.metrics
    assert m.grid_s + m.search_s + m.geometry_s + m.validate_s > 0
    assert m.grid_s + m.search_s + m.geometry_s + m.validate_s <= m.runtime_s + 1.0
    for key in ("grid_s", "search_s", "geometry_s", "validate_s"):
        assert key in m.to_dict()


def test_partial_snapshot_pickles_for_the_worker_boundary() -> None:
    wb = working("router_basic.kicad_pcb")
    seen: list[BoardPartial] = []
    BoardRouter(wb, BoardRouterSettings()).run(on_partial=seen.append)
    assert seen, "no partial snapshot was emitted"
    partial = max(seen, key=lambda p: len(p.added_tracks) + len(p.added_vias))
    clone = pickle.loads(pickle.dumps(JobPartial(4242, partial)))
    assert clone.job_id == 4242
    assert clone.partial.total_nets == partial.total_nets
    assert [t.id for t in clone.partial.added_tracks] == [t.id for t in partial.added_tracks]
    assert [v.id for v in clone.partial.added_vias] == [v.id for v in partial.added_vias]
    assert clone.partial.outcomes == partial.outcomes
