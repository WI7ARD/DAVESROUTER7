"""Parallel board routing (routing/parallel.py): helper processes propose, the
master commits in plan order through the exact validator."""

from __future__ import annotations

from pathlib import Path

import pytest

from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.routing import parallel
from pcbrouter.routing.board_router import (
    BoardRouter,
    BoardRouterSettings,
    BoardRoutingControl,
    BoardStatus,
)
from pcbrouter.routing.working_board import Provenance, WorkingBoard

BOARDS = Path(__file__).parent.parent / "fixtures" / "boards"


def working(name: str = "router_dense.kicad_pcb") -> WorkingBoard:
    path = BOARDS / name
    return WorkingBoard(load_board(path).board, load_project_rules(path))


@pytest.fixture
def small_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parallel, "PARALLEL_MIN_TASKS", 2)


def test_parallel_matches_sequential_and_is_drc_clean(small_minimum: None) -> None:
    seq = BoardRouter(working(), BoardRouterSettings()).run()
    wb = working()
    par = BoardRouter(wb, BoardRouterSettings(parallel_workers=2)).run()
    assert par.metrics.parallel_batches > 0, par.log
    assert any("parallel routing: 2 helper" in line for line in par.log)
    assert par.status is BoardStatus.FULLY_ROUTED and seq.status is BoardStatus.FULLY_ROUTED
    assert par.metrics.nets_completed == seq.metrics.nets_completed
    tracks, vias, removed = par.objects_for(None)
    wb.commit_objects(tracks, vias, removed, "parallel", Provenance.ROUTER_GENERATED)
    assert not wb.engine.run_drc().errors


def test_parallel_is_deterministic(small_minimum: None) -> None:
    def signature() -> object:
        res = BoardRouter(working(), BoardRouterSettings(parallel_workers=2)).run()
        return sorted((t.layer, t.start, t.end) for t in res.added_tracks)

    assert signature() == signature()


def test_helper_start_failure_falls_back_to_sequential(
    small_minimum: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_a: object, **_k: object) -> object:
        raise RuntimeError("route helpers did not start")

    monkeypatch.setattr(parallel, "ParallelRouter", broken)
    res = BoardRouter(working(), BoardRouterSettings(parallel_workers=2)).run()
    assert res.status is BoardStatus.FULLY_ROUTED
    assert any("parallel routing unavailable" in line for line in res.log)
    assert res.metrics.parallel_batches == 0


def test_cancel_stops_parallel_routing(small_minimum: None) -> None:
    control = BoardRoutingControl()
    control.cancel()
    res = BoardRouter(working(), BoardRouterSettings(parallel_workers=2)).run(control=control)
    assert res.status is BoardStatus.CANCELLED and not res.added_tracks


def test_batches_never_mix_overlapping_regions() -> None:
    from pcbrouter.domain.geometry import BoundingBox
    from pcbrouter.routing.board_router import RouteTask

    mm = 1_000_000
    regions = {
        "A": BoundingBox(0, 0, 10 * mm, 10 * mm),
        "B": BoundingBox(5 * mm, 5 * mm, 15 * mm, 15 * mm),  # overlaps A
        "C": BoundingBox(50 * mm, 0, 60 * mm, 10 * mm),
        "D": BoundingBox(11 * mm, 0, 20 * mm, 3 * mm),  # within the 2 mm margin of A
    }
    tasks = [RouteTask(n) for n in "ABCD"]
    batch = parallel.pick_batch(tasks, regions, 3)
    assert [t.net for t in batch] == ["A", "C"]
    assert parallel.auto_workers(-1) <= parallel.MAX_WORKERS
