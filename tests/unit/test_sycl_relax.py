"""Integer relaxation search (routing/search/relax.py) and its fused SYCL kernel
(compute/sycl_relax.py): CPU-vs-device differential tests.

* the NumPy reference is exactly optimal (independent integer Dijkstra) on random
  obstacle grids and on real router grids (vias, wrong-way costs, penalties);
* its paths are legal and realise the reported cost;
* routes found with it pass the router's exact validator;
* the SYCL kernel gives a bit-identical distance field (runs wherever a SYCL
  device exists: an Intel GPU, or the OpenCL CPU device from ``intel-opencl-rt``).
"""

from __future__ import annotations

import heapq
import importlib.util
import time
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.routing.request import RouteRequest
from pcbrouter.routing.result import RouteStatus
from pcbrouter.routing.router import Router
from pcbrouter.routing.search import relax
from pcbrouter.routing.search.astar import DIRS, SearchStatus, search
from pcbrouter.routing.working_board import WorkingBoard

BOARDS = Path(__file__).parent.parent / "fixtures" / "boards"


def dijkstra(g: relax.RelaxGrid, dist0: np.ndarray, targets: np.ndarray) -> int | None:
    """Independent integer Dijkstra over the same states, moves and costs."""
    nl, ny, nx = g.shape
    best: dict[tuple[int, int, int], int] = {}
    heap: list[tuple[int, int, int, int]] = []
    for li, r, c in zip(*np.nonzero(dist0 == 0), strict=True):
        if g.passable[li, r, c]:
            best[(li, r, c)] = 0
            heap.append((0, int(li), int(r), int(c)))
    heapq.heapify(heap)
    while heap:
        d, li, r, c = heapq.heappop(heap)
        if d > best.get((li, r, c), 1 << 62):
            continue
        if targets[li, r, c]:
            return d
        moves = []
        for nd in g.moves:
            dx, dy = DIRS[nd]
            rr, cc = r + dy, c + dx
            if not (0 <= rr < ny and 0 <= cc < nx) or not g.passable[li, rr, cc]:
                continue
            if dx and dy and not (g.passable[li, r, cc] and g.passable[li, rr, c]):
                continue
            step = (int(g.step[li, nd]) * int(g.mult[li, rr, cc]) + relax.MQ // 2) // relax.MQ
            moves.append((li, rr, cc, step))
        if g.via_cost and g.via_ok[r, c]:
            moves += [(l2, r, c, g.via_cost) for l2 in range(nl)
                      if l2 != li and g.passable[l2, r, c]]  # fmt: skip
        for l2, rr, cc, w in moves:
            nd2 = d + w
            if nd2 < best.get((l2, rr, cc), 1 << 62):
                best[(l2, rr, cc)] = nd2
                heapq.heappush(heap, (nd2, l2, rr, cc))
    return None


def random_case(seed: int, nl: int = 2, ny: int = 30, nx: int = 40) -> tuple[Any, Any, Any]:
    rng = np.random.default_rng(seed)
    passable = (rng.random((nl, ny, nx)) > 0.3).astype(np.uint8)
    mult = rng.integers(relax.MQ, 4 * relax.MQ, size=(nl, ny, nx)).astype(np.int32)
    step = np.array([[64, 91, 64, 91, 64, 91, 64, 91]] * nl, dtype=np.int32)
    step[0, [2, 6]] = 192  # wrong way on layer 0 (vertical moves)
    step[-1, [0, 4]] = 192  # and horizontal on the last layer
    via_ok = (rng.random((ny, nx)) > 0.4).astype(np.uint8)
    g = relax.RelaxGrid(passable, mult, step, via_ok, 320, tuple(range(8)), 100_000.0)
    passable[0, 2, 2] = passable[nl - 1, ny - 3, nx - 3] = 1
    dist0 = np.full((nl, ny, nx), relax.INF, dtype=np.int32)
    dist0[0, 2, 2] = 0
    targets = np.zeros((nl, ny, nx), dtype=bool)
    targets[nl - 1, ny - 3, nx - 3] = True
    return g, dist0, targets


def path_cost(g: relax.RelaxGrid, path: list[tuple[int, int]]) -> int:
    """Re-cost a path with the model, checking every move is legal."""
    _nl, _ny, nx = g.shape
    total = 0
    for (l1, i1), (l2, i2) in pairwise(path):
        r1, c1 = divmod(i1, nx)
        r2, c2 = divmod(i2, nx)
        assert g.passable[l2, r2, c2], "path enters a blocked cell"
        if l1 != l2:
            assert i1 == i2 and g.via_ok[r2, c2], "via where no via fits"
            total += g.via_cost
            continue
        dx, dy = c2 - c1, r2 - r1
        nd = DIRS.index((dx, dy))
        assert nd in g.moves
        if dx and dy:
            assert g.passable[l1, r1, c2] and g.passable[l1, r2, c1], "corner cut"
        total += relax.move_cost(g, l1, nd, int(g.mult[l2, r2, c2]))
    return total


@pytest.mark.parametrize("seed", range(6))
def test_reference_is_exactly_optimal_on_random_grids(seed: int) -> None:
    g, dist0, targets = random_case(seed)
    status, dist, _ = relax.solve(np, g, dist0, targets, deadline=time.perf_counter() + 60)
    expected = dijkstra(g, dist0, targets)
    if expected is None:
        assert status is SearchStatus.NO_PATH
        return
    assert status is SearchStatus.FOUND
    got = int(np.where(targets, dist, relax.INF).min())
    assert got == expected
    path = relax.backtrack(g, dist, targets)
    assert path is not None and path[0] == (0, 2 * 40 + 2)
    assert path_cost(g, path) == expected


def working(name: str) -> WorkingBoard:
    path = BOARDS / name
    return WorkingBoard(load_board(path).board, load_project_rules(path))


def recorded_problems(name: str, net: str) -> list[Any]:
    """The SearchProblems the router builds for a real net (frozen copies)."""
    wb = working(name)
    out: list[Any] = []

    def spy(problem: Any, **kw: Any) -> Any:
        g = problem.grid
        out.append(replace(problem, grid=replace(
            g, passable=[p.copy() for p in g.passable], near=[p.copy() for p in g.near],
            penalty=[None if p is None else p.copy() for p in g.penalty],
            factor=[None if p is None else p.copy() for p in g.factor],
            via_ok=None if g.via_ok is None else g.via_ok.copy())))  # fmt: skip
        return search(problem, **kw)

    Router(wb.engine, search_fn=spy).route_net(RouteRequest(net, candidates=1))
    return out


@pytest.mark.parametrize(("board", "net"), [("router_dense.kicad_pcb", "S3"),
                                            ("router_4layer.kicad_pcb", None)])  # fmt: skip
def test_reference_is_optimal_on_real_router_grids(board: str, net: str | None) -> None:
    if net is None:
        net = sorted(n.name for n in working(board).board.nets if n.name)[0]
    problems = recorded_problems(board, net)
    assert problems
    for problem in problems[:2]:
        g = relax.relax_grid(problem)
        targets = np.stack([t & p for t, p in zip(problem.targets, problem.grid.passable,
                                                   strict=True)])  # fmt: skip
        dist0 = relax.seed(g, problem.sources)
        status, dist, _ = relax.solve(np, g, dist0, targets, deadline=time.perf_counter() + 120)
        expected = dijkstra(g, dist0, targets)
        assert status is SearchStatus.FOUND and expected is not None
        assert int(np.where(targets, dist, relax.INF).min()) == expected
        path = relax.backtrack(g, dist, targets)
        assert path is not None and path_cost(g, path) == expected


@pytest.mark.parametrize("net", ["A", "B", "C", "E"])
def test_relax_routes_pass_the_exact_validator(net: str) -> None:
    wb = working("router_basic.kicad_pcb")
    res = Router(wb.engine, search_fn=relax.relax_search).route_net(RouteRequest(net, candidates=1))
    assert res.status is RouteStatus.SUCCESS, res.summary()
    assert res.best is not None and wb.engine.validator.validate_route(res.best.proposal).legal


def _sycl_queue() -> Any:
    if importlib.util.find_spec("dpctl") is None:
        pytest.skip("dpctl (Intel SYCL) is not installed")
    import dpctl

    for flt in ("level_zero:gpu", "opencl:gpu", "opencl:cpu"):
        try:
            return dpctl.SyclQueue(flt)
        except Exception:
            continue
    pytest.skip("no SYCL device (GPU or OpenCL CPU runtime)")


@pytest.mark.parametrize("seed", range(4))
def test_sycl_kernel_is_bit_identical_to_numpy(seed: int) -> None:
    from pcbrouter.compute.sycl_relax import SyclRelax

    q = _sycl_queue()
    eng = SyclRelax(q.sycl_device)
    for nl in (1, 2, 4):
        g, dist0, targets = random_case(seed, nl=nl)
        far = time.perf_counter() + 60
        ref = relax.solve(np, g, dist0, targets, deadline=far)
        dev = eng.solve(g, dist0, targets, deadline=far)
        assert ref[0] is dev[0] and ref[2] == dev[2]
        assert np.array_equal(ref[1], dev[1])


def test_sycl_kernel_on_a_real_router_grid() -> None:
    from pcbrouter.compute.sycl_relax import SyclRelax

    q = _sycl_queue()
    problem = recorded_problems("router_dense.kicad_pcb", "S3")[0]
    g = relax.relax_grid(problem)
    targets = np.stack([t & p for t, p in zip(problem.targets, problem.grid.passable,
                                               strict=True)])  # fmt: skip
    dist0 = relax.seed(g, problem.sources)
    far = time.perf_counter() + 120
    ref = relax.solve(np, g, dist0, targets, deadline=far)
    dev = SyclRelax(q.sycl_device).solve(g, dist0, targets, deadline=far)
    assert ref[0] is SearchStatus.FOUND and dev[0] is SearchStatus.FOUND
    assert np.array_equal(ref[1], dev[1])


def test_diagnostic_stages_on_a_sycl_device() -> None:
    from pcbrouter.compute.sycl_check import run_diagnostic

    q = _sycl_queue()
    diag = run_diagnostic(q.sycl_device.filter_string, bench=False).to_dict()
    names = [s["name"] for s in diag["stages"]]
    assert names[:9] == ["dpctl_import", "dpnp_import", "devices", "gpu_selected", "queue",
                         "allocation", "operation", "verified", "fused_kernel"]  # fmt: skip
    assert all(s["ok"] for s in diag["stages"]), diag
    assert diag["compute_verified"]
