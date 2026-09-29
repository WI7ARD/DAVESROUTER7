"""Regression: A* heuristic on nets spread over the board.

Real boards (KiCad 10 ``pic_programmer``, tracks stripped) showed single nets
expanding ~1.9 million nodes (42 s) and ending NODE_LIMIT/NO_PATH although a
plain flood fill reached every target: the heuristic measured distance to ONE
box around all unconnected pads, which covers most of the board, so it was ~0
almost everywhere and A* degraded to a Dijkstra flood. One box per pad group
(merged to at most 16) keeps the heuristic admissible and informative.
"""

from __future__ import annotations

import numpy as np

from pcbrouter.routing.cost.model import DEFAULT_COST_MODEL
from pcbrouter.routing.occupancy import GridSpec
from pcbrouter.routing.search.astar import (
    MAX_HEURISTIC_BOXES,
    SearchProblem,
    SearchStatus,
    merge_boxes,
    search,
)
from pcbrouter.routing.search.grid import SearchGrid

N = 400  # 40 x 40 mm at 0.1 mm cells


def open_grid() -> SearchGrid:
    spec = GridSpec(0, 0, 100_000, N, N, "F.Cu")
    free = np.ones((N, N), dtype=np.bool_)
    return SearchGrid(
        spec,
        ("F.Cu",),
        [free],
        [np.zeros((N, N), dtype=np.bool_)],
        None,
        [None],
        [None],
    )


def spread_problem(with_boxes: bool) -> SearchProblem:
    """Source near the middle; one target pad near it, one in a far corner — the
    single bounding box then spans almost the whole grid."""
    g = open_grid()
    targets = np.zeros((N, N), dtype=np.bool_)
    near_pad = (200, 202, 230, 234)  # rows 200-202, cols 230-234 (3 mm away)
    far_pad = (2, 4, 2, 4)
    boxes = []
    for r0, r1, c0, c1 in (near_pad, far_pad):
        targets[r0 : r1 + 1, c0 : c1 + 1] = True
        boxes.append((0, r0, r1, c0, c1))
    sources = [np.asarray([200 * N + 200], dtype=np.int64)]
    return SearchProblem(
        g,
        sources,
        [targets],
        DEFAULT_COST_MODEL,
        [1.0],
        0.0,
        None,
        vias_enabled=False,
        target_boxes=tuple(boxes) if with_boxes else (),
    )


def test_per_group_boxes_cut_expansions_and_keep_the_optimum() -> None:
    old = search(spread_problem(False), node_limit=2_000_000, time_limit_s=60)
    new = search(spread_problem(True), node_limit=2_000_000, time_limit_s=60)
    assert old.status is SearchStatus.FOUND and new.status is SearchStatus.FOUND
    assert new.cost == old.cost  # still admissible: same optimal cost
    assert new.expanded * 20 < old.expanded, (new.expanded, old.expanded)


def test_merge_boxes_is_bounded_and_contains_its_inputs() -> None:
    rng = np.random.default_rng(7)
    boxes = []
    for _ in range(60):
        layer = int(rng.integers(0, 2))
        r0, c0 = (int(v) for v in rng.integers(0, 300, 2))
        boxes.append((layer, r0, r0 + int(rng.integers(0, 20)), c0, c0 + int(rng.integers(0, 20))))
    merged = merge_boxes(boxes)
    assert len(merged) <= MAX_HEURISTIC_BOXES
    for b in boxes:  # every input lies inside a merged box on its layer (admissible)
        assert any(
            m[0] == b[0] and m[1] <= b[1] and m[2] >= b[2] and m[3] <= b[3] and m[4] >= b[4]
            for m in merged
        )
    assert merge_boxes(boxes[:5]) == list(dict.fromkeys(boxes[:5]))  # few: unchanged


def test_coarse_to_fine_finds_routes_and_falls_back() -> None:
    from dataclasses import replace

    direct = search(spread_problem(True), node_limit=2_000_000, time_limit_s=60)
    c2f = search(replace(spread_problem(True), coarse_factor=4), node_limit=2_000_000,
                 time_limit_s=60)  # fmt: skip
    assert c2f.status is SearchStatus.FOUND
    assert c2f.cost <= direct.cost * 1.05  # corridor keeps near-optimal routes
    # a wall with a one-cell gap: coarse cells there are "mostly blocked", so the
    # coarse guide misses the gap; the fallback still finds the only route
    p = spread_problem(True)
    wall = np.ones((N, N), dtype=np.bool_)
    wall[:, 215] = False
    wall[201, 215] = True  # the gap
    p = replace(p, grid=replace(p.grid, passable=[wall]), coarse_factor=4)
    out = search(p, node_limit=2_000_000, time_limit_s=60)
    assert out.status is SearchStatus.FOUND
    assert any(idx == 201 * N + 215 for _li, idx in out.path)
    # source walled in: the optimistic coarse flood fill proves there is no route
    ring = np.ones((N, N), dtype=np.bool_)
    ring[180:221, 180] = ring[180:221, 220] = False
    ring[180, 180:221] = ring[220, 180:221] = False
    closed = replace(p, grid=replace(p.grid, passable=[ring]))
    out2 = search(closed, node_limit=2_000_000, time_limit_s=60)
    assert out2.status is SearchStatus.NO_PATH and out2.expanded < 200_000
