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
    monkeypatch.setattr(parallel, "PARALLEL_MIN_SPEEDUP", 0.0)


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


def test_repeated_parallel_runs_are_complete_and_valid(small_minimum: None) -> None:
    """Commit order follows helper completion, so geometry may differ run to run
    (Single worker is the deterministic mode) — but every run must be complete and
    pass the internal geometry check."""
    for _ in range(2):
        wb = working()
        res = BoardRouter(wb, BoardRouterSettings(parallel_workers=2)).run()
        assert res.status is BoardStatus.FULLY_ROUTED
        tracks, vias, removed = res.objects_for(None)
        wb.commit_objects(tracks, vias, removed, "parallel", Provenance.ROUTER_GENERATED)
        assert not wb.engine.run_drc().errors


def test_single_worker_is_deterministic() -> None:
    def signature() -> object:
        res = BoardRouter(working(), BoardRouterSettings(parallel_workers=0)).run()
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


def test_crowded_board_routes_on_one_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nets that all overlap cannot run side by side: no helpers are started."""
    monkeypatch.setattr(parallel, "PARALLEL_MIN_TASKS", 2)
    monkeypatch.setattr(parallel, "PARALLEL_MIN_SPEEDUP", 99.0)

    def must_not_start(*_a: object, **_k: object) -> object:
        raise AssertionError("helpers started on a crowded board")

    monkeypatch.setattr(parallel, "ParallelRouter", must_not_start)
    res = BoardRouter(working(), BoardRouterSettings(parallel_workers=2)).run()
    assert res.status is BoardStatus.FULLY_ROUTED
    assert any("parallel routing skipped" in line for line in res.log)


def test_estimate_speedup_packs_independent_nets() -> None:
    from pcbrouter.domain.geometry import BoundingBox
    from pcbrouter.routing.board_router import RouteTask

    mm = 1_000_000
    spread = {f"N{i}": BoundingBox(i * 20 * mm, 0, i * 20 * mm + mm, mm) for i in range(8)}
    crowded = {f"N{i}": BoundingBox(0, 0, 10 * mm, 10 * mm) for i in range(8)}
    tasks = [RouteTask(f"N{i}") for i in range(8)]
    assert parallel.estimate_speedup(tasks, spread, 4) == 4.0
    assert parallel.estimate_speedup(tasks, crowded, 4) == 1.0


def test_slow_helper_start_never_blows_the_job_deadline(
    small_minimum: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Helpers that never start used to hold the job for START_TIMEOUT_S (120 s)
    whatever the budget: a 60 s Real100 job took 208 s and routed nothing. Start-up
    now gets a share of the remaining budget, then routing goes on sequentially."""
    import time

    seen: list[float] = []

    def never_starts(_fork: object, _workers: int, start_timeout_s: float) -> object:
        seen.append(start_timeout_s)
        time.sleep(start_timeout_s)
        raise RuntimeError(f"route helpers did not start within {start_timeout_s:.0f} s")

    monkeypatch.setattr(parallel, "ParallelRouter", never_starts)
    budget = 24.0
    t0 = time.perf_counter()
    res = BoardRouter(working(), BoardRouterSettings(parallel_workers=2, budget_s=budget)).run()
    wall = time.perf_counter() - t0
    assert seen and seen[0] <= max(parallel.START_MIN_S, parallel.START_BUDGET_SHARE * budget)
    assert wall < budget + 3.0, f"{wall:.1f} s for a {budget:.0f} s budget"
    assert res.metrics.nets_completed > 0  # the rest of the budget still routes


@pytest.mark.parametrize("budget", [0.5, 2.0])
def test_sequential_routing_meets_tight_deadlines(budget: float) -> None:
    import time

    t0 = time.perf_counter()
    BoardRouter(working(), BoardRouterSettings(budget_s=budget)).run()
    assert time.perf_counter() - t0 < budget + 2.0
