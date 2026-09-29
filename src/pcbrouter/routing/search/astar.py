"""A* over (layer, cell, incoming direction[, vias used]) states.

Moves: the 8 compass directions (4 in orthogonal style) to a passable neighbour,
and a via to any other routing layer where a through via fits. Turns sharper than
90 degrees are not generated (no acute angles). Diagonal moves require both
orthogonal neighbours passable (no corner cutting between obstacles).

Heuristic: octile distance to the bounding box of the target cells on each layer,
plus the via cost when that layer differs, scaled by the smallest possible step
factor — a lower bound of the true remaining cost, so the search is admissible
(optimal with respect to the grid and the cost model).

Pruning: a state is not expanded when the same (layer, cell[, vias]) was already
reached more than one 90-degree bend cheaper with another direction; equal f
values prefer the deeper node. The prune is exact, not heuristic: mimicking the
pruned arrival's first move from the cheaper arrival costs at most one extra
90-degree bend (all later headings coincide), so a pruned state can never beat
the arrival that pruned it, and the optimal grid cost is preserved
(test_slack_pruning_preserves_optimal_cost replays real searches through an
independent pruning-free Dijkstra). With the default weight of 1.0 the search
is optimal with respect to the grid and the cost model.

Weighted search: ``heuristic_weight`` scales the heuristic (default 1.0 keeps
admissibility). Weights above 1 trade optimality for speed with a proven bound
(final cost is at most ``weight`` times the optimal grid cost); the exact
validator still accepts or rejects every route downstream.

Determinism: the priority queue breaks ties by depth, then insertion order;
neighbour order is fixed. Same inputs → same path.
"""

from __future__ import annotations

import heapq
import itertools
import math
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import numpy.typing as npt

from pcbrouter.routing.cost.model import CostModel
from pcbrouter.routing.search.grid import SearchGrid

# direction index -> (dx, dy); 8 = "no direction yet"
DIRS: tuple[tuple[int, int], ...] = (
    (1, 0),
    (1, -1),
    (0, -1),
    (-1, -1),
    (-1, 0),
    (-1, 1),
    (0, 1),
    (1, 1),
)
NO_DIR = 8
SQRT2 = math.sqrt(2.0)
_TIME_CHECK_EVERY = 2048


class SearchStatus(Enum):
    FOUND = "found"
    NO_PATH = "no_path"
    TIMEOUT = "timeout"
    NODE_LIMIT = "node_limit"
    CANCELLED = "cancelled"


@dataclass
class SearchProblem:
    grid: SearchGrid
    sources: list[npt.NDArray[np.int64]]  # per layer: flat cell indices
    targets: list[npt.NDArray[np.bool_]]  # per layer: target mask (ny, nx)
    cost: CostModel
    layer_factor: list[float]
    via_cost: float
    max_vias: int | None
    octilinear: bool = True
    vias_enabled: bool = True


@dataclass
class SearchOutcome:
    status: SearchStatus
    path: list[tuple[int, int]] = field(default_factory=list)  # (layer index, cell)
    cost: float = 0.0
    vias: int = 0
    expanded: int = 0
    elapsed_s: float = 0.0
    explored: npt.NDArray[np.int64] | None = None


def _bbox(mask: npt.NDArray[np.bool_]) -> tuple[int, int, int, int] | None:
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    if rows.size == 0:
        return None
    return int(rows[0]), int(rows[-1]), int(cols[0]), int(cols[-1])


