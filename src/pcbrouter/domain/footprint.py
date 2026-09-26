"""Physical footprint placement (the copper/mechanical side of a component)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pcbrouter.domain.geometry import BoundingBox, Point, rotate_point
from pcbrouter.domain.pad import Pad
from pcbrouter.domain.units import Nm


@dataclass(frozen=True, slots=True)
class Courtyard:
    layer: str  # "F.CrtYd" or "B.CrtYd"
    points: tuple[Point, ...]  # closed polygon, absolute coordinates


class BoardSide(Enum):
    FRONT = "front"
    BACK = "back"


@dataclass(frozen=True, slots=True)
class Footprint:
    """A placed footprint.

    ``local_bounds`` is the body extent in the footprint's *local, unrotated* frame
    (derived from courtyard/fab graphics, falling back to pad extents). Use
    :meth:`outline` for the rotated absolute polygon.
    """

    id: str
    lib_id: str  # e.g. "Resistor_SMD:R_0603_1608Metric"
    position: Point
    rotation_deg: float
    side: BoardSide
    pads: tuple[Pad, ...]
    local_bounds: BoundingBox | None
    locked: bool = False
    attributes: tuple[str, ...] = ()  # e.g. ("smd",), ("through_hole",)
    #: Courtyard polygons in absolute board coordinates. Placement metadata only:
    #: courtyards are NOT routing keepouts (see docs/geometry_engine.md).
    courtyards: tuple[Courtyard, ...] = ()
    #: Footprint-level clearance override for its pads (``None`` = inherit).
    local_clearance: Nm | None = None

    def outline(self) -> tuple[Point, ...]:
        """Absolute polygon (4 corners) of the body, or empty if unknown."""
        if self.local_bounds is None:
            return ()
        return tuple(
            rotate_point(self.position + corner, self.rotation_deg, self.position)
            for corner in self.local_bounds.corners
        )

    @property
    def bounds(self) -> BoundingBox | None:
        pts = list(self.outline())
        for pad in self.pads:
            pts.extend(pad.bounds.corners)
        return BoundingBox.from_points(pts)
