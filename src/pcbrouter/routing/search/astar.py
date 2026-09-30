"""A* over (layer, cell, incoming direction[, vias used]) states.

Moves: the 8 compass directions (4 in orthogonal style) to a passable neighbour,
and a via to any other routing layer where a through via fits. Turns sharper than
90 degrees are not generated (no acute angles). Diagonal moves require both
orthogonal neighbours passable (no corner cutting between obstacles).

Heuristic: octile distance to the nearest target box (one box per unconnected
pad group when the caller gives ``target_boxes``, merged to at most 16; else one
box around all target cells per layer), plus the via cost when that layer
differs, scaled by the smallest possible step factor — a lower bound of the true
remaining cost, so the search is admissible (optimal with respect to the grid and
the cost model). With preferred layer directions (``cost.wrong_way_factor`` >= 2)
the distance is Manhattan, plus the cheaper of the wrong-way extra or two vias
for same-layer targets across the layer's direction: still a lower bound.

Coarse-to-fine (``coarse_factor``): a search on a coarser grid guides a corridor
for the fine search; a full fine search is the fallback, and an optimistic
coarse flood fill proves "no path" cheaply. See :func:`search`.

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
from dataclasses import dataclass, field, replace
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
    #: tighter heuristic targets: (layer, r0, r1, c0, c1) boxes that together cover
    #: every target cell, e.g. one per unconnected pad group. Without them the
    #: heuristic measures to ONE box around all targets per layer, which is ~0
    #: almost everywhere on a net spread over the board (search degrades to a
    #: Dijkstra flood: millions of nodes, then NODE_LIMIT/NO_PATH).
    target_boxes: tuple[tuple[int, int, int, int, int], ...] = ()
    #: preferred direction per layer: 0 none, 1 horizontal, 2 vertical; steps
    #: against it cost ``cost.wrong_way_factor`` (>= 1: heuristic stays admissible)
    layer_dirs: tuple[int, ...] = ()
    #: coarse-to-fine: first search a grid ``coarse_factor`` times coarser, then
    #: search the fine grid only inside a corridor around that path (full fine
    #: search as fallback, so no route is lost). 0/1 = off.
    coarse_factor: int = 0


#: at most this many heuristic boxes (each costs one NumPy pass per layer)
MAX_HEURISTIC_BOXES = 16


def merge_boxes(
    boxes: list[tuple[int, int, int, int, int]], limit: int = MAX_HEURISTIC_BOXES
) -> list[tuple[int, int, int, int, int]]:
    """Merge same-layer boxes (smallest area growth first) until at most ``limit``
    remain. A merged box contains both inputs, so the distance to it is never
    larger than to either: the heuristic stays a lower bound (admissible)."""
    out = list(dict.fromkeys(boxes))

    def area(b: tuple[int, int, int, int, int]) -> int:
        return (b[2] - b[1] + 1) * (b[4] - b[3] + 1)

    while len(out) > limit:
        best: tuple[int, int, int] | None = None
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                p, q = out[i], out[j]
                if p[0] != q[0]:
                    continue
                m = (p[0], min(p[1], q[1]), max(p[2], q[2]), min(p[3], q[3]), max(p[4], q[4]))
                grow = area(m) - area(p) - area(q)
                if best is None or grow < best[0]:
                    best = (grow, i, j)
        if best is None:
            break  # one box per layer already
        _g, i, j = best
        p, q = out[i], out[j]
        out[i] = (p[0], min(p[1], q[1]), max(p[2], q[2]), min(p[3], q[3]), max(p[4], q[4]))
        del out[j]
    return out


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


#: coarse-to-fine outcome counts for this process (diagnostics / benchmarks)
C2F_STATS: dict[str, int] = {}


def _count(key: str, nodes: int = 0) -> None:
    C2F_STATS[key] = C2F_STATS.get(key, 0) + 1
    if nodes:
        C2F_STATS[key + "_nodes"] = C2F_STATS.get(key + "_nodes", 0) + nodes


#: corridor half-width around the coarse path, in coarse cells
CORRIDOR_RADIUS = 2
#: below this many fine cells per layer a direct search is already cheap
COARSE_MIN_CELLS = 40_000
#: when the source copper already lies within this many cells of a target box,
#: the direct search is cheap and a coarse corridor only adds work (measured on a
#: 4-layer board: GND plane connections found directly in ~600 nodes, while the
#: coarse corridor failed twice first, ~1 s per connection)
COARSE_NEAR_CELLS = 40


def _source_near_target(problem: SearchProblem) -> bool:
    nx = problem.grid.nx
    boxes = list(problem.target_boxes)
    if not boxes:
        for li, t in enumerate(problem.targets):
            b = _bbox(t)
            if b is not None:
                boxes.append((li, *b))
    for cells in problem.sources:
        if cells.size == 0:
            continue
        r, c = np.divmod(cells, nx)
        r0, r1, c0, c1 = int(r.min()), int(r.max()), int(c.min()), int(c.max())
        for _bl, br0, br1, bc0, bc1 in boxes:
            dr = max(0, br0 - r1, r0 - br1)
            dc = max(0, bc0 - c1, c0 - bc1)
            if max(dr, dc) <= COARSE_NEAR_CELLS:
                return True
    return False


#: a coarse cell is passable when at least this share of its fine cells is: a
#: path through nearly-blocked coarse cells mostly fails at fine resolution
COARSE_FILL = 0.5
#: corridor retry radius (coarse cells) before the full fine search
CORRIDOR_RETRY_RADIUS = 5


def _block_any(a: npt.NDArray[np.bool_], k: int) -> npt.NDArray[np.bool_]:
    ny, nx = a.shape
    py, px = -ny % k, -nx % k
    if py or px:
        a = np.pad(a, ((0, py), (0, px)))
    return a.reshape(a.shape[0] // k, k, a.shape[1] // k, k).any(axis=(1, 3))


def _block_fill(a: npt.NDArray[np.bool_], k: int, fill: float) -> npt.NDArray[np.bool_]:
    ny, nx = a.shape
    py, px = -ny % k, -nx % k
    if py or px:
        a = np.pad(a, ((0, py), (0, px)))
    share = a.reshape(a.shape[0] // k, k, a.shape[1] // k, k).mean(axis=(1, 3))
    return np.asarray(share >= fill)


def _reachable_optimistic(problem: SearchProblem, k: int) -> bool:
    """Can any target be reached on the OPTIMISTIC coarse grid (a coarse cell is
    free when any of its fine cells is), with 8-neighbour moves, no turn rules and
    vias anywhere a via fits? Every fine path projects onto such a coarse walk, so
    False proves that no fine path exists."""
    g = problem.grid
    free = [_block_any(p, k) for p in g.passable]
    tgt = [_block_any(t, k) for t in problem.targets]
    via = None if (g.via_ok is None or not problem.vias_enabled) else _block_any(g.via_ok, k)
    cny, cnx = free[0].shape
    reach = [np.zeros((cny, cnx), dtype=np.bool_) for _ in free]
    for li, cells in enumerate(problem.sources):
        if cells.size:
            r, c = np.divmod(cells, g.nx)
            reach[li][r // k, c // k] = True
    for li in range(len(free)):
        reach[li] &= free[li] | tgt[li]
    while True:
        if any(bool((reach[li] & tgt[li]).any()) for li in range(len(free))):
            return True
        grown = []
        for li, rm in enumerate(reach):
            d = rm.copy()
            d[1:, :] |= rm[:-1, :]
            d[:-1, :] |= rm[1:, :]
            d[:, 1:] |= rm[:, :-1]
            d[:, :-1] |= rm[:, 1:]
            d[1:, 1:] |= rm[:-1, :-1]
            d[:-1, :-1] |= rm[1:, 1:]
            d[1:, :-1] |= rm[:-1, 1:]
            d[:-1, 1:] |= rm[1:, :-1]
            grown.append(d & (free[li] | tgt[li]))
        if via is not None and len(grown) > 1:
            hop = np.zeros_like(via)
            for gm in grown:
                hop |= gm & via
            grown = [gm | (hop & (free[li] | tgt[li])) for li, gm in enumerate(grown)]
        if all(bool((grown[li] == reach[li]).all()) for li in range(len(free))):
            return False
        reach = grown


def _coarse_problem(problem: SearchProblem, k: int, fill: float = COARSE_FILL) -> SearchProblem:
    """The same problem on a k-times coarser grid. A coarse cell is passable when
    any of its fine cells is (optimistic: the coarse path is only a guide; the
    fine search inside its corridor, or the fallback, decides)."""
    g = problem.grid
    spec = g.spec
    cny, cnx = -(-spec.ny // k), -(-spec.nx // k)
    cspec = replace(spec, cell=spec.cell * k, nx=cnx, ny=cny)
    passable = [_block_fill(p, k, fill) if fill > 0 else _block_any(p, k) for p in g.passable]
    zeros = np.zeros((cny, cnx), dtype=np.bool_)
    via_ok = None if g.via_ok is None else _block_any(g.via_ok, k)
    grid = SearchGrid(cspec, g.layers, passable, [zeros for _ in g.layers], via_ok,
                      [None for _ in g.layers], [None for _ in g.layers])  # fmt: skip
    sources = []
    for li, cells in enumerate(problem.sources):
        r, c = np.divmod(cells, spec.nx)
        cells_c = np.unique((r // k) * cnx + (c // k)).astype(np.int64)
        passable[li].reshape(-1)[cells_c] = True  # pads may sit in crowded blocks
        sources.append(cells_c)
    targets = [_block_any(t, k) for t in problem.targets]
    for li, t in enumerate(targets):
        passable[li] |= t
    boxes = tuple((b[0], b[1] // k, b[2] // k, b[3] // k, b[4] // k) for b in problem.target_boxes)
    return replace(problem, grid=grid, sources=sources, targets=targets, target_boxes=boxes,
                   coarse_factor=0)  # fmt: skip


def search(
    problem: SearchProblem,
    *,
    node_limit: int,
    time_limit_s: float,
    cancel: threading.Event | None = None,
    record_explored: bool = False,
    heuristic_weight: float = 1.0,
) -> SearchOutcome:
    """A* search; with ``problem.coarse_factor`` > 1 coarse-to-fine (see there)."""
    k = problem.coarse_factor
    g = problem.grid
    if k <= 1 or g.n < COARSE_MIN_CELLS or record_explored or _source_near_target(problem):
        return _search(problem, node_limit=node_limit, time_limit_s=time_limit_s,
                       cancel=cancel, record_explored=record_explored,
                       heuristic_weight=heuristic_weight)  # fmt: skip
    t0 = time.perf_counter()
    coarse = _coarse_problem(problem, k)
    c_out = _search(coarse, node_limit=node_limit, time_limit_s=time_limit_s / 4,
                    cancel=cancel, heuristic_weight=heuristic_weight)  # fmt: skip
    expanded = c_out.expanded
    _count(f"coarse_{c_out.status.value}", c_out.expanded)
    if c_out.status is SearchStatus.NO_PATH:
        # half-free coarse cells may close a narrow real channel: try the
        # optimistic coarse grid (any free fine cell) before a full fine search
        coarse = _coarse_problem(problem, k, fill=0.0)
        c_out = _search(coarse, node_limit=node_limit, time_limit_s=time_limit_s / 4,
                        cancel=cancel, heuristic_weight=heuristic_weight)  # fmt: skip
        expanded += c_out.expanded
        _count(f"coarse_any_{c_out.status.value}", c_out.expanded)
    if c_out.status is SearchStatus.CANCELLED:
        return c_out
    if c_out.status is SearchStatus.FOUND:
        cg = coarse.grid
        for radius in (CORRIDOR_RADIUS, CORRIDOR_RETRY_RADIUS):
            band = np.zeros((cg.ny, cg.nx), dtype=np.bool_)
            for _li, idx in c_out.path:
                r, c = divmod(idx, cg.nx)
                band[max(0, r - radius) : r + radius + 1,
                     max(0, c - radius) : c + radius + 1] = True  # fmt: skip
            fine_band = np.kron(band, np.ones((k, k), dtype=np.bool_))[: g.ny, : g.nx]
            narrowed = replace(g, passable=[p & fine_band for p in g.passable])
            left = max(0.05, time_limit_s - (time.perf_counter() - t0))
            f_out = _search(replace(problem, grid=narrowed, coarse_factor=0),
                            node_limit=node_limit, time_limit_s=left / 2, cancel=cancel,
                            heuristic_weight=heuristic_weight)  # fmt: skip
            expanded += f_out.expanded
            _count(f"corridor{radius}_{f_out.status.value}", f_out.expanded)
            if f_out.status is SearchStatus.FOUND or f_out.status is SearchStatus.CANCELLED:
                f_out.expanded = expanded
                f_out.elapsed_s = time.perf_counter() - t0
                return f_out
    if c_out.status is SearchStatus.NO_PATH and not _reachable_optimistic(problem, k):
        _count("proved_no_path")
        c_out.expanded = expanded
        c_out.elapsed_s = time.perf_counter() - t0
        return c_out
    # corridor too tight, or no/limited coarse path that could not be proved
    # impossible: full fine search
    left = max(0.05, time_limit_s - (time.perf_counter() - t0))
    out = _search(problem, node_limit=node_limit, time_limit_s=left, cancel=cancel,
                  heuristic_weight=heuristic_weight)  # fmt: skip
    _count(f"fallback_{out.status.value}", out.expanded)
    out.expanded += expanded
    out.elapsed_s = time.perf_counter() - t0
    return out


def _search(
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
    if problem.target_boxes:
        boxes2 = [(b[0], (b[1], b[2], b[3], b[4])) for b in merge_boxes(list(problem.target_boxes))]
    min_factor = min(lf)
    if any(f is not None for f in factor):
        min_factor *= cm.corridor_prefer_factor
    via_h = problem.via_cost if vias_on else 0.0
    weight = max(1.0, heuristic_weight)
    # With layer directions and wrong_way_factor >= 2 a diagonal step costs at
    # least (1 + 2) / 2 * sqrt(2) > 2 straight steps, so Manhattan distance is
    # still a lower bound on every layer — and tighter than octile.
    manhattan = (
        bool(problem.layer_dirs)
        and cm.wrong_way_factor >= 2.0
        and all(problem.layer_dirs[li] for li in range(min(nl, len(problem.layer_dirs))))
        and len(problem.layer_dirs) >= nl
    )
    wwf = cm.wrong_way_factor
    # cheapest extra per cell of cross-direction distance, beyond Manhattan cost
    ww_extra = min(wwf - 1.0, (1.0 + wwf) / 2.0 * SQRT2 - 2.0)

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
            if manhattan:
                h = (hi + lo) * cell * min_factor
                if bl == li:
                    # same-layer target across this layer's preferred direction:
                    # every cell of that distance costs wrong-way extra (straight
                    # or diagonal, whichever is cheaper), unless two vias are paid
                    perp = dr if problem.layer_dirs[li] == 1 else dc
                    extra = perp * cell * min_factor * ww_extra
                    if vias_on:
                        extra = np.minimum(extra, 2.0 * via_h)
                    h = h + extra
            else:
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
    # per-layer step length including the preferred-direction factor
    ww = cm.wrong_way_factor
    layer_step: list[list[float]] = []
    for li in range(nl):
        pref = problem.layer_dirs[li] if li < len(problem.layer_dirs) else 0
        row_s: list[float] = []
        for nd, (dx, dy) in enumerate(DIRS):
            f = 1.0
            if pref and ww > 1.0:
                if dx and dy:
                    f = (1.0 + ww) / 2.0
                elif (pref == 1 and dy) or (pref == 2 and dx):
                    f = ww
            row_s.append(step_len[nd] * f)
        layer_step.append(row_s)
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
        lstep = layer_step[li]
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
            step = lstep[nd]
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