def search(
    problem: SearchProblem,
    *,
    node_limit: int,
    time_limit_s: float,
    cancel: threading.Event | None = None,
    record_explored: bool = False,
    heuristic_weight: float = 1.0,
) -> SearchOutcome:
    t0 = time.perf_counter()
    g = problem.grid
    nx, n, nl = g.nx, g.n, len(g.layers)
    cell = float(g.spec.cell)
    cm = problem.cost
    passable = [p.reshape(-1).tobytes() for p in g.passable]
    near = [p.reshape(-1).tobytes() for p in g.near]
    target = [t.reshape(-1).tobytes() for t in problem.targets]
    # Plain Python lists (not NumPy scalars): the hot loop indexes these
    # millions of times, and each NumPy scalar materialisation costs ~100 ns.
    penalty = [None if p is None else p.reshape(-1).tolist() for p in g.penalty]
    factor = [None if f is None else f.reshape(-1).tolist() for f in g.factor]
    vias_on = problem.vias_enabled and g.via_ok is not None and nl > 1
    via_ok = g.via_ok.reshape(-1).tobytes() if vias_on and g.via_ok is not None else b""
    vlim = problem.max_vias
    nv = (vlim + 1) if (vias_on and vlim is not None) else 1
    track_vias = vias_on and vlim is not None
    lf = problem.layer_factor
    prox = cm.proximity_factor
    dirs = [0, 2, 4, 6] if not problem.octilinear else list(range(8))

    boxes = [(li, _bbox(t)) for li, t in enumerate(problem.targets)]
    boxes2 = [(li, b) for li, b in boxes if b is not None]
    if not boxes2:
        return SearchOutcome(SearchStatus.NO_PATH, elapsed_s=time.perf_counter() - t0)
    min_factor = min(lf)
    if any(f is not None for f in factor):
        min_factor *= cm.corridor_prefer_factor
    via_h = problem.via_cost if vias_on else 0.0
    weight = max(1.0, heuristic_weight)

    # Exact octile distance-to-target field per layer (R3): the same formula as
    # the old per-push heuristic, evaluated once with NumPy instead of once per
    # pushed node. Values are bit-identical, so paths and determinism are kept.
    ny = g.ny
    rows = np.arange(ny, dtype=np.float64).reshape(-1, 1)
    cols = np.arange(nx, dtype=np.float64).reshape(1, -1)
    hfield: list[list[float]] = []
    for li in range(nl):
        best_arr: npt.NDArray[np.float64] | None = None
        for bl, (r0, r1, c0, c1) in boxes2:
            dr = np.where(rows < r0, r0 - rows, np.where(rows > r1, rows - r1, 0.0))
            dc = np.where(cols < c0, c0 - cols, np.where(cols > c1, cols - c1, 0.0))
            lo = np.minimum(dr, dc)
            hi = np.maximum(dr, dc)
            h = ((hi - lo) + SQRT2 * lo) * cell * min_factor
            if bl != li:
                h = h + via_h
            best_arr = h if best_arr is None else np.minimum(best_arr, h)
        assert best_arr is not None
        if weight != 1.0:
            best_arr = best_arr * weight
        hfield.append(best_arr.reshape(-1).tolist())

    # Turn bend-cost table [incoming dir 0..8][move]: None = disallowed (> 90°).
    # Step lengths per move (R3): hoisted out of the per-neighbour loop.
    step_len = [cell * (SQRT2 if dx and dy else 1.0) for dx, dy in DIRS]
    bend = (0.0, cm.bend45_nm, cm.bend90_nm)
    _turn_cost: list[list[float | None]] = []
    for d in range(9):
        row: list[float | None] = []
        for nd in range(8):
            if d == NO_DIR:
                row.append(0.0)
                continue
            turn = abs(nd - d)
            if turn > 4:
                turn = 8 - turn
            row.append(bend[turn] if turn <= 2 else None)
        _turn_cost.append(row)

    heap: list[tuple[float, float, int, float, int]] = []
    # best cost to reach a (vias, layer, cell) with any direction: states more than
    # one 90-degree bend worse cannot lead to a cheaper route (dominance pruning)
    cell_best: dict[int, float] = {}
    slack = cm.bend90_nm
    best_g: dict[int, float] = {}
    parent: dict[int, int] = {}
    tie = 0
    for li, cells in enumerate(problem.sources):
        pl = passable[li]
        for idx in cells.tolist():
            if not pl[idx]:
                continue
            s = (li * n + idx) * 9 + NO_DIR
            if s in best_g:
                continue
            best_g[s] = 0.0
            heapq.heappush(heap, (hfield[li][idx], 0.0, tie, 0.0, s))
            tie += 1
    if not heap:
        return SearchOutcome(SearchStatus.NO_PATH, elapsed_s=time.perf_counter() - t0)

    expanded = 0
    explored: list[int] = []
    deadline = t0 + time_limit_s
    status = SearchStatus.NO_PATH
    goal = -1
    while heap:
        _f, _ng, _t, gc, s = heapq.heappop(heap)
        if gc > best_g.get(s, math.inf):
            continue
        d = s % 9
        rest = s // 9
        idx = rest % n
        rest //= n
        li = rest % nl
        v = rest // nl
        if target[li][idx]:
            status, goal = SearchStatus.FOUND, s
            break
        expanded += 1
        if record_explored:
            explored.append(idx)
        if expanded >= node_limit:
            status = SearchStatus.NODE_LIMIT
            break
        if expanded % _TIME_CHECK_EVERY == 0:
            if time.perf_counter() > deadline:
                status = SearchStatus.TIMEOUT
                break
            if cancel is not None and cancel.is_set():
                status = SearchStatus.CANCELLED
                break
        r, c = divmod(idx, nx)
        pl = passable[li]
        nr_l = near[li]
        pen = penalty[li]
        fac = factor[li]
        base = (v * nl + li) * n
        turn_costs = _turn_cost[d]
        h_layer = hfield[li]
        for nd in dirs:
            bcost = turn_costs[nd]
            if bcost is None:
                continue
            dx, dy = DIRS[nd]
            rr, cc = r + dy, c + dx
            if rr < 0 or rr >= ny or cc < 0 or cc >= nx:
                continue
            ni = rr * nx + cc
            if not pl[ni]:
                continue
            if dx and dy and not (pl[r * nx + cc] and pl[rr * nx + c]):
                continue
            step = step_len[nd]
            w = lf[li] * (float(fac[ni]) if fac is not None else 1.0)
            cost = step * w * (1.0 + prox * nr_l[ni])
            if pen is not None:
                cost += step * float(pen[ni])
            cost += bcost
            ng = gc + cost
            ck = base + ni
            cb = cell_best.get(ck, math.inf)
            if ng > cb + slack:
                continue
            ns = ck * 9 + nd
            if ng < best_g.get(ns, math.inf):
                best_g[ns] = ng
                if ng < cb:
                    cell_best[ck] = ng
                parent[ns] = s
                heapq.heappush(heap, (ng + h_layer[ni], -ng, tie, ng, ns))
                tie += 1
        if vias_on and via_ok[idx] and (not track_vias or v + 1 < nv):
            v2 = v + 1 if track_vias else v
            for l2 in range(nl):
                if l2 == li or not passable[l2][idx]:
                    continue
                ns = ((v2 * nl + l2) * n + idx) * 9 + NO_DIR
                ng = gc + problem.via_cost
                if ng < best_g.get(ns, math.inf):
                    best_g[ns] = ng
                    parent[ns] = s
                    heapq.heappush(heap, (ng + hfield[l2][idx], -ng, tie, ng, ns))
                    tie += 1
    out = SearchOutcome(status, expanded=expanded, elapsed_s=time.perf_counter() - t0)
    if record_explored:
        out.explored = np.asarray(explored, dtype=np.int64)
    if status is not SearchStatus.FOUND:
        return out
    path: list[tuple[int, int]] = []
    s = goal
    out.cost = best_g[goal]
    seen: set[int] = set()
    max_steps = len(parent) + 1
    while True:
        if s in seen or len(path) > max_steps:
            # Corrupt parent chain must not hang the worker: report no path.
            return SearchOutcome(
                SearchStatus.NO_PATH,
                expanded=expanded,
                elapsed_s=time.perf_counter() - t0,
            )
        seen.add(s)
        rest = s // 9
        idx = rest % n
        li = (rest // n) % nl
        path.append((li, idx))
        if s not in parent:
            break
        s = parent[s]
    path.reverse()
    out.path = path
    out.vias = sum(1 for a, b in itertools.pairwise(path) if a[0] != b[0])
    return out
