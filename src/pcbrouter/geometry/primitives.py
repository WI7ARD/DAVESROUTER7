"""User-facing geometric primitives.

Each primitive converts to one or more :class:`~pcbrouter.geometry.shapes.Shape`
(core + radius) and delegates every distance/intersection question to the single
exact kernel in :mod:`pcbrouter.geometry.distance` — formulas are not duplicated.

``Point`` and ``BoundingBox`` are the Stage 1 domain types, re-exported here.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

from pcbrouter.domain.geometry import BoundingBox, Point, rotate_point
from pcbrouter.domain.units import Nm
from pcbrouter.geometry.clearance import gap_between
from pcbrouter.geometry.distance import core_distance2
from pcbrouter.geometry.exact import cross
from pcbrouter.geometry.path import arc_chords
from pcbrouter.geometry.polygon import Polygon
from pcbrouter.geometry.shapes import Shape, capsule, circle, rectangle

__all__ = [
    "Arc",
    "BoundingBox",
    "Capsule",
    "Circle",
    "Line",
    "OrientedRectangle",
    "Point",
    "Polygon",
    "Polyline",
    "Rectangle",
    "RoundedRectangle",
    "Segment",
    "Vector",
    "distance",
    "intersects",
]


@dataclass(frozen=True, slots=True)
class Vector:
    """A displacement in nm (not a location)."""

    x: Nm
    y: Nm

    def dot(self, other: Vector) -> int:
        return self.x * other.x + self.y * other.y

    def cross(self, other: Vector) -> int:
        return self.x * other.y - self.y * other.x

    @property
    def length(self) -> float:
        return math.hypot(self.x, self.y)

    def rotated(self, angle_deg: float) -> Vector:
        p = rotate_point(Point(self.x, self.y), angle_deg)
        return Vector(p.x, p.y)

    @staticmethod
    def between(a: Point, b: Point) -> Vector:
        return Vector(b.x - a.x, b.y - a.y)


class _Primitive(ABC):
    """Shared behaviour; subclasses implement :meth:`shapes`."""

    __slots__ = ()

    @abstractmethod
    def shapes(self) -> list[Shape]: ...

    def bounds(self) -> BoundingBox:
        boxes = [s.bounds for s in self.shapes()]
        box = boxes[0]
        for b in boxes[1:]:
            box = box.union(b)
        return box

    def contains(self, p: Point) -> bool:
        return any(s.contains(p) for s in self.shapes())

    def distance_to(self, other: _Primitive | Point) -> float:
        return distance(self, other)

    def intersects(self, other: _Primitive | Point) -> bool:
        return intersects(self, other)

    def expanded(self, clearance: Nm) -> list[Shape]:
        """Clearance envelope: every shape grown by ``clearance`` (exact)."""
        return [s.inflated(clearance) for s in self.shapes()]


def _shapes_of(obj: _Primitive | Point) -> list[Shape]:
    if isinstance(obj, Point):
        return [circle(obj, 0)]
    return obj.shapes()


def distance(a: _Primitive | Point, b: _Primitive | Point) -> float:
    """Smallest boundary-to-boundary distance in nm (0 when touching/overlapping)."""
    return max(0.0, min(gap_between(x, y) for x in _shapes_of(a) for y in _shapes_of(b)))


def intersects(a: _Primitive | Point, b: _Primitive | Point) -> bool:
    for x in _shapes_of(a):
        for y in _shapes_of(b):
            reach = x.radius + y.radius
            d2 = core_distance2(x.core, y.core, limit=reach)
            if d2 is not None and d2.num <= reach * reach * d2.den:
                return True
    return False


@dataclass(frozen=True, slots=True)
class Segment(_Primitive):
    a: Point
    b: Point

    def shapes(self) -> list[Shape]:
        return [capsule(self.a, self.b, 0)]

    @property
    def length(self) -> float:
        return self.a.distance_to(self.b)

    def translated(self, dx: Nm, dy: Nm) -> Segment:
        return Segment(Point(self.a.x + dx, self.a.y + dy), Point(self.b.x + dx, self.b.y + dy))

    def rotated(self, angle_deg: float, center: Point) -> Segment:
        return Segment(
            rotate_point(self.a, angle_deg, center), rotate_point(self.b, angle_deg, center)
        )


@dataclass(frozen=True, slots=True)
class Line:
    """Infinite line through ``a`` and ``b`` (``a != b``)."""

    a: Point
    b: Point

    def side(self, p: Point) -> int:
        """+1 / -1 for the two half-planes, 0 on the line (exact)."""
        c = cross(self.a, self.b, p)
        return (c > 0) - (c < 0)

    def distance_to(self, p: Point) -> float:
        dx, dy = self.b.x - self.a.x, self.b.y - self.a.y
        return abs(cross(self.a, self.b, p)) / math.hypot(dx, dy)

    def intersection(self, other: Line) -> tuple[float, float] | None:
        """Intersection point (float) or ``None`` if parallel."""
        d1x, d1y = self.b.x - self.a.x, self.b.y - self.a.y
        d2x, d2y = other.b.x - other.a.x, other.b.y - other.a.y
        den = d1x * d2y - d1y * d2x
        if den == 0:
            return None
        t = ((other.a.x - self.a.x) * d2y - (other.a.y - self.a.y) * d2x) / den
        return self.a.x + t * d1x, self.a.y + t * d1y


@dataclass(frozen=True, slots=True)
class Circle(_Primitive):
    center: Point
    radius: Nm

    def shapes(self) -> list[Shape]:
        return [circle(self.center, self.radius)]

    def translated(self, dx: Nm, dy: Nm) -> Circle:
        return Circle(Point(self.center.x + dx, self.center.y + dy), self.radius)

    def rotated(self, angle_deg: float, center: Point) -> Circle:
        return Circle(rotate_point(self.center, angle_deg, center), self.radius)


@dataclass(frozen=True, slots=True)
class Capsule(_Primitive):
    """Segment grown by ``radius`` — a straight PCB track (width = 2 * radius)."""

    a: Point
    b: Point
    radius: Nm

    def shapes(self) -> list[Shape]:
        return [capsule(self.a, self.b, self.radius)]

    def translated(self, dx: Nm, dy: Nm) -> Capsule:
        return Capsule(
            Point(self.a.x + dx, self.a.y + dy), Point(self.b.x + dx, self.b.y + dy), self.radius
        )

    def rotated(self, angle_deg: float, center: Point) -> Capsule:
        return Capsule(
            rotate_point(self.a, angle_deg, center),
            rotate_point(self.b, angle_deg, center),
            self.radius,
        )


@dataclass(frozen=True, slots=True)
class Rectangle(_Primitive):
    """Axis-aligned rectangle given by its bounds."""

    box: BoundingBox

    def shapes(self) -> list[Shape]:
        return [rectangle(self.box.center, self.box.width, self.box.height)]

    def translated(self, dx: Nm, dy: Nm) -> Rectangle:
        b = self.box
        return Rectangle(BoundingBox(b.min_x + dx, b.min_y + dy, b.max_x + dx, b.max_y + dy))

    def rotated(self, angle_deg: float, center: Point) -> OrientedRectangle:
        return OrientedRectangle(
            rotate_point(self.box.center, angle_deg, center),
            self.box.width,
            self.box.height,
            angle_deg,
        )


@dataclass(frozen=True, slots=True)
class OrientedRectangle(_Primitive):
    center: Point
    width: Nm
    height: Nm
    angle_deg: float = 0.0

    def shapes(self) -> list[Shape]:
        return [rectangle(self.center, self.width, self.height, self.angle_deg)]

    def translated(self, dx: Nm, dy: Nm) -> OrientedRectangle:
        c = Point(self.center.x + dx, self.center.y + dy)
        return OrientedRectangle(c, self.width, self.height, self.angle_deg)

    def rotated(self, angle_deg: float, center: Point) -> OrientedRectangle:
        return OrientedRectangle(
            rotate_point(self.center, angle_deg, center),
            self.width,
            self.height,
            (self.angle_deg + angle_deg) % 360.0,
        )


@dataclass(frozen=True, slots=True)
class RoundedRectangle(_Primitive):
    center: Point
    width: Nm
    height: Nm
    corner_radius: Nm
    angle_deg: float = 0.0

    def shapes(self) -> list[Shape]:
        return [rectangle(self.center, self.width, self.height, self.angle_deg, self.corner_radius)]

    def translated(self, dx: Nm, dy: Nm) -> RoundedRectangle:
        c = Point(self.center.x + dx, self.center.y + dy)
        return RoundedRectangle(c, self.width, self.height, self.corner_radius, self.angle_deg)

    def rotated(self, angle_deg: float, center: Point) -> RoundedRectangle:
        return RoundedRectangle(
            rotate_point(self.center, angle_deg, center),
            self.width,
            self.height,
            self.corner_radius,
            (self.angle_deg + angle_deg) % 360.0,
        )


@dataclass(frozen=True, slots=True)
class Polyline(_Primitive):
    points: tuple[Point, ...]
    width: Nm = 0

    def shapes(self) -> list[Shape]:
        from pcbrouter.geometry.path import polyline_capsules

        return polyline_capsules(list(self.points), self.width // 2)


@dataclass(frozen=True, slots=True)
class Arc(_Primitive):
    """Circular arc through three points, drawn with ``width`` (0 = hairline)."""

    start: Point
    mid: Point
    end: Point
    width: Nm = 0

    def shapes(self) -> list[Shape]:
        from pcbrouter.geometry.path import arc_capsules

        return arc_capsules(self.start, self.mid, self.end, self.width // 2)

    def chord_points(self) -> list[Point]:
        return arc_chords(self.start, self.mid, self.end)[0]
