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
from collections.abc import Callable

import numpy as np
import numpy.typing as npt

from pcbrouter.domain.geometry import BoundingBox
from pcbrouter.geometry.distance import Core, PointCore, SegmentCore
from pcbrouter.geometry.polygon import Polygon

type FloatGrid = npt.NDArray[np.float64]
type BoolGrid = npt.NDArray[np.bool_]


class CancelledError(Exception):
    """A raster loop was cancelled via its ``cancel`` predicate."""


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


def polygon_inside_grid(
    xc: FloatGrid,
    yc: FloatGrid,
    poly: Polygon,
    cancel: Callable[[], bool] | None = None,
) -> BoolGrid:
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
        if cancel is not None and r % 64 == 0 and cancel():
            raise CancelledError()
        c = (ay > y) != (by > y)
        if not c.any():
            continue
        xi = ax[c] + (y - ay[c]) * (bx[c] - ax[c]) / (by[c] - ay[c])
        xi.sort()
        greater = len(xi) - np.searchsorted(xi, xc, side="right")  # count(x_int > x)
        out[r] = (greater & 1).astype(np.bool_)
    return out


#: polygon edges spanning at most this many cells (plus the reach band) are
#: evaluated together in one vectorised batch; longer edges one by one
_BATCH_EDGE_CELLS = 16
#: (edge, cell) pairs per vectorised batch (memory bound: ~50 bytes per pair)
_BATCH_PAIRS = 2_000_000


def polygon_near_mask(
    xc: FloatGrid,
    yc: FloatGrid,
    poly: Polygon,
    radius: float,
    reach: float,
    cancel: Callable[[], bool] | None = None,
) -> BoolGrid:
    """Cells whose centre is inside ``poly`` or within ``reach`` of it when grown by
    ``radius`` — i.e. ``distance_field(...) - radius <= reach`` — without a
    full-window distance field per edge: each edge is evaluated only in its own
    small neighbourhood. Identical result, far less work for big polygons (zone
    fills with thousands of vertices).

    Zone fills consist mostly of short edges: those are evaluated in vectorised
    batches of (edge, cell) pairs with the same float expression as
    :func:`_segment_distance` (a 25 000-edge GND pour went from 1.8 s to a few
    tens of ms per grid); long edges keep the per-edge path."""
    mask = polygon_inside_grid(xc, yc, poly, cancel)
    if len(xc) == 0 or len(yc) == 0:
        return mask
    grow = math.ceil(reach + radius) + 1
    pts = np.array([(p.x, p.y) for p in poly.points], dtype=np.float64)
    ax, ay = pts[:, 0], pts[:, 1]
    bx, by = np.roll(ax, -1), np.roll(ay, -1)
    c0 = np.searchsorted(xc, np.minimum(ax, bx) - grow, "left")
    c1 = np.searchsorted(xc, np.maximum(ax, bx) + grow, "right")
    r0 = np.searchsorted(yc, np.minimum(ay, by) - grow, "left")
    r1 = np.searchsorted(yc, np.maximum(ay, by) + grow, "right")
    nc, nr = c1 - c0, r1 - r0
    live = (nc > 0) & (nr > 0)
    short = live & (nc <= _BATCH_EDGE_CELLS + 2 * grow) & (nr <= _BATCH_EDGE_CELLS + 2 * grow)
    short &= nc * nr <= (_BATCH_EDGE_CELLS + 2) ** 2 * 4
    for i in np.flatnonzero(live & ~short):  # long edges: one window each
        if cancel is not None and cancel():
            raise CancelledError()
        xs, ys = np.meshgrid(xc[c0[i] : c1[i]], yc[r0[i] : r1[i]])
        d = _segment_distance(
            xs, ys, float(ax[i]), float(ay[i]), float(bx[i]), float(by[i])
        )
        mask[r0[i] : r1[i], c0[i] : c1[i]] |= (d - radius) <= reach
    idx = np.flatnonzero(short)
    sizes = (nc * nr)[idx]
    start = 0
    while start < len(idx):
        if cancel is not None and cancel():
            raise CancelledError()
        stop = start + max(1, int(np.searchsorted(np.cumsum(sizes[start:]), _BATCH_PAIRS)))
        e = idx[start:stop]
        n = (nc * nr)[e]
        edge = np.repeat(np.arange(len(e)), n)  # pair -> edge (within batch)
        offs = np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n)
        w = nc[e][edge]
        col = c0[e][edge] + offs % w
        row = r0[e][edge] + offs // w
        x, y = xc[col], yc[row]
        eax, eay, ebx, eby = ax[e][edge], ay[e][edge], bx[e][edge], by[e][edge]
        dx, dy = ebx - eax, eby - eay
        len2 = dx * dx + dy * dy
        zero = len2 == 0.0
        safe = np.where(zero, 1.0, len2)
        t = np.clip(((x - eax) * dx + (y - eay) * dy) / safe, 0.0, 1.0)
        d = np.where(
            zero,
            np.hypot(x - eax, y - eay),
            np.hypot(x - (eax + t * dx), y - (eay + t * dy)),
        )
        hit = (d - radius) <= reach
        mask[row[hit], col[hit]] = True
        start = stop
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
