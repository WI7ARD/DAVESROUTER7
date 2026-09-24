"""Copper/obstacle shapes: a *core* (point, segment or polygon) grown by a radius.

This one representation covers every PCB shape the engine needs, exactly:

==================  ==========================================  ==========
PCB object          Core                                         Radius
==================  ==========================================  ==========
round pad / via     point (centre)                               d / 2
track segment       segment (centreline)                         width / 2
oval pad            segment (long axis, shortened)               short / 2
rectangle pad       polygon (the rectangle)                      0
rounded rectangle   polygon (rectangle shrunk by the radius)     corner r
custom polygon      polygon                                      stroke / 2
==================  ==========================================  ==========

Growing a shape by a clearance (Minkowski sum with a disk) is just
``radius + clearance`` — so clearance envelopes and obstacle inflation are exact.

Every shape carries an :class:`ShapeAccuracy`. Approximations are always
*conservative* (the shape covers at least the real copper) and are labelled; they
are never reported as exact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum

from pcbrouter.domain.geometry import BoundingBox, Point, rotate_point
from pcbrouter.domain.units import Nm
from pcbrouter.geometry.distance import Core, PointCore, PolygonCore, SegmentCore
from pcbrouter.geometry.errors import InvalidGeometryError
from pcbrouter.geometry.polygon import Polygon, convex_hull


class ShapeAccuracy(Enum):
    EXACT = "exact"
    #: Covers at least the real geometry (safe for collision checks), maybe more.
    CONSERVATIVE = "conservative_approximation"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            ShapeAccuracy.EXACT: "exact",
            ShapeAccuracy.CONSERVATIVE: "conservative approximation",
            ShapeAccuracy.UNKNOWN: "unknown",
        }[self]

    @staticmethod
    def worst(*values: ShapeAccuracy) -> ShapeAccuracy:
        order = [ShapeAccuracy.EXACT, ShapeAccuracy.CONSERVATIVE, ShapeAccuracy.UNKNOWN]
        return max(values, key=order.index) if values else ShapeAccuracy.EXACT


@dataclass(frozen=True, slots=True)
class Shape:
    core: Core
    radius: Nm = 0
    accuracy: ShapeAccuracy = ShapeAccuracy.EXACT
    #: Why the shape is approximate (``None`` when exact).
    note: str | None = None

    def __post_init__(self) -> None:
        if self.radius < 0:
            raise InvalidGeometryError(f"negative shape radius {self.radius}")

    @property
    def bounds(self) -> BoundingBox:
        return self.core.bounds.expanded(self.radius)

    def inflated(self, amount: Nm) -> Shape:
        """Minkowski sum with a disk of ``amount`` (exact; used for clearance envelopes
        and obstacle inflation)."""
        return replace(self, radius=self.radius + amount)

    def translated(self, dx: Nm, dy: Nm) -> Shape:
        core = self.core
        moved: Core
        if isinstance(core, PointCore):
            moved = PointCore(Point(core.p.x + dx, core.p.y + dy))
        elif isinstance(core, SegmentCore):
            moved = SegmentCore(
                Point(core.a.x + dx, core.a.y + dy), Point(core.b.x + dx, core.b.y + dy)
            )
        else:
            moved = PolygonCore(core.polygon.translated(dx, dy))
        return replace(self, core=moved)

    def rotated(self, angle_deg: float, center: Point) -> Shape:
        core = self.core
        turned: Core
        if isinstance(core, PointCore):
            turned = PointCore(rotate_point(core.p, angle_deg, center))
        elif isinstance(core, SegmentCore):
            turned = SegmentCore(
                rotate_point(core.a, angle_deg, center), rotate_point(core.b, angle_deg, center)
            )
        else:
            turned = PolygonCore(core.polygon.rotated(angle_deg, center))
        return replace(self, core=turned)

    def contains(self, p: Point) -> bool:
        """``p`` is inside the shape or on its boundary (exact)."""
        from pcbrouter.geometry.distance import core_distance2

        d = core_distance2(self.core, PointCore(p))
        assert d is not None
        return d.num <= self.radius * self.radius * d.den

    @property
    def kind(self) -> str:
        core = self.core
        if isinstance(core, PointCore):
            return "circle"
        if isinstance(core, SegmentCore):
            return "capsule"
        return "rounded polygon" if self.radius else "polygon"


# ---------------------------------------------------------------- constructors
def circle(center: Point, radius: Nm, accuracy: ShapeAccuracy = ShapeAccuracy.EXACT) -> Shape:
    return Shape(PointCore(center), radius, accuracy)


def capsule(a: Point, b: Point, radius: Nm, accuracy: ShapeAccuracy = ShapeAccuracy.EXACT) -> Shape:
    """Segment grown by ``radius`` — the exact shape of a straight PCB track."""
    if a == b:
        return Shape(PointCore(a), radius, accuracy)
    return Shape(SegmentCore(a, b), radius, accuracy)


def polygon(
    points: list[Point] | tuple[Point, ...],
    radius: Nm = 0,
    accuracy: ShapeAccuracy = ShapeAccuracy.EXACT,
    note: str | None = None,
) -> Shape:
    return Shape(PolygonCore(Polygon(points)), radius, accuracy, note)


def rectangle(
    center: Point,
    width: Nm,
    height: Nm,
    angle_deg: float = 0.0,
    corner_radius: Nm = 0,
    accuracy: ShapeAccuracy = ShapeAccuracy.EXACT,
) -> Shape:
    """(Rounded) rectangle centred on ``center``, rotated by ``angle_deg`` (KiCad CCW).

    A rounded rectangle is its rectangle shrunk by ``corner_radius`` grown back by a
    disk of ``corner_radius`` — exact, not an approximation.
    """
    r = max(0, min(corner_radius, width // 2, height // 2))
    hw, hh = width // 2 - r, height // 2 - r
    if hw <= 0 and hh <= 0:
        return circle(center, r, accuracy)
    if hw <= 0 or hh <= 0:
        # Degenerate core: a segment along the long axis (e.g. a stadium).
        a = Point(center.x - max(hw, 0), center.y - max(hh, 0))
        b = Point(center.x + max(hw, 0), center.y + max(hh, 0))
        return Shape(
            SegmentCore(rotate_point(a, angle_deg, center), rotate_point(b, angle_deg, center)),
            r,
            accuracy,
        )
    corners = [
        Point(center.x - hw, center.y - hh),
        Point(center.x + hw, center.y - hh),
        Point(center.x + hw, center.y + hh),
        Point(center.x - hw, center.y + hh),
    ]
    if angle_deg:
        corners = [rotate_point(c, angle_deg, center) for c in corners]
    return Shape(PolygonCore(Polygon(corners)), r, accuracy)


def oval(center: Point, width: Nm, height: Nm, angle_deg: float = 0.0) -> Shape:
    """Stadium / oblong pad: exact capsule along the long axis."""
    return rectangle(center, width, height, angle_deg, corner_radius=min(width, height) // 2)


def arc_sagitta(radius: float, step_deg: float) -> float:
    """Maximum distance between an arc and its chord for a chord spanning
    ``step_deg`` — the error of approximating an arc by chords."""
    return radius * (1 - math.cos(math.radians(step_deg) / 2))


def outline_points(shape: Shape, max_step_deg: float = 10.0) -> list[Point]:
    """Polygon approximation of the shape *outline* (for display only)."""
    core = shape.core
    r = shape.radius
    if isinstance(core, PointCore):
        n = max(8, int(360 / max_step_deg))
        return [
            Point(
                core.p.x + round(r * math.cos(2 * math.pi * i / n)),
                core.p.y + round(r * math.sin(2 * math.pi * i / n)),
            )
            for i in range(n)
        ]
    if isinstance(core, SegmentCore):
        a, b = core.a, core.b
        base = math.atan2(b.y - a.y, b.x - a.x)
        pts: list[Point] = []
        steps = max(4, int(180 / max_step_deg))
        for end, start_angle in ((b, base - math.pi / 2), (a, base + math.pi / 2)):
            for i in range(steps + 1):
                t = start_angle + math.pi * i / steps
                pts.append(Point(end.x + round(r * math.cos(t)), end.y + round(r * math.sin(t))))
        return pts
    poly = core.polygon
    if r == 0:
        return list(poly.points)
    # Hull of disks at the vertices: exact for convex polygons (every pad shape),
    # a slight over-estimate for concave ones. Display/export only.
    n = max(8, int(360 / max_step_deg))
    samples = [
        Point(
            v.x + round(r * math.cos(2 * math.pi * i / n)),
            v.y + round(r * math.sin(2 * math.pi * i / n)),
        )
        for v in poly.points
        for i in range(n)
    ]
    return convex_hull(samples)
