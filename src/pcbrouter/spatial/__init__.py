"""Spatial indexing behind one interface (:class:`SpatialIndex`)."""

from __future__ import annotations

from pcbrouter.spatial.grid_index import GridIndex, choose_cell_size
from pcbrouter.spatial.index import LinearIndex, SpatialIndex, SpatialIndexError
from pcbrouter.spatial.query import LayeredIndex

__all__ = [
    "GridIndex",
    "LayeredIndex",
    "LinearIndex",
    "SpatialIndex",
    "SpatialIndexError",
    "choose_cell_size",
]
