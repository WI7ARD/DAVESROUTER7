"""Layer-specific routing occupancy grids (Stage 4 preparation; no path search).

A cell answers: "may the *centreline* of a track of net N, width W, on layer L pass
through this cell centre?" Obstacles are inflated by the candidate's half-width plus
the clearance resolved for (N, obstacle) — so the map depends on net, width, layer
and rules, and one map is never reused for a different width.

Storage is a compact ``uint8`` NumPy array (1 byte per cell; a 100x100 mm board at
0.1 mm is 1 MB), row-major ``[row = y, col = x]``, ready for a future GPU backend.

Cell states (higher value wins where several apply)::

    FREE < SAME_NET < FOREIGN_NET < BLOCKED < KEEPOUT < EDGE < OUTSIDE_BOARD

``UNKNOWN`` marks cells that could not be classified (no board outline). Cells are
sampled at their centres: the map is a routing aid; the exact validator
(:class:`~pcbrouter.routing.validator.RouteValidator`) stays authoritative.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

import numpy as np
import numpy.typing as npt

from pcbrouter.domain.geometry import BoundingBox, Point
from pcbrouter.domain.units import Nm
from pcbrouter.geometry.board import BoardGeometry, ItemKind, RegionStatus
from pcbrouter.geometry.distance import PointCore, PolygonCore, SegmentCore
from pcbrouter.geometry.errors import GeometryError
from pcbrouter.geometry.raster import (
    FAST_POLYGON_EDGES,
    centers,
    distance_field,
    polygon_inside_grid,
    polygon_near_mask,
)
from pcbrouter.geometry.shapes import Shape
from pcbrouter.routing.collision import item_type_of
from pcbrouter.rules.model import UNBOUNDED, ItemType, ResolvedValue
from pcbrouter.rules.resolver import RuleResolver

log = logging.getLogger(__name__)

#: Mark point/segment obstacles in vectorised batches (identical cells; tests
#: compare against the per-shape path by turning this off).
BATCH_MARKING = True
#: (shape, cell) pairs per vectorised batch
_BATCH_PAIRS = 2_000_000
#: polygons with at most this many edges (pads) are batched too
_BATCH_POLY_EDGES = 32
#: Refuse grids above this many cells (memory/time guard); pick a coarser resolution.
MAX_CELLS = 40_000_000
GRID_RESOLUTIONS_MM = (0.10, 0.20, 0.25, 0.50)


class CellState(IntEnum):
    FREE = 0
    SAME_NET = 1
    FOREIGN_NET = 2
    BLOCKED = 3  # drills / mechanical holes
    KEEPOUT = 4
    EDGE = 5  # within the board-edge clearance
    OUTSIDE_BOARD = 6
    UNKNOWN = 7

    @property
    def label(self) -> str:
        return self.name.replace("_", " ").lower()


@dataclass(frozen=True, slots=True)
class GridSpec:
    origin_x: Nm
    origin_y: Nm
    cell: Nm
    nx: int
    ny: int
    layer: str

    @property
    def cell_count(self) -> int:
        return self.nx * self.ny

    def index_of(self, p: Point) -> tuple[int, int] | None:
        col = (p.x - self.origin_x) // self.cell
        row = (p.y - self.origin_y) // self.cell
        if 0 <= col < self.nx and 0 <= row < self.ny:
            return row, col
        return None

    def cell_center(self, row: int, col: int) -> Point:
        return Point(
            self.origin_x + col * self.cell + self.cell // 2,
            self.origin_y + row * self.cell + self.cell // 2,
        )

    @property
    def bounds(self) -> BoundingBox:
        return BoundingBox(
            self.origin_x,
            self.origin_y,
            self.origin_x + self.nx * self.cell,
            self.origin_y + self.ny * self.cell,
        )


@dataclass
class OccupancyMap:
    spec: GridSpec
    net: str | None
    width: Nm
    cells: npt.NDArray[np.uint8]
    #: False when a rule needed to inflate obstacles was unknown (see ``notes``).
    rules_complete: bool = True
    notes: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    def state_at(self, p: Point) -> CellState:
        idx = self.spec.index_of(p)
        if idx is None:
            return CellState.OUTSIDE_BOARD
        return CellState(int(self.cells[idx]))

    def counts(self) -> dict[str, int]:
        values, counts = np.unique(self.cells, return_counts=True)
        return {CellState(int(v)).label: int(c) for v, c in zip(values, counts, strict=True)}

    @property
    def free_fraction(self) -> float:
        inside = self.cells != CellState.OUTSIDE_BOARD
        total = int(inside.sum())
        return float((self.cells == CellState.FREE).sum()) / total if total else 0.0


def grid_spec_for(
    geo: BoardGeometry, layer: str, cell: Nm, bounds: BoundingBox | None = None
) -> GridSpec:
    if cell <= 0:
        raise GeometryError(f"grid resolution must be positive (got {cell} nm)")
    box = bounds or geo.board.bounds
    if box is None:
        raise GeometryError("board has no extent (no outline and no copper)", "The board is empty.")
    nx = max(1, math.ceil(box.width / cell))
    ny = max(1, math.ceil(box.height / cell))
    if nx * ny > MAX_CELLS:
        raise GeometryError(
            f"grid of {nx}x{ny} cells exceeds the {MAX_CELLS:,}-cell limit",
            f"A {cell / 1e6:g} mm grid is too fine for this board ({nx * ny:,} cells). "
            "Choose a coarser resolution.",
        )
    return GridSpec(box.min_x, box.min_y, cell, nx, ny, layer)


class _Rasterizer:
    def __init__(self, spec: GridSpec, cells: npt.NDArray[np.uint8]) -> None:
        self.spec = spec
        self.cells = cells
        self.xs = centers(spec.origin_x, spec.cell, spec.nx)
        self.ys = centers(spec.origin_y, spec.cell, spec.ny)

    def window(self, box: BoundingBox) -> tuple[slice, slice] | None:
        s = self.spec
        c0 = max(0, (box.min_x - s.origin_x) // s.cell)
        c1 = min(s.nx, (box.max_x - s.origin_x) // s.cell + 1)
        r0 = max(0, (box.min_y - s.origin_y) // s.cell)
        r1 = min(s.ny, (box.max_y - s.origin_y) // s.cell + 1)
        if c0 >= c1 or r0 >= r1:
            return None
        return slice(r0, r1), slice(c0, c1)

    def mark(
        self,
        shape: Shape,
        reach: float,
        state: CellState,
        *,
        overwrite_below: CellState | None = None,
    ) -> None:
        """Mark cells whose centre lies within ``reach`` of the shape's copper.

        Marking takes the maximum (most severe wins), except that own-net
        copper overwrites up to ``overwrite_below``: a centreline over its own
        pad is legal even inside a foreign clearance band, and without this
        escape source cells read as enclosed (spurious NO_ESCAPE). Harder
        states (holes, keepouts, edge, outside) always win.
        """
        box = shape.bounds.expanded(math.ceil(reach))
        win = self.window(box)
        if win is None:
            return
        rows, cols = win
        core = shape.core
        if isinstance(core, PolygonCore) and core.polygon.edge_count > FAST_POLYGON_EDGES:
            # big polygons (zone fills): same cells, without a full-window
            # distance field per edge (that was minutes per layer for a GND pour)
            hit = polygon_near_mask(
                self.xs[cols], self.ys[rows], core.polygon, float(shape.radius), float(reach)
            )
        else:
            xs, ys = np.meshgrid(self.xs[cols], self.ys[rows])
            hit = (distance_field(core, xs, ys, box) - shape.radius) <= reach
        sub = self.cells[rows, cols]
        if overwrite_below is None:
            np.maximum(sub, np.where(hit, np.uint8(state), np.uint8(0)).astype(np.uint8), out=sub)
        else:
            sub[hit & (sub <= np.uint8(overwrite_below))] = np.uint8(state)

    def mark_batch(
        self,
        items: list[tuple[Shape, float]],
        state: CellState,
        *,
        overwrite_below: CellState | None = None,
    ) -> None:
        """:meth:`mark` for many shapes at once: point and segment shapes are
        evaluated together as (shape, cell) pairs with the same float expressions
        as :func:`distance_field`, so the cells are identical; polygons go through
        :meth:`mark`. All shapes of one call share ``state`` (marking is a max, or
        an overwrite of cells at or below ``overwrite_below``: order-free)."""
        if not BATCH_MARKING:
            for shape, reach in items:
                self.mark(shape, reach, state, overwrite_below=overwrite_below)
            return
        s = self.spec
        rows0: list[int] = []
        rows1: list[int] = []
        cols0: list[int] = []
        cols1: list[int] = []
        coords: list[tuple[float, float, float, float, float, float]] = []
        polys: list[tuple[Shape, float]] = []
        for shape, reach in items:
            core = shape.core
            if isinstance(core, PolygonCore) and core.polygon.edge_count <= _BATCH_POLY_EDGES:
                polys.append((shape, reach))
                continue
            if not isinstance(core, (PointCore, SegmentCore)):
                self.mark(shape, reach, state, overwrite_below=overwrite_below)
                continue
            box = shape.bounds.expanded(math.ceil(reach))
            c0 = max(0, (box.min_x - s.origin_x) // s.cell)
            c1 = min(s.nx, (box.max_x - s.origin_x) // s.cell + 1)
            r0 = max(0, (box.min_y - s.origin_y) // s.cell)
            r1 = min(s.ny, (box.max_y - s.origin_y) // s.cell + 1)
            if c0 >= c1 or r0 >= r1:
                continue
            if isinstance(core, PointCore):
                ax = bx = float(core.p.x)
                ay = by = float(core.p.y)
            else:
                ax, ay, bx, by = (
                    float(core.a.x), float(core.a.y), float(core.b.x), float(core.b.y)
                )  # fmt: skip
            rows0.append(r0)
            rows1.append(r1)
            cols0.append(c0)
            cols1.append(c1)
            coords.append((ax, ay, bx, by, float(shape.radius), float(reach)))
        if polys:
            self._mark_small_polygons(polys, state, overwrite_below)
        if not coords:
            return
        r0a, r1a = np.array(rows0), np.array(rows1)
        c0a, c1a = np.array(cols0), np.array(cols1)
        arr = np.array(coords, dtype=np.float64)
        nc, nr = c1a - c0a, r1a - r0a
        sizes = nc * nr
        start = 0
        while start < len(arr):
            stop = start + max(1, int(np.searchsorted(np.cumsum(sizes[start:]), _BATCH_PAIRS)))
            n = sizes[start:stop]
            k = np.repeat(np.arange(start, stop), n)
            offs = np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n)
            col = c0a[k] + offs % nc[k]
            row = r0a[k] + offs // nc[k]
            x, y = self.xs[col], self.ys[row]
            ax, ay, bx, by = arr[k, 0], arr[k, 1], arr[k, 2], arr[k, 3]
            dx, dy = bx - ax, by - ay
            len2 = dx * dx + dy * dy
            zero = len2 == 0.0
            t = np.clip(((x - ax) * dx + (y - ay) * dy) / np.where(zero, 1.0, len2), 0.0, 1.0)
            d = np.where(
                zero, np.hypot(x - ax, y - ay), np.hypot(x - (ax + t * dx), y - (ay + t * dy))
            )
            hit = (d - arr[k, 4]) <= arr[k, 5]
            self._apply(row[hit], col[hit], state, overwrite_below)
            start = stop

    def _apply(
        self,
        hr: npt.NDArray[np.int64],
        hc: npt.NDArray[np.int64],
        state: CellState,
        overwrite_below: CellState | None,
    ) -> None:
        cur = self.cells[hr, hc]
        if overwrite_below is None:
            self.cells[hr, hc] = np.maximum(cur, np.uint8(state))
        else:
            ok = cur <= np.uint8(overwrite_below)
            self.cells[hr[ok], hc[ok]] = np.uint8(state)

    def _mark_small_polygons(
        self,
        items: list[tuple[Shape, float]],
        state: CellState,
        overwrite_below: CellState | None,
    ) -> None:
        """Small polygons (pads) in one pass over (polygon, cell, edge) triples:
        the distance is the minimum over the edges, and cells inside the polygon
        count as distance 0 (the same even-odd crossing expression as
        ``polygon_inside``), exactly as :func:`distance_field` computes it."""
        s = self.spec
        wins: list[tuple[int, int, int, int]] = []
        edges: list[npt.NDArray[np.float64]] = []
        params: list[tuple[float, float]] = []
        for shape, reach in items:
            box = shape.bounds.expanded(math.ceil(reach))
            c0 = max(0, (box.min_x - s.origin_x) // s.cell)
            c1 = min(s.nx, (box.max_x - s.origin_x) // s.cell + 1)
            r0 = max(0, (box.min_y - s.origin_y) // s.cell)
            r1 = min(s.ny, (box.max_y - s.origin_y) // s.cell + 1)
            if c0 >= c1 or r0 >= r1:
                continue
            assert isinstance(shape.core, PolygonCore)
            pts = np.array([(p.x, p.y) for p in shape.core.polygon.points], dtype=np.float64)
            e = np.empty((len(pts), 4))
            e[:, 0], e[:, 1] = pts[:, 0], pts[:, 1]
            e[:, 2], e[:, 3] = np.roll(pts[:, 0], -1), np.roll(pts[:, 1], -1)
            wins.append((r0, r1, c0, c1))
            edges.append(e)
            params.append((float(shape.radius), float(reach)))
        if not wins:
            return
        w = np.array(wins)
        nr, nc = w[:, 1] - w[:, 0], w[:, 3] - w[:, 2]
        n_cells = nr * nc
        n_edges = np.array([len(e) for e in edges])
        all_edges = np.concatenate(edges)
        edge_start = np.cumsum(n_edges) - n_edges
        prm = np.array(params)
        start = 0
        while start < len(w):
            work = n_cells[start:] * n_edges[start:]
            stop = start + max(1, int(np.searchsorted(np.cumsum(work), _BATCH_PAIRS)))
            ks = np.arange(start, stop)
            # one entry per (polygon, cell)
            kc = np.repeat(ks, n_cells[ks])
            offs = np.arange(len(kc)) - np.repeat(np.cumsum(n_cells[ks]) - n_cells[ks], n_cells[ks])
            col = w[kc, 2] + offs % nc[kc]
            row = w[kc, 0] + offs // nc[kc]
            # one entry per (polygon, cell, edge)
            ne = n_edges[kc]
            cell_id = np.repeat(np.arange(len(kc)), ne)
            eoff = np.arange(int(ne.sum())) - np.repeat(np.cumsum(ne) - ne, ne)
            ed = all_edges[edge_start[kc][cell_id] + eoff]
            x, y = self.xs[col][cell_id], self.ys[row][cell_id]
            ax, ay, bx, by = ed[:, 0], ed[:, 1], ed[:, 2], ed[:, 3]
            dx, dy = bx - ax, by - ay
            len2 = dx * dx + dy * dy
            zero = len2 == 0.0
            t = np.clip(((x - ax) * dx + (y - ay) * dy) / np.where(zero, 1.0, len2), 0.0, 1.0)
            d = np.where(
                zero, np.hypot(x - ax, y - ay), np.hypot(x - (ax + t * dx), y - (ay + t * dy))
            )
            flat = ay == by
            crosses = ((ay > y) != (by > y)) & ~flat
            x_int = ax + (y - ay) * (bx - ax) / np.where(flat, 1.0, by - ay)
            odd = (crosses & (x < x_int)).astype(np.int64)
            bounds = np.cumsum(ne) - ne
            dmin = np.minimum.reduceat(d, bounds)
            inside = (np.add.reduceat(odd, bounds) & 1).astype(bool)
            hit = inside | ((dmin - prm[kc, 0]) <= prm[kc, 1])
            self._apply(row[hit], col[hit], state, overwrite_below)
            start = stop


def enforced_clearance(resolver: RuleResolver, req: ResolvedValue) -> Nm | None:
    """The clearance the exact validator will actually demand for ``req``.

    With conservative rule handling a *possibly stricter* bound from an unsupported
    critical rule is enforced too (a smaller gap is RULE_UNKNOWN, which is illegal).
    Inflating the grid by less made the search offer paths the validator then
    refused, repair after repair, until the net failed with VALIDATION (measured on
    a KiCad QA board whose custom rules use ``insideCourtyard()``). An unbounded
    bound cannot be rasterised and is left to the validator."""
    value = req.value
    bound = req.possibly_stricter
    if resolver.conservative and bound is not None and bound < UNBOUNDED:
        return bound if value is None else max(value, bound)
    return value


#: board-outline inside masks per (outline, grid placement): net-independent and
#: stable across commits (the geometry is rebuilt per commit, the outline is not)
_INSIDE_CACHE: dict[tuple[int, int, int, int, int, int], tuple[Any, npt.NDArray[np.bool_]]] = {}
_INSIDE_CACHE_MAX = 32
_INSIDE_LOCK = threading.Lock()  # layers are built on a thread pool


def _inside_board(geo: BoardGeometry, spec: GridSpec, raster: _Rasterizer) -> npt.NDArray[np.bool_]:
    """Cells whose centre lies on board material (even-odd over the outline
    loops), cached: every occupancy build of a job used to recompute it. The
    entry keeps the outline it was made from and is used only on an exact match."""
    outline = geo.board.outline.segments
    key = (hash(outline), spec.origin_x, spec.origin_y, spec.cell, spec.nx, spec.ny)
    with _INSIDE_LOCK:
        hit = _INSIDE_CACHE.get(key)
    if hit is not None and hit[0] == outline:
        return hit[1]
    inside = np.zeros((spec.ny, spec.nx), dtype=np.bool_)
    for loop in geo.region.loops:
        inside ^= polygon_inside_grid(raster.xs, raster.ys, loop)
    inside.setflags(write=False)
    with _INSIDE_LOCK:
        if len(_INSIDE_CACHE) >= _INSIDE_CACHE_MAX:
            _INSIDE_CACHE.pop(next(iter(_INSIDE_CACHE)))
        _INSIDE_CACHE[key] = (outline, inside)
    return inside


def build_occupancy(
    geo: BoardGeometry,
    resolver: RuleResolver,
    layer: str,
    net: str | None,
    width: Nm,
    cell: Nm,
    bounds: BoundingBox | None = None,
    item: ItemType = ItemType.TRACK,
) -> OccupancyMap:
    """Rasterise one layer for a candidate track of ``net`` and ``width``.

    With ``item=ItemType.VIA`` the map is for a via centre of diameter ``width``
    (keepouts forbidding vias, via clearances)."""
    t0 = time.perf_counter()
    if not geo.is_copper_layer(layer):
        raise GeometryError(f"{layer} is not a copper layer", f"{layer} is not a copper layer.")
    if width <= 0:
        raise GeometryError("track width must be positive")
    spec = grid_spec_for(geo, layer, cell, bounds)
    cells = np.zeros((spec.ny, spec.nx), dtype=np.uint8)
    raster = _Rasterizer(spec, cells)
    r_track = width / 2
    # Segment soundness: a cell answers "may the centreline pass through this
    # cell centre", but search edges join adjacent centres, and a point on such
    # an edge can sit up to half a cell diagonal from the nearer endpoint.
    # Obstacles are grown by that margin (track maps only; via centres start
    # no segments) so the grid never promises a path the exact validator
    # refuses — otherwise repair rounds burn blocking the same channel cell
    # by cell until VALIDATION exhaustion.
    seg_margin = cell * math.sqrt(2) / 2 if item is ItemType.TRACK else 0.0
    notes: list[str] = []
    complete = True

    # Board region: outside / cutout cells.
    if geo.region.status is RegionStatus.KNOWN:
        cells[~_inside_board(geo, spec, raster)] = CellState.OUTSIDE_BOARD
        edge_req = resolver.resolve_edge_clearance(net, item, layer)
        if edge_req.value is None:
            complete = False
            notes.append("copper-to-edge clearance unknown: edge band = track half-width only")
        reach = r_track + (enforced_clearance(resolver, edge_req) or 0) + seg_margin
        raster.mark_batch([(edge.shape, reach) for edge in geo.edges.values()], CellState.EDGE)
    else:
        cells[:] = CellState.UNKNOWN
        complete = False
        notes.append(f"board outline {geo.region.status.value}: cells cannot be classified")
        return OccupancyMap(spec, net, width, cells, complete, notes, time.perf_counter() - t0)

    # Keepouts forbidding tracks.
    keep_items: list[tuple[Shape, float]] = []
    for k in geo.keepouts.values():
        forbidden = k.rules.vias if item is ItemType.VIA else k.rules.tracks
        if layer in k.layers and forbidden:
            keep_items.append((k.shape, r_track + seg_margin))
    raster.mark_batch(keep_items, CellState.KEEPOUT)

    # Mechanical holes (and foreign holes on layers without their copper).
    hole_req = resolver.resolve_hole_clearance(net, item, layer)
    hole_clr = enforced_clearance(resolver, hole_req) or 0
    hole_items: list[tuple[Shape, float]] = []
    for h in geo.holes.values():
        owner = geo.copper.get(h.owner_uid) if h.owner_uid else None
        if owner is not None and (owner.net == net or layer in owner.layers):
            continue
        if hole_req.value is None:
            complete = False
        hole_items.append((h.shape, r_track + hole_clr + seg_margin))
    raster.mark_batch(hole_items, CellState.BLOCKED)

    # Copper. Own-net shapes are marked in a second pass so they win ties
    # against foreign clearance bands (escape sources stay passable) while
    # harder states (holes, keepouts, edge) marked above still win.
    unknown_pairs = 0
    own_shapes: list[Shape] = []
    foreign_items: list[tuple[Shape, float]] = []
    for obj in geo.copper_near(layer, spec.bounds):
        if net is not None and obj.net == net:
            own_shapes.extend(obj.shapes)
            continue
        if obj.kind is ItemKind.ZONE_FILL:
            continue  # refillable: consistent with the collision engine's default
        req = resolver.resolve_clearance(
            net, obj.net, item, item_type_of(obj.kind), layer,
            None, obj.local_clearance, None, obj.label,
        )  # fmt: skip
        if req.value is None:
            unknown_pairs += 1
        clr = enforced_clearance(resolver, req) or 0
        foreign_items.extend((s, r_track + clr + seg_margin) for s in obj.shapes)
    raster.mark_batch(foreign_items, CellState.FOREIGN_NET)
    # The overwrite lets a TRACK centreline leave its own pad through a foreign
    # clearance band (escape). A VIA must never get it: its radius is the reach
    # here, so every cell within one via radius of an own pad became "legal" for a
    # via centre even inside a neighbouring pad's clearance band. At 0.5 mm pitch
    # that offered vias overlapping the next pad; the exact validator refused them
    # repair after repair until the net failed (measured on a 4-layer board).
    own_overwrite = CellState.FOREIGN_NET if item is ItemType.TRACK else None
    raster.mark_batch(
        [(s, r_track) for s in own_shapes], CellState.SAME_NET, overwrite_below=own_overwrite
    )
    if unknown_pairs:
        complete = False
        notes.append(f"clearance unknown for {unknown_pairs} object(s): only overlap blocked")
    if resolver.ruleset.critical_unsupported:
        complete = False
        notes.append("unsupported critical rules present: map may be optimistic")
    result = OccupancyMap(spec, net, width, cells, complete, notes, time.perf_counter() - t0)
    if log.isEnabledFor(logging.INFO):  # free_fraction scans the grid twice
        log.info(
            "occupancy.build layer=%s net=%s width_um=%d cell_um=%d cells=%d free=%.2f "
            "complete=%s ms=%.1f",
            layer, net, width // 1000, cell // 1000, spec.cell_count, result.free_fraction,
            complete, result.elapsed_s * 1e3,
        )  # fmt: skip
    return result
