"""Spatial index interface.

Callers only see :class:`SpatialIndex`; the implementation can change (grid today,
maybe an R-tree or GPU structure later) without touching collision code.

Contract:

* entries are ``(object_id, bounding box)``; boxes are inclusive integer nm boxes;
* :meth:`query` returns every id whose box intersects the query box — a *superset*
  filter; exact geometry tests happen afterwards;
* results are returned in insertion order, so every query is deterministic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pcbrouter.domain.geometry import BoundingBox, Point


class SpatialIndexError(Exception):
    """Misuse of a spatial index (duplicate insert, unknown id)."""


class SpatialIndex(ABC):
    #: Short name for diagnostics, e.g. "uniform-grid".
    kind: str = "abstract"

    @abstractmethod
    def insert(self, object_id: str, bounds: BoundingBox) -> None: ...

    @abstractmethod
    def remove(self, object_id: str) -> None: ...

    def update(self, object_id: str, bounds: BoundingBox) -> None:
        self.remove(object_id)
        self.insert(object_id, bounds)

    @abstractmethod
    def query(self, bounds: BoundingBox) -> list[str]: ...

    def query_radius(self, point: Point, radius: int) -> list[str]:
        """Ids whose boxes intersect the square around ``point`` (superset of the disk)."""
        return self.query(BoundingBox.around(point, max(radius, 0)))

    @abstractmethod
    def bounds_of(self, object_id: str) -> BoundingBox: ...

    @abstractmethod
    def copy(self) -> SpatialIndex:
        """Independent copy (for working-board geometry)."""

    @abstractmethod
    def __len__(self) -> int: ...

    @abstractmethod
    def __contains__(self, object_id: object) -> bool: ...


class LinearIndex(SpatialIndex):
    """Reference implementation: checks every entry. Used to verify faster indexes
    in tests and as a baseline in benchmarks — never in hot paths."""

    kind = "linear (reference)"

    def __init__(self) -> None:
        self._boxes: dict[str, BoundingBox] = {}

    def insert(self, object_id: str, bounds: BoundingBox) -> None:
        if object_id in self._boxes:
            raise SpatialIndexError(f"duplicate id {object_id!r}")
        self._boxes[object_id] = bounds

    def remove(self, object_id: str) -> None:
        if self._boxes.pop(object_id, None) is None:
            raise SpatialIndexError(f"unknown id {object_id!r}")

    def query(self, bounds: BoundingBox) -> list[str]:
        return [oid for oid, box in self._boxes.items() if box.intersects(bounds)]

    def bounds_of(self, object_id: str) -> BoundingBox:
        return self._boxes[object_id]

    def copy(self) -> LinearIndex:
        other = LinearIndex()
        other._boxes = dict(self._boxes)
        return other

    def __len__(self) -> int:
        return len(self._boxes)

    def __contains__(self, object_id: object) -> bool:
        return object_id in self._boxes
