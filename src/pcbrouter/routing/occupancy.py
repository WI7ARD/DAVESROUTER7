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
import time
from dataclasses import dataclass, field
from enum import IntEnum

import numpy as np
import numpy.typing as npt

from pcbrouter.domain.geometry import BoundingBox, Point
from pcbrouter.domain.units import Nm
from pcbrouter.geometry.board import BoardGeometry, ItemKind, RegionStatus
from pcbrouter.geometry.errors import GeometryError
from pcbrouter.geometry.raster import centers, distance_field, polygon_inside
from pcbrouter.geometry.shapes import Shape
from pcbrouter.routing.collision import item_type_of
from pcbrouter.rules.model import ItemType
from pcbrouter.rules.resolver import RuleResolver

log = logging.getLogger(__name__)

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

    def mark(self, shape: Shape, reach: float, state: CellState) -> None:
        """Mark cells whose centre lies within ``reach`` of the shape's copper."""
        box = shape.bounds.expanded(math.ceil(reach))
        win = self.window(box)
        if win is None:
            return
        rows, cols = win
        xs, ys = np.meshgrid(self.xs[cols], self.ys[rows])
        dist = distance_field(shape.core, xs, ys, box) - shape.radius
        sub = self.cells[rows, cols]
        np.maximum(
            sub, np.where(dist <= reach, np.uint8(state), np.uint8(0)).astype(np.uint8), out=sub
        )


def build_occupancy(
    geo: BoardGeometry,
    resolver: RuleResolver,
    layer: str,
    net: str | None,
    width: Nm,
    cell: Nm,
    bounds: BoundingBox | None = None,
) -> OccupancyMap:
    """Rasterise one layer for a candidate track of ``net`` and ``width``."""
    t0 = time.perf_counter()
    if not geo.is_copper_layer(layer):
        raise GeometryError(f"{layer} is not a copper layer", f"{layer} is not a copper layer.")
    if width <= 0:
        raise GeometryError("track width must be positive")
    spec = grid_spec_for(geo, layer, cell, bounds)
    cells = np.zeros((spec.ny, spec.nx), dtype=np.uint8)
    raster = _Rasterizer(spec, cells)
    r_track = width / 2
    notes: list[str] = []
    complete = True

    # Board region: outside / cutout cells.
    if geo.region.status is RegionStatus.KNOWN:
        xs, ys = np.meshgrid(raster.xs, raster.ys)
        inside = np.zeros(cells.shape, dtype=np.bool_)
        for loop in geo.region.loops:
            inside ^= polygon_inside(xs, ys, loop)
        cells[~inside] = CellState.OUTSIDE_BOARD
        edge_req = resolver.resolve_edge_clearance(net, ItemType.TRACK, layer)
        if edge_req.value is None:
            complete = False
            notes.append("copper-to-edge clearance unknown: edge band = track half-width only")
        reach = r_track + (edge_req.value or 0)
        for edge in geo.edges.values():
            raster.mark(edge.shape, reach, CellState.EDGE)
    else:
        cells[:] = CellState.UNKNOWN
        complete = False
        notes.append(f"board outline {geo.region.status.value}: cells cannot be classified")
        return OccupancyMap(spec, net, width, cells, complete, notes, time.perf_counter() - t0)

    # Keepouts forbidding tracks.
    for k in geo.keepouts.values():
        if layer in k.layers and k.rules.tracks:
            raster.mark(k.shape, r_track, CellState.KEEPOUT)

    # Mechanical holes (and foreign holes on layers without their copper).
    hole_req = resolver.resolve_hole_clearance(net, ItemType.TRACK, layer)
    for h in geo.holes.values():
        owner = geo.copper.get(h.owner_uid) if h.owner_uid else None
        if owner is not None and (owner.net == net or layer in owner.layers):
            continue
        if hole_req.value is None:
            complete = False
        raster.mark(h.shape, r_track + (hole_req.value or 0), CellState.BLOCKED)

    # Copper.
    unknown_pairs = 0
    for item in geo.copper_near(layer, spec.bounds):
        if net is not None and item.net == net:
            for s in item.shapes:
                raster.mark(s, r_track, CellState.SAME_NET)
            continue
        if item.kind is ItemKind.ZONE_FILL:
            continue  # refillable: consistent with the collision engine's default
        req = resolver.resolve_clearance(
            net, item.net, ItemType.TRACK, item_type_of(item.kind), layer,
            None, item.local_clearance, None, item.label,
        )  # fmt: skip
        if req.value is None:
            unknown_pairs += 1
        for s in item.shapes:
            raster.mark(s, r_track + (req.value or 0), CellState.FOREIGN_NET)
    if unknown_pairs:
        complete = False
        notes.append(f"clearance unknown for {unknown_pairs} object(s): only overlap blocked")
    if resolver.ruleset.critical_unsupported:
        complete = False
        notes.append("unsupported critical rules present: map may be optimistic")
    result = OccupancyMap(spec, net, width, cells, complete, notes, time.perf_counter() - t0)
    log.info(
        "occupancy.build layer=%s net=%s width_um=%d cell_um=%d cells=%d free=%.2f "
        "complete=%s ms=%.1f",
        layer, net, width // 1000, cell // 1000, spec.cell_count, result.free_fraction,
        complete, result.elapsed_s * 1e3,
    )  # fmt: skip
    return result
