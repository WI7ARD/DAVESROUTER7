"""Deterministic PCB geometry kernel (Stage 3).

* Coordinates are integer nanometres (Stage 1's canonical unit).
* Every shape is a *core* (point, segment, polygon) grown by a radius
  (:mod:`~pcbrouter.geometry.shapes`), so clearance inflation is exact.
* Decisions use exact integer/rational arithmetic (:mod:`~pcbrouter.geometry.exact`);
  floats are only used to report distances and in sampled occupancy grids.
* Boundary semantics and the single 1 nm tolerance live in
  :mod:`~pcbrouter.geometry.clearance`.

See docs/geometry_engine.md.
"""

from __future__ import annotations

from pcbrouter.geometry.clearance import (
    GEOMETRY_TOLERANCE_NM,
    GapResult,
    check_clearance,
    gap_between,
    meets_clearance,
    overlaps,
    touches,
)
from pcbrouter.geometry.errors import GeometryError, InvalidGeometryError, UnsupportedGeometryError
from pcbrouter.geometry.polygon import Polygon
from pcbrouter.geometry.shapes import (
    Shape,
    ShapeAccuracy,
    capsule,
    circle,
    oval,
    polygon,
    rectangle,
)

#: Bumped whenever geometry semantics change (reported in diagnostics/snapshots).
GEOMETRY_ENGINE_VERSION = "3.0.0"

__all__ = [
    "GEOMETRY_ENGINE_VERSION",
    "GEOMETRY_TOLERANCE_NM",
    "GapResult",
    "GeometryError",
    "InvalidGeometryError",
    "Polygon",
    "Shape",
    "ShapeAccuracy",
    "UnsupportedGeometryError",
    "capsule",
    "check_clearance",
    "circle",
    "gap_between",
    "meets_clearance",
    "oval",
    "overlaps",
    "polygon",
    "rectangle",
    "touches",
]
