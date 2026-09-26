"""Compiled search grid: the Stage 3 occupancy maps turned into flat arrays.

One :class:`SearchGrid` serves one (net, width, via size, layers, window). For each
routing layer it holds:

* ``passable`` — cell centre is a legal place for the track *centreline*: FREE or
  own-net copper in the width-dependent occupancy map (obstacles inflated by half
  the width plus the resolved clearance, keepouts, holes, board edge, cutouts);
* ``near`` — passable cell adjacent to a non-passable one (clearance-proximity cost);
* ``via_ok`` — a through via of the requested size may be centred here: legal on
  *every* copper layer it spans (via-mode occupancy, keepouts forbidding vias).

Arrays are NumPy for building and Python ``bytes`` for the search hot loop.
The grid is a routing aid: every final segment/via is re-checked by the exact
validator.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt

from pcbrouter.board_engine import BoardEngine
from pcbrouter.domain.geometry import BoundingBox, Point
from pcbrouter.domain.units import Nm
from pcbrouter.geometry.raster import centers, distance_field
from pcbrouter.geometry.shapes import Shape
from pcbrouter.routing.occupancy import CellState, GridSpec
from pcbrouter.rules.model import ItemType

type BoolGrid = npt.NDArray[np.bool_]
type FloatGrid = npt.NDArray[np.float64]


class GridCancelled(Exception):  # noqa: N818 - a stop signal, not an error
    """Raised when grid compilation is cancelled (maps to CANCELLED, not an error)."""


@dataclass
class SearchGrid:
    spec: GridSpec
    layers: tuple[str, ...]
    passable: list[BoolGrid]
    near: list[BoolGrid]
    via_ok: BoolGrid | None
    #: optional extra cost per cell as a fraction of the step (>= 0), per layer
    penalty: list[FloatGrid | None]
    #: multiplicative step factor per cell (soft "prefer" corridors <= 1), per layer
    factor: list[FloatGrid | None]
    notes: list[str] = field(default_factory=list)
    rules_complete: bool = True

    @property
    def nx(self) -> int:
        return self.spec.nx

    @property
    def ny(self) -> int:
        return self.spec.ny

    @property
    def n(self) -> int:
        return self.spec.nx * self.spec.ny

    def index_of(self, p: Point) -> int | None:
        rc = self.spec.index_of(p)
        return None if rc is None else rc[0] * self.nx + rc[1]

    def center(self, idx: int) -> Point:
        return self.spec.cell_center(idx // self.nx, idx % self.nx)

    def cells_within(self, shape: Shape, reach: float = 0.0) -> npt.NDArray[np.int64]:
        """Flat indices of cells whose centre lies within ``reach`` of the shape."""
        s = self.spec
        box = shape.bounds.expanded(math.ceil(reach))
        c0 = max(0, (box.min_x - s.origin_x) // s.cell)
        c1 = min(s.nx, (box.max_x - s.origin_x) // s.cell + 1)
        r0 = max(0, (box.min_y - s.origin_y) // s.cell)
        r1 = min(s.ny, (box.max_y - s.origin_y) // s.cell + 1)
        if c0 >= c1 or r0 >= r1:
            return np.zeros(0, dtype=np.int64)
        xs = centers(s.origin_x + c0 * s.cell, s.cell, c1 - c0)
        ys = centers(s.origin_y + r0 * s.cell, s.cell, r1 - r0)
        gx, gy = np.meshgrid(xs, ys)
        dist = distance_field(shape.core, gx, gy, box) - shape.radius
        rows, cols = np.nonzero(dist <= reach)
        return ((rows + r0) * s.nx + (cols + c0)).astype(np.int64)

    def block(self, layer_index: int, cells: npt.NDArray[np.int64]) -> None:
        """Make cells impassable (repair after a failed exact validation)."""
        self.passable[layer_index].reshape(-1)[cells] = False
        if self.via_ok is not None:
            self.via_ok.reshape(-1)[cells] = False

    def block_via(self, cells: npt.NDArray[np.int64]) -> None:
        if self.via_ok is not None:
            self.via_ok.reshape(-1)[cells] = False

    def add_penalty(self, layer_index: int, cells: npt.NDArray[np.int64], amount: float) -> None:
        pen = self.penalty[layer_index]
        if pen is None:
            pen = np.zeros((self.ny, self.nx), dtype=np.float64)
            self.penalty[layer_index] = pen
        pen.reshape(-1)[cells] += amount


def _near(passable: BoolGrid) -> BoolGrid:
    blocked = ~passable
    grown = blocked.copy()
    grown[1:, :] |= blocked[:-1, :]
    grown[:-1, :] |= blocked[1:, :]
    grown[:, 1:] |= blocked[:, :-1]
    grown[:, :-1] |= blocked[:, 1:]
    return passable & grown


def _passable(cells: npt.NDArray[np.uint8]) -> BoolGrid:
    return np.asarray((cells == CellState.FREE) | (cells == CellState.SAME_NET))


def compile_grid(
    engine: BoardEngine,
    net: str,
    layers: tuple[str, ...],
    width: Nm,
    via_diameter: Nm | None,
    cell: Nm,
    window: BoundingBox | None = None,
    progress: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
) -> SearchGrid:
    """``progress`` (optional) is told which layer is being rasterised."""

    def _cancelled() -> bool:
        return cancel is not None and cancel.is_set()

    notes: list[str] = []
    complete = True
    passable: list[BoolGrid] = []
    spec: GridSpec | None = None
    total = len(layers) + (len(engine.geometry.copper_layers) if via_diameter else 0)
    step = 0

    def tell(what: str) -> None:
        nonlocal step
        step += 1
        if progress is not None:
            progress(f"grid {step}/{total}: {what}")

    for layer in layers:
        if _cancelled():
            raise GridCancelled()
        tell(f"tracks on {layer}")
        occ = engine.occupancy(layer, net, width, cell, window)
        spec = occ.spec
        complete &= occ.rules_complete
        notes += [f"{layer}: {n}" for n in occ.notes]
        passable.append(_passable(occ.cells))
    assert spec is not None
    via_ok: BoolGrid | None = None
    if via_diameter is not None and len(layers) > 1:
        via_ok = np.ones((spec.ny, spec.nx), dtype=np.bool_)
        for layer in engine.geometry.copper_layers:  # a through via spans every layer
            if _cancelled():
                raise GridCancelled()
            tell(f"vias on {layer}")
            occ = engine.occupancy(layer, net, via_diameter, cell, window, ItemType.VIA)
            complete &= occ.rules_complete
            via_ok &= _passable(occ.cells)
    return SearchGrid(
        spec=spec,
        layers=layers,
        passable=passable,
        near=[_near(p) for p in passable],
        via_ok=via_ok,
        penalty=[None] * len(layers),
        factor=[None] * len(layers),
        notes=list(dict.fromkeys(notes)),
        rules_complete=complete,
    )
