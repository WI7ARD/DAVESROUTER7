"""What a zone refill does to connectivity: a conservative model.

The router treats foreign zone fills as refillable (KiCad workflow: route, then
refill). A stored fill that new foreign copper now crosses is *stale*: after the
refill KiCad cuts it around that copper, and a pour that used to join several
pads can split into islands (Real100 K037 GND: "Missing connection between
Zone [GND] and Zone [GND]"). Counting the stale fill as connectivity claims
connections KiCad will not have.

This module estimates the refilled pour from the stored fill, never claiming
more than KiCad will produce:

1. A fill is *disturbed* when foreign copper violates the zone clearance against
   it by more than :data:`DISTURB_TOL_NM` (a correctly filled pour never does).
   Undisturbed fills keep their exact polygon.
2. For a disturbed fill: rasterise the stored polygon (cells fully inside), remove
   every cell near disturbing copper grown by its clearance (refill can only
   remove copper there; copper KiCad would *add* elsewhere is ignored).
3. KiCad drops necks thinner than ``min_thickness`` (deflate/inflate by half of
   it), so components are found on the mask eroded by that half (4-connected);
   their labels then spread back over the uneroded copper. Copper reached by
   two labels is a neck and is dropped. Thin copper (thermal spokes) joins a
   component only when it touches exactly one.
4. Same-net items attach to the components their copper touches.

Result per disturbed fill: groups of same-net item uids that stay connected
through it. Validated against KiCad refills with the DRC oracle.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from pcbrouter.domain.geometry import BoundingBox
from pcbrouter.domain.units import Nm
from pcbrouter.geometry.board import BoardGeometry, CopperItem, ItemKind
from pcbrouter.geometry.clearance import touches
from pcbrouter.geometry.distance import Core, PolygonCore
from pcbrouter.geometry.raster import centers, distance_field, polygon_inside_grid

#: foreign copper closer than (clearance - this) to a stored fill makes it stale
DISTURB_TOL_NM = 20_000
#: raster cell bounds (the cell is min_thickness / 3, clamped, and grown until the
#: fill's bounding box needs at most MAX_CELLS cells)
MIN_CELL_NM = 10_000
MAX_CELLS = 6_000_000
#: KiCad's default zone minimum width when the file has none
DEFAULT_MIN_THICKNESS_NM = 250_000

#: (fill, other item, layer) -> (certain clearance, clearance incl. rules that may apply)
ClearanceFn = Callable[[CopperItem, CopperItem, str], tuple[Nm, Nm]]


@dataclass(frozen=True)
class RefillResult:
    #: same-net item uids grouped by the refilled component they attach to
    groups: tuple[frozenset[str], ...]
    #: foreign items that made the stored fill stale
    disturbed_by: tuple[str, ...]


class RefillModel:
    """Shared by a geometry and its copies; results are cached by content."""

    def __init__(self, clearance: ClearanceFn, key: object = None) -> None:
        self._clearance = clearance
        #: identifies the rules the cached results were computed with
        self.key = key
        self._disturb: dict[tuple[str, str, BoundingBox], bool] = {}
        self._results: dict[tuple[str, frozenset[str]], RefillResult] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ public
    def result(self, geo: BoardGeometry, fill: CopperItem) -> RefillResult | None:
        """``None`` when ``fill`` is not stale (use its stored polygon)."""
        layer = next(iter(fill.layers))
        disturbing = self._disturbing(geo, fill, layer)
        if not disturbing:
            return None
        key = (fill.uid, frozenset(f"{i.uid}@{i.bounds}" for i in disturbing))
        cached = self._results.get(key)
        if cached is not None:
            return cached
        res = self._refill(geo, fill, layer, disturbing)
        with self._lock:
            self._results[key] = res
        return res

    # ------------------------------------------------------------ disturbance
    def _disturbing(self, geo: BoardGeometry, fill: CopperItem, layer: str) -> list[CopperItem]:
        out: list[CopperItem] = []
        shape = fill.shapes[0]
        for other in geo.copper_near(layer, fill.bounds.expanded(2_000_000)):
            if other.net == fill.net and other.net is not None:
                continue
            if other.kind is ItemKind.ZONE_FILL:
                continue  # zone vs zone: priorities, settled by the stored fill
            key = (fill.uid, other.uid, other.bounds)
            hit = self._disturb.get(key)
            if hit is None:
                clr, _ = self._clearance(fill, other, layer)
                reach = max(0, clr - DISTURB_TOL_NM)
                hit = other.bounds.expanded(reach).intersects(fill.bounds) and any(
                    touches(s, shape, reach) for s in other.shapes
                )
                self._disturb[key] = hit
            if hit:
                out.append(other)
        return out

    # ------------------------------------------------------------ raster refill
    def _refill(
        self, geo: BoardGeometry, fill: CopperItem, layer: str, disturbing: list[CopperItem]
    ) -> RefillResult:
        zone = next((z for z in geo.board.zones if z.id == fill.source_id), None)
        min_th = (zone.min_thickness if zone else None) or DEFAULT_MIN_THICKNESS_NM
        box = fill.bounds
        cell = max(MIN_CELL_NM, min_th // 3)
        while (box.width / cell + 1) * (box.height / cell + 1) > MAX_CELLS:
            cell = int(cell * 1.25) + 1
        nx = int(box.width // cell) + 1
        ny = int(box.height // cell) + 1
        xc = centers(box.min_x, cell, nx)
        yc = centers(box.min_y, cell, ny)
        core = fill.shapes[0].core
        assert isinstance(core, PolygonCore)
        inside = polygon_inside_grid(xc, yc, core.polygon)
        mask = _erode(inside, 1)  # cells fully inside the stored polygon
        half_diag = cell * math.sqrt(0.5)
        for other in disturbing:
            _, clr = self._clearance(fill, other, layer)
            for s in other.shapes:
                _clear_near(mask, xc, yc, s.core, s.radius + clr + half_diag)
        k = math.ceil((min_th / 2) / cell) + 1
        core_cells = _erode_disk(mask, k)
        labels = _label4(core_cells)
        labels = _spread(labels, mask, k + 1)
        labels = _attach_thin(labels, mask)
        groups: dict[int, set[str]] = {}
        for item in geo.copper_near(layer, box):
            if item.net != fill.net or item.uid == fill.uid:
                continue
            if item.kind is ItemKind.ZONE_FILL:
                continue
            for lab in _labels_touching(labels, xc, yc, item, layer, cell):
                groups.setdefault(lab, set()).add(item.uid)
        return RefillResult(
            tuple(frozenset(g) for _, g in sorted(groups.items()) if g),
            tuple(sorted(i.uid for i in disturbing)),
        )


# ---------------------------------------------------------------- raster helpers
BoolGrid = npt.NDArray[np.bool_]
IntGrid = npt.NDArray[np.int32]


def _shift[A: np.ndarray](a: A, dy: int, dx: int, fill: object) -> A:
    out: A = np.full_like(a, fill)
    h, w = a.shape
    ys = slice(max(dy, 0), h + min(dy, 0))
    yd = slice(max(-dy, 0), h + min(-dy, 0))
    xs = slice(max(dx, 0), w + min(dx, 0))
    xd = slice(max(-dx, 0), w + min(-dx, 0))
    out[ys, xs] = a[yd, xd]
    return out


def _erode(mask: BoolGrid, k: int) -> BoolGrid:
    """Square erosion by ``k`` cells (outside the grid counts as empty)."""
    out = mask.copy()
    for dy in range(-k, k + 1):
        for dx in range(-k, k + 1):
            if dy or dx:
                out &= _shift(mask, dy, dx, False)
    return out


def _erode_disk(mask: BoolGrid, k: int) -> BoolGrid:
    out = mask.copy()
    for dy in range(-k, k + 1):
        for dx in range(-k, k + 1):
            if (dy or dx) and dy * dy + dx * dx <= k * k:
                out &= _shift(mask, dy, dx, False)
    return out


def _clear_near(
    mask: BoolGrid,
    xc: npt.NDArray[np.float64],
    yc: npt.NDArray[np.float64],
    core: Core,
    reach: float,
) -> None:
    b = core.bounds.expanded(math.ceil(reach) + 1)
    c0, c1 = np.searchsorted(xc, b.min_x, "left"), np.searchsorted(xc, b.max_x, "right")
    r0, r1 = np.searchsorted(yc, b.min_y, "left"), np.searchsorted(yc, b.max_y, "right")
    if c0 >= c1 or r0 >= r1:
        return
    xs, ys = np.meshgrid(xc[c0:c1], yc[r0:r1])
    d = distance_field(core, xs, ys, b)
    mask[r0:r1, c0:c1] &= d > reach


def _label4(mask: BoolGrid) -> IntGrid:
    """4-connected component labels (0 = empty), by run union-find."""
    h, w = mask.shape
    labels = np.zeros((h, w), dtype=np.int32)
    parent: list[int] = [0]

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    prev: list[tuple[int, int, int]] = []
    for r in range(h):
        row = mask[r]
        if not row.any():
            prev = []
            continue
        d = np.diff(np.concatenate(([0], row.view(np.int8), [0])))
        starts, stops = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
        cur: list[tuple[int, int, int]] = []
        j = 0
        for s, e in zip(starts.tolist(), stops.tolist(), strict=True):
            lab = 0
            while j < len(prev) and prev[j][1] <= s:
                j += 1
            k = j
            while k < len(prev) and prev[k][0] < e:
                other = find(prev[k][2])
                if lab == 0:
                    lab = other
                elif other != lab:
                    parent[max(lab, other)] = min(lab, other)
                    lab = min(lab, other)
                k += 1
            if lab == 0:
                lab = len(parent)
                parent.append(lab)
            cur.append((s, e, lab))
            labels[r, s:e] = lab
        prev = cur
    roots = np.array([find(i) for i in range(len(parent))], dtype=np.int32)
    return roots[labels]


_NEIGHBOURS8 = [(dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx]


def _spread(labels: IntGrid, mask: BoolGrid, rounds: int) -> IntGrid:
    """Grow labels over ``mask`` for ``rounds`` steps; a cell reached by two
    different labels is a neck and becomes -1 (never joins anything)."""
    lab = labels.copy()
    for _ in range(rounds):
        free = mask & (lab == 0)
        if not free.any():
            break
        lo = np.full(lab.shape, np.iinfo(np.int32).max, dtype=np.int32)
        hi = np.zeros(lab.shape, dtype=np.int32)
        for dy, dx in _NEIGHBOURS8:
            n = _shift(lab, dy, dx, 0)
            pos = n > 0
            lo = np.where(pos, np.minimum(lo, n), lo)
            hi = np.where(pos, np.maximum(hi, n), hi)
            hi = np.where(n < 0, -1, hi)  # touching a neck taints the cell
        reach = free & (hi != 0)
        same = reach & (lo == hi)
        lab[same] = hi[same]
        lab[reach & ~same] = -1
    return lab


def _attach_thin(labels: IntGrid, mask: BoolGrid) -> IntGrid:
    """Copper still unlabelled (thin parts: spokes, slivers) joins the one
    component it touches; if it touches none or several, it stays out."""
    thin = mask & (labels == 0)
    if not thin.any():
        return labels
    regions = _label4(thin)
    out = labels.copy()
    touching: dict[int, set[int]] = {}
    for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        n = _shift(labels, dy, dx, 0)
        sel = (regions > 0) & (n != 0)
        for reg, lab in zip(regions[sel].tolist(), n[sel].tolist(), strict=True):
            touching.setdefault(reg, set()).add(lab)
    for reg, labs in touching.items():
        if len(labs) == 1 and next(iter(labs)) > 0:
            out[regions == reg] = next(iter(labs))
    return out


def _labels_touching(
    labels: IntGrid,
    xc: npt.NDArray[np.float64],
    yc: npt.NDArray[np.float64],
    item: CopperItem,
    layer: str,
    cell: int,
) -> set[int]:
    """Component labels whose cells lie on ``item``'s connecting copper."""
    found: set[int] = set()
    for s in item.contact_shapes(layer, to_zone=True):
        b = s.bounds.expanded(2 * cell)
        c0, c1 = np.searchsorted(xc, b.min_x, "left"), np.searchsorted(xc, b.max_x, "right")
        r0, r1 = np.searchsorted(yc, b.min_y, "left"), np.searchsorted(yc, b.max_y, "right")
        if c0 >= c1 or r0 >= r1:
            continue
        xs, ys = np.meshgrid(xc[c0:c1], yc[r0:r1])
        on = distance_field(s.core, xs, ys, b) <= s.radius + 1.5 * cell
        sub = labels[r0:r1, c0:c1][on]
        found.update(int(v) for v in np.unique(sub) if v > 0)
    return found


def attach_refill(geo: BoardGeometry, clearance: ClearanceFn, key: object = None) -> None:
    """Make connectivity on ``geo`` (and its copies) refill-aware. A model for the
    same rules (``key``) is kept, with its caches."""
    current = geo.refill
    if isinstance(current, RefillModel) and current.key == key and key is not None:
        current._clearance = clearance
        return
    geo.refill = RefillModel(clearance, key)
