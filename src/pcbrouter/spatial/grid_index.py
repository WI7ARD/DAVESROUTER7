"""Uniform-grid spatial hash — the default :class:`SpatialIndex`.

Why a grid (see docs/geometry_engine.md): PCB objects are small and fairly uniform
in size, coordinates are integers, and a hash grid needs no native dependency
(``rtree`` would add libspatialindex to the Windows installer). Average insert and
query cost is O(objects per cell).

Objects spanning more than :data:`MAX_CELLS_PER_OBJECT` cells (board-sized zone
fills) are kept in a small "large objects" list checked on every query instead of
being copied into thousands of cells.
"""

from __future__ import annotations

from collections.abc import Iterator

from pcbrouter.domain.geometry import BoundingBox
from pcbrouter.spatial.index import SpatialIndex, SpatialIndexError

MAX_CELLS_PER_OBJECT = 1024


class GridIndex(SpatialIndex):
    kind = "uniform-grid"

    def __init__(self, cell_size: int) -> None:
        if cell_size <= 0:
            raise SpatialIndexError(f"cell size must be positive, got {cell_size}")
        self.cell_size = cell_size
        self._cells: dict[tuple[int, int], list[str]] = {}
        self._boxes: dict[str, BoundingBox] = {}
        self._order: dict[str, int] = {}
        self._large: list[str] = []
        self._counter = 0

    def _cell_range(self, box: BoundingBox) -> tuple[int, int, int, int]:
        c = self.cell_size
        return box.min_x // c, box.min_y // c, box.max_x // c, box.max_y // c

    def _cells_of(self, box: BoundingBox) -> Iterator[tuple[int, int]]:
        x0, y0, x1, y1 = self._cell_range(box)
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                yield gx, gy

    def _is_large(self, box: BoundingBox) -> bool:
        x0, y0, x1, y1 = self._cell_range(box)
        return (x1 - x0 + 1) * (y1 - y0 + 1) > MAX_CELLS_PER_OBJECT

    def insert(self, object_id: str, bounds: BoundingBox) -> None:
        if object_id in self._boxes:
            raise SpatialIndexError(f"duplicate id {object_id!r}")
        self._boxes[object_id] = bounds
        self._order[object_id] = self._counter
        self._counter += 1
        if self._is_large(bounds):
            self._large.append(object_id)
            return
        for key in self._cells_of(bounds):
            self._cells.setdefault(key, []).append(object_id)

    def remove(self, object_id: str) -> None:
        box = self._boxes.pop(object_id, None)
        if box is None:
            raise SpatialIndexError(f"unknown id {object_id!r}")
        del self._order[object_id]
        if object_id in self._large:
            self._large.remove(object_id)
            return
        for key in self._cells_of(box):
            cell_ids = self._cells.get(key)
            if cell_ids is not None:
                cell_ids.remove(object_id)
                if not cell_ids:
                    del self._cells[key]

    def query(self, bounds: BoundingBox) -> list[str]:
        found: set[str] = set()
        x0, y0, x1, y1 = self._cell_range(bounds)
        span = (x1 - x0 + 1) * (y1 - y0 + 1)
        if span > len(self._cells):
            # Huge query box: walk the occupied cells instead of the empty ones.
            for (gx, gy), bucket in self._cells.items():
                if x0 <= gx <= x1 and y0 <= gy <= y1:
                    found.update(bucket)
        else:
            for gx in range(x0, x1 + 1):
                for gy in range(y0, y1 + 1):
                    ids = self._cells.get((gx, gy))
                    if ids:
                        found.update(ids)
        found.update(self._large)
        boxes = self._boxes
        hits = [oid for oid in found if boxes[oid].intersects(bounds)]
        order = self._order
        hits.sort(key=order.__getitem__)
        return hits

    def bounds_of(self, object_id: str) -> BoundingBox:
        return self._boxes[object_id]

    def __len__(self) -> int:
        return len(self._boxes)

    def __contains__(self, object_id: object) -> bool:
        return object_id in self._boxes

    @property
    def occupied_cells(self) -> int:
        return len(self._cells)


def choose_cell_size(
    boxes: list[BoundingBox], minimum: int = 250_000, maximum: int = 5_000_000
) -> int:
    """Cell size ~ 2x the median object extent, clamped to [0.25 mm, 5 mm]."""
    if not boxes:
        return 1_000_000
    extents = sorted(max(b.width, b.height) for b in boxes)
    median = extents[len(extents) // 2]
    return max(minimum, min(maximum, 2 * median))
