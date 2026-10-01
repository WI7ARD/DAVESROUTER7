"""polygon_near_mask's vectorised short-edge batches must give exactly the cells
the original per-edge loop gave (the occupancy grid's soundness depends on it)."""

from __future__ import annotations

import math
import random

import numpy as np

from pcbrouter.domain.geometry import Point
from pcbrouter.geometry import raster
from pcbrouter.geometry.polygon import Polygon
from pcbrouter.geometry.raster import centers, polygon_inside_grid, polygon_near_mask


def _reference(xc, yc, poly, radius, reach):  # type: ignore[no-untyped-def]
    """The pre-batching implementation, kept verbatim as the oracle."""
    mask = polygon_inside_grid(xc, yc, poly)
    grow = math.ceil(reach + radius) + 1
    for a, b in poly.edges():
        x0, x1 = min(a.x, b.x) - grow, max(a.x, b.x) + grow
        y0, y1 = min(a.y, b.y) - grow, max(a.y, b.y) + grow
        c0, c1 = np.searchsorted(xc, x0, "left"), np.searchsorted(xc, x1, "right")
        r0, r1 = np.searchsorted(yc, y0, "left"), np.searchsorted(yc, y1, "right")
        if c0 >= c1 or r0 >= r1:
            continue
        xs, ys = np.meshgrid(xc[c0:c1], yc[r0:r1])
        d = raster._segment_distance(xs, ys, float(a.x), float(a.y), float(b.x), float(b.y))
        mask[r0:r1, c0:c1] |= (d - radius) <= reach
    return mask


def _blob(rng: random.Random, n: int, cx: int, cy: int, r: int) -> Polygon:
    """A star-shaped polygon with n vertices (short and long edges mixed)."""
    pts = []
    for i in range(n):
        ang = 2 * math.pi * i / n
        rad = r * (0.4 + 0.6 * rng.random())
        pts.append(Point(round(cx + rad * math.cos(ang)), round(cy + rad * math.sin(ang))))
    return Polygon(pts)


def test_batched_mask_equals_the_per_edge_reference() -> None:
    rng = random.Random(7)
    cell = 100_000
    xc, yc = centers(0, cell, 220), centers(0, cell, 180)
    for n, r in ((12, 6_000_000), (300, 7_000_000), (2_000, 8_000_000)):
        poly = _blob(rng, n, 11_000_000, 9_000_000, r)
        for radius, reach in ((0.0, 125_000.0), (50_000.0, 300_000.0), (0.0, 0.0)):
            got = polygon_near_mask(xc, yc, poly, radius, reach)
            want = _reference(xc, yc, poly, radius, reach)
            assert np.array_equal(got, want), (n, radius, reach)


def test_tiny_batches_and_clipped_windows_still_match(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(raster, "_BATCH_PAIRS", 50)  # force many batches
    rng = random.Random(3)
    cell = 100_000
    # the window covers only part of the polygon: edges outside are skipped
    xc, yc = centers(4_000_000, cell, 60), centers(3_000_000, cell, 50)
    poly = _blob(rng, 500, 7_000_000, 6_000_000, 5_000_000)
    got = polygon_near_mask(xc, yc, poly, 0.0, 200_000.0)
    assert np.array_equal(got, _reference(xc, yc, poly, 0.0, 200_000.0))


def test_repeated_vertices_give_zero_length_edges_safely() -> None:
    cell = 100_000
    xc, yc = centers(0, cell, 50), centers(0, cell, 50)
    pts = [Point(1_000_000, 1_000_000), Point(4_000_000, 1_000_000), Point(4_000_000, 1_000_000),
           Point(4_000_000, 4_000_000), Point(1_000_000, 4_000_000)]  # fmt: skip
    poly = Polygon(pts)
    for reach in (0.0, 150_000.0):
        assert np.array_equal(
            polygon_near_mask(xc, yc, poly, 0.0, reach), _reference(xc, yc, poly, 0.0, reach)
        )
