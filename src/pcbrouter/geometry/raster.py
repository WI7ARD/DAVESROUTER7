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
