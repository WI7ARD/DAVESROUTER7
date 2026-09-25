"""Array-parallel wavefront search (Stage 6): the GPU-friendly search algorithm.

Why not A* on the GPU: A* is a sequential priority-queue algorithm; it maps poorly
to thousands of GPU threads. A *wavefront* (parallel Bellman-Ford / Lee
propagation) instead relaxes every cell at once::

    repeat:  dist = min(dist, shift_d(dist) + step_cost_d)  for the 8 directions
             dist[l] = min(dist[l], min_other(dist) + via_cost)  where a via fits
    until no cell that improved can still beat the best target distance

Every operation is an element-wise array op, written against an array module
``xp`` (NumPy on the CPU, CuPy on an NVIDIA GPU — same code). The path is then
extracted by walking downhill from the best target cell.

Exactness of the stopping rule: step costs are positive, so once every cell that
changed in an iteration has a distance >= the best target distance, no later
iteration can improve the target.

Differences from the CPU A* (documented, tested): no direction in the state, so
bend penalties are not modelled (the path simplifier and the exact validator
still apply) and via *limits* are not supported — requests with ``max_vias`` use
the CPU A*. Both searches feed the same geometry pipeline and final validator:
routes may differ, hard rules are identical.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any

import numpy as np

from pcbrouter.routing.search.astar import SQRT2, SearchOutcome, SearchProblem, SearchStatus

INF = 3.0e38  # plain Python float: a portable scalar for every array library
_CHECK_EVERY = 1  # every sweep: one check is negligible next to a full-grid relaxation


def _shift(xp: Any, a: Any, dy: int, dx: int, fill: float) -> Any:
    """out[r, c] = a[r - dy, c - dx] (values arriving from the (-dy, -dx) neighbour)."""
    out = xp.full(a.shape, fill, dtype=a.dtype)
    h, w = a.shape
    rs_dst = slice(max(dy, 0), h + min(dy, 0))
    rs_src = slice(max(-dy, 0), h + min(-dy, 0))
    cs_dst = slice(max(dx, 0), w + min(dx, 0))
    cs_src = slice(max(-dx, 0), w + min(-dx, 0))
    out[rs_dst, cs_dst] = a[rs_src, cs_src]
    return out


def wavefront_search(
    problem: SearchProblem,
    *,
    node_limit: int,
    time_limit_s: float,
    cancel: threading.Event | None = None,
    record_explored: bool = False,
    xp: Any = np,
) -> SearchOutcome:
    t0 = time.perf_counter()
    g = problem.grid
    nl = len(g.layers)
    ny, nx = g.ny, g.nx
    cell = float(g.spec.cell)
    cm = problem.cost
    if problem.max_vias is not None and problem.vias_enabled:
        raise ValueError("wavefront search does not support via limits (use the CPU A*)")

    # per-layer step cost field: cost of *entering* a cell (orthogonal step length)
    passable = [xp.asarray(p) for p in g.passable]
    step = []
    for li in range(nl):
        f = np.full((ny, nx), problem.layer_factor[li], dtype=np.float32)
        fac, pen = g.factor[li], g.penalty[li]
        if fac is not None:
            f *= fac.astype(np.float32)
        f *= 1.0 + cm.proximity_factor * g.near[li].astype(np.float32)
        if pen is not None:
            f += pen.astype(np.float32)
        step.append(xp.asarray(f * np.float32(cell)))
    via_ok = (
        xp.asarray(g.via_ok) if (problem.vias_enabled and g.via_ok is not None and nl > 1) else None
    )
    targets = [xp.asarray(t & p) for t, p in zip(problem.targets, g.passable, strict=True)]
    # initial distances are built on the host (portable across NumPy/CuPy/dpnp)
    dist = []
    for li, cells in enumerate(problem.sources):
        d0 = np.full((ny, nx), INF, dtype=np.float32)
        if cells.size:
            d0.reshape(-1)[cells] = 0.0
        d0[~g.passable[li]] = INF
        dist.append(xp.asarray(d0))

    dirs = [(0, 1), (0, -1), (1, 0), (-1, 0)]
    if problem.octilinear:
        dirs += [(1, 1), (1, -1), (-1, 1), (-1, -1)]
    # a diagonal move needs both orthogonal neighbours passable (no corner cutting)
    diag_ok = {}
    for li in range(nl):
        for dy, dx in dirs:
            if dy and dx:
                diag_ok[(li, dy, dx)] = _shift(xp, passable[li], dy, 0, False) & _shift(
                    xp, passable[li], 0, dx, False
                )
    via_cost = float(problem.via_cost)
    iterations = 0
    status = SearchStatus.NO_PATH
    deadline = t0 + time_limit_s
    best_target = INF
    while True:
        iterations += 1
        changed_any = False
        min_changed = INF
        new_dist = []
        for li in range(nl):
            d = dist[li]
            cand = d
            for dy, dx in dirs:
                mult = SQRT2 if (dy and dx) else 1.0
                arrived = _shift(xp, d, dy, dx, INF) + step[li] * mult
                if dy and dx:
                    arrived = xp.where(diag_ok[(li, dy, dx)], arrived, INF)
                cand = xp.minimum(cand, arrived)
            new_dist.append(xp.where(passable[li], cand, INF))
        if via_ok is not None:
            best = new_dist[0]
            for li in range(1, nl):
                best = xp.minimum(best, new_dist[li])
            for li in range(nl):
                viad = xp.where(via_ok & passable[li], best + via_cost, INF)
                new_dist[li] = xp.minimum(new_dist[li], viad)
        for li in range(nl):
            improved = new_dist[li] < dist[li]
            if bool(improved.any()):
                changed_any = True
                min_changed = min(min_changed, float(new_dist[li][improved].min()))
            dist[li] = new_dist[li]
            t = targets[li]
            if bool(t.any()):
                best_target = min(best_target, float(dist[li][t].min()))
        if not changed_any or (best_target < INF and min_changed >= best_target):
            status = SearchStatus.FOUND if best_target < INF else SearchStatus.NO_PATH
            break
        if iterations >= n_cells(ny, nx, nl):  # hard bound: no path is longer
            status = SearchStatus.NODE_LIMIT
            break
        if iterations % _CHECK_EVERY == 0:
            if time.perf_counter() > deadline:
                status = SearchStatus.TIMEOUT
                break
            if cancel is not None and cancel.is_set():
                status = SearchStatus.CANCELLED
                break
    out = SearchOutcome(
        status, expanded=iterations * n_cells(ny, nx, nl), elapsed_s=time.perf_counter() - t0
    )
    if status is not SearchStatus.FOUND:
        return out
    host = [np.asarray(_to_host(xp, d)) for d in dist]
    step_h = [np.asarray(_to_host(xp, s)) for s in step]
    pass_h = [np.asarray(_to_host(xp, p)) for p in passable]
    via_h = None if via_ok is None else np.asarray(_to_host(xp, via_ok))
    tgt_h = [np.asarray(_to_host(xp, t)) for t in targets]
    out.path = _backtrack(host, step_h, pass_h, via_h, tgt_h, dirs, float(via_cost))
    out.cost = best_target
    out.vias = sum(1 for a, b in zip(out.path, out.path[1:], strict=False) if a[0] != b[0])
    return out


def n_cells(ny: int, nx: int, nl: int) -> int:
    return ny * nx * nl


def _to_host(xp: Any, a: Any) -> Any:
    """Device array -> NumPy (CuPy: ``.get()``; dpnp: ``asnumpy``)."""
    if xp is np:
        return a
    if hasattr(xp, "asnumpy"):
        return xp.asnumpy(a)
    return a.get() if hasattr(a, "get") else a


def _backtrack(
    dist: list[np.ndarray],
    step: list[np.ndarray],
    passable: list[np.ndarray],
    via_ok: np.ndarray | None,
    targets: list[np.ndarray],
    dirs: list[tuple[int, int]],
    via_cost: float,
) -> list[tuple[int, int]]:
    ny, nx = dist[0].shape
    best = (math.inf, 0, 0, 0)
    for li, t in enumerate(targets):
        if t.any():
            masked = np.where(t, dist[li], np.inf)
            idx = int(np.argmin(masked))
            v = float(masked.reshape(-1)[idx])
            if v < best[0]:
                best = (v, li, idx // nx, idx % nx)
    _, li, r, c = best
    path = [(li, r * nx + c)]
    tol = 2.0  # nm; float32 distances
    for _ in range(ny * nx * len(dist)):
        d = float(dist[li][r, c])
        if d <= 0:
            break
        moved = False
        for dy, dx in dirs:
            pr, pc = r - dy, c - dx
            if not (0 <= pr < ny and 0 <= pc < nx) or not passable[li][pr, pc]:
                continue
            if dy and dx and not (passable[li][r, pc] and passable[li][pr, c]):
                continue
            cost = float(step[li][r, c]) * (SQRT2 if dy and dx else 1.0)
            if abs(float(dist[li][pr, pc]) + cost - d) <= max(tol, d * 1e-6):
                r, c = pr, pc
                moved = True
                break
        if not moved and via_ok is not None and via_ok[r, c]:
            for l2 in range(len(dist)):
                if l2 != li and abs(float(dist[l2][r, c]) + via_cost - d) <= max(tol, d * 1e-6):
                    li = l2
                    moved = True
                    break
        if not moved:
            # float32 rounding: take the steepest-descent neighbour
            best_n = None
            for dy, dx in dirs:
                pr, pc = r - dy, c - dx
                if (
                    0 <= pr < ny
                    and 0 <= pc < nx
                    and dist[li][pr, pc] < d
                    and (best_n is None or dist[li][pr, pc] < dist[li][best_n[0], best_n[1]])
                ):
                    best_n = (pr, pc)
            if best_n is None:
                break
            r, c = best_n
        path.append((li, r * nx + c))
    path.reverse()
    return path
