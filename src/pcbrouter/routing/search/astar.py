"""A* over (layer, cell, incoming direction[, vias used]) states.

Moves: the 8 compass directions (4 in orthogonal style) to a passable neighbour,
and a via to any other routing layer where a through via fits. Turns sharper than
90 degrees are not generated (no acute angles). Diagonal moves require both
orthogonal neighbours passable (no corner cutting between obstacles).

Heuristic: octile distance to the bounding box of the target cells on each layer,
plus the via cost when that layer differs, scaled by the smallest possible step
factor — a lower bound of the true remaining cost, so the search is admissible
(optimal with respect to the grid and the cost model).

Determinism: the priority queue breaks ties by insertion order; neighbour order
is fixed. Same inputs → same path.
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
) -> SearchOutcome:
    t0 = time.perf_counter()
    g = problem.grid
    nx, n, nl = g.nx, g.n, len(g.layers)
    cell = float(g.spec.cell)
    cm = problem.cost
    passable = [p.reshape(-1).tobytes() for p in g.passable]
    near = [p.reshape(-1).tobytes() for p in g.near]
    target = [t.reshape(-1).tobytes() for t in problem.targets]
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
    bend = (0.0, cm.bend45_nm, cm.bend90_nm)

    boxes = [(li, _bbox(t)) for li, t in enumerate(problem.targets)]
    boxes2 = [(li, b) for li, b in boxes if b is not None]
    if not boxes2:
        return SearchOutcome(SearchStatus.NO_PATH, elapsed_s=time.perf_counter() - t0)
    min_factor = min(lf)
    if any(f is not None for f in factor):
        min_factor *= cm.corridor_prefer_factor
    via_h = problem.via_cost if vias_on else 0.0

    def heuristic(li: int, idx: int) -> float:
        r, c = divmod(idx, nx)
        best = math.inf
        for bl, (r0, r1, c0, c1) in boxes2:
            dr = r0 - r if r < r0 else (r - r1 if r > r1 else 0)
            dc = c0 - c if c < c0 else (c - c1 if c > c1 else 0)
            lo, hi = (dr, dc) if dr < dc else (dc, dr)
            h = ((hi - lo) + SQRT2 * lo) * cell * min_factor
            if bl != li:
                h += via_h
            if h < best:
                best = h
        return best

    heap: list[tuple[float, int, float, int]] = []
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
            heapq.heappush(heap, (heuristic(li, idx), tie, 0.0, s))
            tie += 1
    if not heap:
        return SearchOutcome(SearchStatus.NO_PATH, elapsed_s=time.perf_counter() - t0)

    expanded = 0
    explored: list[int] = []
    deadline = t0 + time_limit_s
    status = SearchStatus.NO_PATH
    goal = -1
    ny = g.ny
    while heap:
        _f, _t, gc, s = heapq.heappop(heap)
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
        for nd in dirs:
            if d != NO_DIR:
                turn = abs(nd - d)
                if turn > 4:
                    turn = 8 - turn
                if turn > 2:
                    continue
            else:
                turn = 0
            dx, dy = DIRS[nd]
            rr, cc = r + dy, c + dx
            if rr < 0 or rr >= ny or cc < 0 or cc >= nx:
                continue
            ni = rr * nx + cc
            if not pl[ni]:
                continue
            if dx and dy and not (pl[r * nx + cc] and pl[rr * nx + c]):
                continue
            step = cell * (SQRT2 if dx and dy else 1.0)
            w = lf[li] * (fac[ni] if fac is not None else 1.0)
            cost = step * w * (1.0 + prox * nr_l[ni])
            if pen is not None:
                cost += step * pen[ni]
            cost += bend[turn]
            ns = (base + ni) * 9 + nd
            ng = gc + cost
            if ng < best_g.get(ns, math.inf):
                best_g[ns] = ng
                parent[ns] = s
                heapq.heappush(heap, (ng + heuristic(li, ni), tie, ng, ns))
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
                    heapq.heappush(heap, (ng + heuristic(l2, idx), tie, ng, ns))
                    tie += 1
    out = SearchOutcome(status, expanded=expanded, elapsed_s=time.perf_counter() - t0)
    if record_explored:
        out.explored = np.asarray(explored, dtype=np.int64)
    if status is not SearchStatus.FOUND:
        return out
    path: list[tuple[int, int]] = []
    s = goal
    out.cost = best_g[goal]
    while True:
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
