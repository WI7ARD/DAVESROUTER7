"""Vectorised (NumPy) distance fields for grid rasterisation.

Occupancy grids classify *cell centres*: a cell is blocked for a candidate track
when its centre is closer to foreign copper than ``track half-width + clearance``.
That is a sampled view of the continuous geometry, computed here in float64 over
many points at once (sub-nm precision at board scale). It is a routing aid; the
authoritative legality check for any concrete segment remains the exact validator
(:mod:`pcbrouter.routing.validator`).

Arrays are plain contiguous float64/bool NumPy arrays so a later GPU backend can
swap in an array library with the same API.
"""

from __future__ import annotations

import math

import numpy as np
import numpy.typing as npt

from pcbrouter.domain.geometry import BoundingBox
from pcbrouter.geometry.distance import Core, PointCore, SegmentCore
from pcbrouter.geometry.polygon import Polygon

type FloatGrid = npt.NDArray[np.float64]
type BoolGrid = npt.NDArray[np.bool_]


def centers(origin: int, cell: int, count: int) -> FloatGrid:
    """Cell-centre coordinates along one axis."""
    return origin + (np.arange(count, dtype=np.float64) + 0.5) * cell


def _segment_distance(
    xs: FloatGrid, ys: FloatGrid, ax: float, ay: float, bx: float, by: float
) -> FloatGrid:
    dx, dy = bx - ax, by - ay
    len2 = dx * dx + dy * dy
    if len2 == 0.0:
        return np.hypot(xs - ax, ys - ay)
    t = np.clip(((xs - ax) * dx + (ys - ay) * dy) / len2, 0.0, 1.0)
    return np.hypot(xs - (ax + t * dx), ys - (ay + t * dy))


def polygon_inside(xs: FloatGrid, ys: FloatGrid, poly: Polygon) -> BoolGrid:
    inside = np.zeros(xs.shape, dtype=np.bool_)
    for a, b in poly.edges():
        ax, ay, bx, by = float(a.x), float(a.y), float(b.x), float(b.y)
        if ay == by:
            continue
        crosses = (ay > ys) != (by > ys)
        x_int = ax + (ys - ay) * (bx - ax) / (by - ay)
        inside ^= crosses & (xs < x_int)
    return inside


#: polygons with more edges than this are rasterised with the scanline/band path
FAST_POLYGON_EDGES = 64


def polygon_inside_grid(xc: FloatGrid, yc: FloatGrid, poly: Polygon) -> BoolGrid:
    """``polygon_inside`` for a regular grid (``xc`` columns, ``yc`` rows), computed
    per row: O(rows × edges) instead of O(rows × cols × edges). Same even-odd rule
    and the same float expression, so the result is identical cell for cell."""
    pts = np.array([(p.x, p.y) for p in poly.points], dtype=np.float64)
    ax, ay = pts[:, 0], pts[:, 1]
    bx, by = np.roll(ax, -1), np.roll(ay, -1)
    keep = ay != by
    ax, ay, bx, by = ax[keep], ay[keep], bx[keep], by[keep]
    out = np.zeros((len(yc), len(xc)), dtype=np.bool_)
    for r, y in enumerate(yc):
        c = (ay > y) != (by > y)
        if not c.any():
            continue
        xi = ax[c] + (y - ay[c]) * (bx[c] - ax[c]) / (by[c] - ay[c])
        xi.sort()
        greater = len(xi) - np.searchsorted(xi, xc, side="right")  # count(x_int > x)
        out[r] = (greater & 1).astype(np.bool_)
    return out


def polygon_near_mask(
    xc: FloatGrid, yc: FloatGrid, poly: Polygon, radius: float, reach: float
) -> BoolGrid:
    """Cells whose centre is inside ``poly`` or within ``reach`` of it when grown by
    ``radius`` — i.e. ``distance_field(...) - radius <= reach`` — without a
    full-window distance field per edge: each edge is evaluated only in its own
    small neighbourhood. Identical result, far less work for big polygons (zone
    fills with thousands of vertices)."""
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
        d = _segment_distance(xs, ys, float(a.x), float(a.y), float(b.x), float(b.y))
        mask[r0:r1, c0:c1] |= (d - radius) <= reach
    return mask


def distance_field(core: Core, xs: FloatGrid, ys: FloatGrid, window: BoundingBox) -> FloatGrid:
    """Distance (nm, float) from every point ``(xs, ys)`` to ``core``; 0 inside
    polygons. ``window`` bounds the points (lets big polygons skip far edges)."""
    if isinstance(core, PointCore):
        return np.hypot(xs - core.p.x, ys - core.p.y)
    if isinstance(core, SegmentCore):
        return _segment_distance(
            xs, ys, float(core.a.x), float(core.a.y), float(core.b.x), float(core.b.y)
        )
    poly = core.polygon
    dist = np.full(xs.shape, np.inf)
    for a, b in poly.edges_near(window):
        np.minimum(
            dist,
            _segment_distance(xs, ys, float(a.x), float(a.y), float(b.x), float(b.y)),
            out=dist,
        )
    dist[polygon_inside(xs, ys, poly)] = 0.0
    return dist
