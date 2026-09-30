"""Integer grid relaxation: the GPU-friendly shortest-path search (reference).

The OrthoRoute idea: the GPU does not route many nets at once; it accelerates the
expensive part *inside* one search. Every sweep relaxes all (layer, cell) states in
parallel (synchronous Bellman-Ford)::

    new[l, c] = min(old[l, c],  min over the 8 moves  old[l, c - d] + cost(l, c, d))
    new[l, c] = min(new[l, c],  min over layers l' of new[l', c] + via)   (via_ok[c])

``xp`` is NumPy here (the CPU reference) or dpnp; ``compute/sycl_relax.py`` runs
the same sweep as one fused SYCL kernel. Costs are **int32** in 1/``UNIT`` of a
grid step, so every implementation computes bit-identical distance fields and the
path is recovered with exact equality (no float tolerance). The cost model is the
A*'s (``astar._search``): move length x preferred-direction (wrong-way) factor x
(layer factor x corridor factor x (1 + proximity x near) + penalty), entering-cell
convention, no corner cutting, vias where ``via_ok`` — without bend costs (the state
has no direction). Legality never depends on arithmetic: ``passable`` / ``via_ok``
are the exact occupancy masks, and every path still goes through the router's
exact validator like an A* path.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

import numpy as np
import numpy.typing as npt

from pcbrouter.routing.search.astar import DIRS, SQRT2, SearchOutcome, SearchProblem, SearchStatus

UNIT = 64  # cost units per orthogonal grid step on a factor-1 cell
MQ = 256  # fixed-point scale of the per-cell multiplier
INF = 1 << 30  # "unreached"; INF + any step still fits in int32
#: sweeps between host synchronisations (stop test); extra sweeps never change
#: converged distances, so batching keeps the result exact
SWEEPS_PER_SYNC = 16


@dataclass
class RelaxGrid:
    """Everything a relaxation needs, as integer arrays (host NumPy)."""

    passable: npt.NDArray[np.uint8]  # (L, ny, nx)
    mult: npt.NDArray[np.int32]  # (L, ny, nx) per-cell multiplier x MQ
    step: npt.NDArray[np.int32]  # (L, 8) move length x direction factor x UNIT
    via_ok: npt.NDArray[np.uint8]  # (ny, nx); all zero when vias are off
    via_cost: int
    moves: tuple[int, ...]  # DIRS indices in use (4 or 8)
    cell_nm: float

    @property
    def shape(self) -> tuple[int, int, int]:
        nl, ny, nx = self.passable.shape
        return int(nl), int(ny), int(nx)


def relax_grid(problem: SearchProblem) -> RelaxGrid:
    """Convert a SearchProblem (the A*'s input) into the integer model."""
    g = problem.grid
    nl, ny, nx = len(g.layers), g.ny, g.nx
    cm = problem.cost
    passable = np.stack([p.astype(np.uint8) for p in g.passable])
    mult = np.empty((nl, ny, nx), dtype=np.int32)
    for li in range(nl):
        m = np.full((ny, nx), float(problem.layer_factor[li]), dtype=np.float64)
        fac = g.factor[li]
        if fac is not None:
            m *= fac
        m *= 1.0 + cm.proximity_factor * g.near[li]
        pen = g.penalty[li]
        if pen is not None:
            m += pen
        mult[li] = np.clip(np.rint(m * MQ), 1, 1 << 20).astype(np.int32)
    ww = cm.wrong_way_factor
    step = np.zeros((nl, 8), dtype=np.int32)
    for li in range(nl):
        pref = problem.layer_dirs[li] if li < len(problem.layer_dirs) else 0
        for nd, (dx, dy) in enumerate(DIRS):
            f = SQRT2 if dx and dy else 1.0
            if pref and ww > 1.0:
                if dx and dy:
                    f *= (1.0 + ww) / 2.0
                elif (pref == 1 and dy) or (pref == 2 and dx):
                    f *= ww
            step[li, nd] = round(f * UNIT)
    vias_on = problem.vias_enabled and g.via_ok is not None and nl > 1
    via_ok = (
        g.via_ok.astype(np.uint8) if vias_on and g.via_ok is not None
        else np.zeros((ny, nx), dtype=np.uint8)
    )  # fmt: skip
    cell = float(g.spec.cell)
    via_cost = round(problem.via_cost / cell * UNIT) if vias_on else 0
    moves = tuple(range(8)) if problem.octilinear else (0, 2, 4, 6)
    return RelaxGrid(passable, mult, step, via_ok, max(1, via_cost), moves, cell)


def move_cost(g: RelaxGrid, li: int, nd: int, mult: Any) -> Any:
    """Integer cost of entering cells with multiplier ``mult`` by move ``nd``."""
    return (int(g.step[li, nd]) * mult + MQ // 2) // MQ


def seed(g: RelaxGrid, sources: list[npt.NDArray[np.int64]]) -> npt.NDArray[np.int32]:
    nl, ny, nx = g.shape
    flat = np.full((nl, ny * nx), INF, dtype=np.int32)
    for li, cells in enumerate(sources):
        if cells.size:
            flat[li, cells] = 0
    dist: npt.NDArray[np.int32] = flat.reshape(nl, ny, nx)
    dist[g.passable == 0] = INF
    return dist


def sweep(xp: Any, g: RelaxGrid, dist: Any, dev: dict[str, Any] | None = None) -> Any:
    """One synchronous relaxation sweep (array code for NumPy or dpnp)."""
    dev = dev if dev is not None else _device_arrays(xp, g)
    nl, ny, nx = g.shape
    out = []
    for li in range(nl):
        d = dist[li]
        pad = xp.full((ny + 2, nx + 2), INF, dtype=xp.int32)
        pad[1:-1, 1:-1] = d
        ppad = dev["ppad"][li]
        cand = d
        for nd in g.moves:
            dx, dy = DIRS[nd]
            pred = pad[1 - dy : 1 - dy + ny, 1 - dx : 1 - dx + nx]  # value at c - (dx, dy)
            arrived = xp.minimum(pred + dev["cost"][li][nd], INF)
            if dx and dy:  # no corner cutting: both orthogonal neighbours passable
                ok = (ppad[1 - dy : 1 - dy + ny, 1 : 1 + nx] != 0) & (
                    ppad[1 : 1 + ny, 1 - dx : 1 - dx + nx] != 0
                )
                arrived = xp.where(ok, arrived, INF)
            cand = xp.minimum(cand, arrived)
        out.append(xp.where(dev["pass"][li] != 0, cand, INF))
    new = xp.stack(out)
    if g.via_cost and bool(g.via_ok.any()):
        best = xp.min(new, axis=0)
        via = xp.minimum(best + g.via_cost, INF)
        can = (dev["via_ok"] != 0)[None, :, :] & (dev["pass"] != 0)
        new = xp.where(can, xp.minimum(new, via[None, :, :]), new)
    return new


def _device_arrays(xp: Any, g: RelaxGrid) -> dict[str, Any]:
    nl, ny, nx = g.shape
    ppad = np.zeros((nl, ny + 2, nx + 2), dtype=np.uint8)
    ppad[:, 1:-1, 1:-1] = g.passable
    cost = [
        [xp.asarray(move_cost(g, li, nd, g.mult[li]).astype(np.int32)) for nd in range(8)]
        for li in range(nl)
    ]
    return {
        "pass": xp.asarray(g.passable),
        "ppad": xp.asarray(ppad),
        "via_ok": xp.asarray(g.via_ok),
        "cost": cost,
    }


def solve(
    xp: Any,
    g: RelaxGrid,
    dist0: npt.NDArray[np.int32],
    targets: npt.NDArray[np.bool_],
    *,
    deadline: float,
    cancel: threading.Event | None = None,
    to_host: Any = np.asarray,
) -> tuple[SearchStatus, npt.NDArray[np.int32], int]:
    """Relax until the best target can no longer improve; (status, dist, sweeps).

    Exact stop rule: step costs are positive, so once no cell improved below the
    best target distance in a sweep, no later sweep can lower it."""
    dev = _device_arrays(xp, g)
    dist = xp.asarray(dist0)
    tmask = xp.asarray(targets)
    sweeps = 0
    limit = int(np.prod(g.shape)) + 1
    while True:
        prev = dist
        for _ in range(SWEEPS_PER_SYNC):
            prev = dist
            dist = sweep(xp, g, dist, dev)
            sweeps += 1
        improved = dist < prev
        stats = xp.stack(
            [
                xp.min(xp.where(improved, dist, INF)),
                xp.min(xp.where(tmask, dist, INF)),
            ]
        )
        min_changed, best_t = (int(v) for v in to_host(stats))
        if min_changed >= INF or (best_t < INF and min_changed >= best_t):
            status = SearchStatus.FOUND if best_t < INF else SearchStatus.NO_PATH
            return status, np.asarray(to_host(dist), dtype=np.int32), sweeps
        if sweeps >= limit:
            return SearchStatus.NODE_LIMIT, np.asarray(to_host(dist), dtype=np.int32), sweeps
        if time.perf_counter() > deadline:
            return SearchStatus.TIMEOUT, np.asarray(to_host(dist), dtype=np.int32), sweeps
        if cancel is not None and cancel.is_set():
            return SearchStatus.CANCELLED, np.asarray(to_host(dist), dtype=np.int32), sweeps


def backtrack(
    g: RelaxGrid, dist: npt.NDArray[np.int32], targets: npt.NDArray[np.bool_]
) -> list[tuple[int, int]] | None:
    """Exact predecessor walk from the best target to a source (distance 0).

    Every step satisfies ``dist[prev] + cost(prev -> cur) == dist[cur]`` exactly,
    so the returned path realises the reported cost; None if no chain exists."""
    nl, ny, nx = g.shape
    masked = np.where(targets, dist, INF)
    flat = int(np.argmin(masked))
    if int(masked.reshape(-1)[flat]) >= INF:
        return None
    li, rest = divmod(flat, ny * nx)
    r, c = divmod(rest, nx)
    path = [(li, r * nx + c)]
    for _ in range(nl * ny * nx):
        d = int(dist[li, r, c])
        if d == 0:
            path.reverse()
            return path
        moved = False
        m = int(g.mult[li, r, c])
        for nd in g.moves:
            dx, dy = DIRS[nd]
            pr, pc = r - dy, c - dx
            if not (0 <= pr < ny and 0 <= pc < nx) or not g.passable[li, pr, pc]:
                continue
            if dx and dy and not (g.passable[li, pr, c] and g.passable[li, r, pc]):
                continue
            if int(dist[li, pr, pc]) + move_cost(g, li, nd, m) == d:
                r, c, moved = pr, pc, True
                break
        if not moved and g.via_ok[r, c]:
            for l2 in range(nl):
                if l2 != li and g.passable[l2, r, c] and int(dist[l2, r, c]) + g.via_cost == d:
                    li, moved = l2, True
                    break
        if not moved:
            return None
        path.append((li, r * nx + c))
    return None


def relax_search(
    problem: SearchProblem,
    *,
    node_limit: int,
    time_limit_s: float,
    cancel: threading.Event | None = None,
    record_explored: bool = False,
    heuristic_weight: float = 1.0,
    xp: Any = np,
    to_host: Any = np.asarray,
    solver: Any = None,
) -> SearchOutcome:
    """SearchFn-compatible relaxation search (same contract as ``astar.search``).

    ``solver(g, dist0, targets, deadline=, cancel=)`` replaces the array solve,
    e.g. ``SyclRelax.solve`` (fused GPU kernel); the result must be identical."""
    t0 = time.perf_counter()
    if problem.max_vias is not None and problem.vias_enabled:
        raise ValueError("relaxation search does not support via limits (use the CPU A*)")
    g = relax_grid(problem)
    targets = np.stack([t & p for t, p in zip(problem.targets, problem.grid.passable,
                                               strict=True)])  # fmt: skip
    dist0 = seed(g, problem.sources)
    if not targets.any() or not (dist0 == 0).any():
        return SearchOutcome(SearchStatus.NO_PATH, elapsed_s=time.perf_counter() - t0)
    if solver is not None:
        status, dist, sweeps = solver(g, dist0, targets, deadline=t0 + time_limit_s,
                                      cancel=cancel)  # fmt: skip
    else:
        status, dist, sweeps = solve(xp, g, dist0, targets, deadline=t0 + time_limit_s,
                                     cancel=cancel, to_host=to_host)  # fmt: skip
    cells = int(np.prod(g.shape))
    out = SearchOutcome(status, expanded=sweeps * cells, elapsed_s=time.perf_counter() - t0)
    if status is not SearchStatus.FOUND:
        return out
    path = backtrack(g, dist, targets)
    if path is None:  # cannot happen with exact integers; never return a broken path
        out.status = SearchStatus.NO_PATH
        return out
    out.path = path
    out.cost = float(np.where(targets, dist, INF).min()) * g.cell_nm / UNIT
    out.vias = sum(1 for a, b in pairwise(path) if a[0] != b[0])
    return out
