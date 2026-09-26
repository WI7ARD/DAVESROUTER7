"""Copper track segments (straight and arc)."""

from __future__ import annotations

from dataclasses import dataclass

from pcbrouter.domain.geometry import BoundingBox, Point, arc_length, arc_points
from pcbrouter.domain.units import Nm


@dataclass(frozen=True, slots=True)
class Track:
    """A copper track. ``mid`` is set only for arc tracks (3-point arc form)."""

    id: str
    start: Point
    end: Point
    width: Nm
    layer: str
    net_name: str | None
    mid: Point | None = None
    locked: bool = False

    @property
    def is_arc(self) -> bool:
        return self.mid is not None

    @property
    def length(self) -> Nm:
        """Centreline length, rounded to the nearest nanometre."""
        if self.mid is not None:
            return round(arc_length(self.start, self.mid, self.end))
        return round(self.start.distance_to(self.end))

    def centerline(self) -> list[Point]:
        if self.mid is not None:
            return arc_points(self.start, self.mid, self.end)
        return [self.start, self.end]

    @property
    def bounds(self) -> BoundingBox:
        box = BoundingBox.from_points(self.centerline())
        assert box is not None
        return box.expanded(self.width // 2)
