"""Layer-aware indexing: one spatial index per copper layer.

Through-hole pads and vias are inserted into the index of every layer they occupy,
so a query on ``F.Cu`` never returns ``B.Cu``-only tracks (no cross-layer work).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from pcbrouter.domain.geometry import BoundingBox, Point
from pcbrouter.spatial.index import SpatialIndex


class LayeredIndex:
    def __init__(self, layers: Iterable[str], factory: Callable[[], SpatialIndex]) -> None:
        self._indexes: dict[str, SpatialIndex] = {layer: factory() for layer in layers}

    @property
    def layers(self) -> list[str]:
        return list(self._indexes)

    def index(self, layer: str) -> SpatialIndex | None:
        return self._indexes.get(layer)

    def insert(self, object_id: str, bounds: BoundingBox, layers: Iterable[str]) -> None:
        for layer in layers:
            idx = self._indexes.get(layer)
            if idx is not None:
                idx.insert(object_id, bounds)

    def remove(self, object_id: str, layers: Iterable[str]) -> None:
        for layer in layers:
            idx = self._indexes.get(layer)
            if idx is not None and object_id in idx:
                idx.remove(object_id)

    def query(self, layer: str, bounds: BoundingBox) -> list[str]:
        idx = self._indexes.get(layer)
        return idx.query(bounds) if idx is not None else []

    def query_radius(self, layer: str, point: Point, radius: int) -> list[str]:
        idx = self._indexes.get(layer)
        return idx.query_radius(point, radius) if idx is not None else []

    def copy(self) -> LayeredIndex:
        other = LayeredIndex((), lambda: self._indexes[next(iter(self._indexes))])
        other._indexes = {layer: idx.copy() for layer, idx in self._indexes.items()}
        return other

    def entry_count(self) -> int:
        """Total entries across layers (multi-layer objects counted per layer)."""
        return sum(len(idx) for idx in self._indexes.values())
