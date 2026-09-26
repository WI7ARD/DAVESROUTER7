"""Lightweight integer geometry primitives.

All coordinates are internal nanometres (see :mod:`pcbrouter.domain.units`).

Coordinate system: identical to KiCad — X grows to the right, Y grows *downwards*.
Positive rotation angles are counter-clockwise *as seen on screen*, which matches
KiCad's convention.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from pcbrouter.domain.units import Nm


@dataclass(frozen=True, slots=True, order=True)
class Point:
    """An exact integer point in internal units."""

    x: Nm
    y: Nm

    def __add__(self, other: Point) -> Point:
        return Point(self.x + other.x, self.y + other.y)

    def __sub__(self, other: Point) -> Point:
        return Point(self.x - other.x, self.y - other.y)

    def distance_to(self, other: Point) -> float:
        """Euclidean distance in internal units (float: lengths are rarely integral)."""
        return math.hypot(other.x - self.x, other.y - self.y)


ORIGIN = Point(0, 0)


def rotate_point(point: Point, angle_deg: float, center: Point = ORIGIN) -> Point:
    """Rotate ``point`` about ``center`` using KiCad's on-screen CCW convention.

    Because Y points down, an on-screen counter-clockwise rotation by ``a`` is::

        x' =  x*cos(a) + y*sin(a)
        y' = -x*sin(a) + y*cos(a)

    Exact results are returned for multiples of 90 degrees (the overwhelmingly
    common case for component placement) to avoid introducing 1 nm errors.
    """
    dx = point.x - center.x
    dy = point.y - center.y
    quarter_turns = angle_deg / 90.0
    if quarter_turns.is_integer():
        turns = int(quarter_turns) % 4
        if turns == 0:
            rx, ry = dx, dy
        elif turns == 1:
            rx, ry = dy, -dx
        elif turns == 2:
            rx, ry = -dx, -dy
        else:
            rx, ry = -dy, dx
    else:
        rad = math.radians(angle_deg)
        c, s = math.cos(rad), math.sin(rad)
        rx = round(dx * c + dy * s)
        ry = round(-dx * s + dy * c)
    return Point(center.x + rx, center.y + ry)


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """Axis-aligned bounding box (inclusive) in internal units."""

    min_x: Nm
    min_y: Nm
    max_x: Nm
    max_y: Nm

    def __post_init__(self) -> None:
        if self.min_x > self.max_x or self.min_y > self.max_y:
            raise ValueError(
                f"invalid bounding box: ({self.min_x},{self.min_y})-({self.max_x},{self.max_y})"
            )

    @classmethod
    def from_points(cls, points: Iterable[Point]) -> BoundingBox | None:
        """Smallest box containing ``points``; ``None`` if there are no points."""
        it = iter(points)
        first = next(it, None)
        if first is None:
            return None
        min_x = max_x = first.x
        min_y = max_y = first.y
        for p in it:
            if p.x < min_x:
                min_x = p.x
            elif p.x > max_x:
                max_x = p.x
            if p.y < min_y:
                min_y = p.y
            elif p.y > max_y:
                max_y = p.y
        return cls(min_x, min_y, max_x, max_y)

    @classmethod
    def around(cls, center: Point, half_width: Nm, half_height: Nm | None = None) -> BoundingBox:
        hh = half_width if half_height is None else half_height
        return cls(center.x - half_width, center.y - hh, center.x + half_width, center.y + hh)

    @property
    def width(self) -> Nm:
        return self.max_x - self.min_x

    @property
    def height(self) -> Nm:
        return self.max_y - self.min_y

    @property
    def center(self) -> Point:
        return Point((self.min_x + self.max_x) // 2, (self.min_y + self.max_y) // 2)

    @property
    def corners(self) -> tuple[Point, Point, Point, Point]:
        return (
            Point(self.min_x, self.min_y),
            Point(self.max_x, self.min_y),
            Point(self.max_x, self.max_y),
            Point(self.min_x, self.max_y),
        )

    def union(self, other: BoundingBox) -> BoundingBox:
        return BoundingBox(
            min(self.min_x, other.min_x),
            min(self.min_y, other.min_y),
            max(self.max_x, other.max_x),
            max(self.max_y, other.max_y),
        )

    def expanded(self, margin: Nm) -> BoundingBox:
        return BoundingBox(
            self.min_x - margin, self.min_y - margin, self.max_x + margin, self.max_y + margin
        )

    def contains(self, point: Point) -> bool:
        return self.min_x <= point.x <= self.max_x and self.min_y <= point.y <= self.max_y

    def intersects(self, other: BoundingBox) -> bool:
        return not (
            other.min_x > self.max_x
            or other.max_x < self.min_x
            or other.min_y > self.max_y
            or other.max_y < self.min_y
        )


def union_all(boxes: Iterable[BoundingBox | None]) -> BoundingBox | None:
    """Union of all non-``None`` boxes, or ``None`` if there are none."""
    result: BoundingBox | None = None
    for box in boxes:
        if box is None:
            continue
        result = box if result is None else result.union(box)
    return result


def circle_from_three_points(a: Point, b: Point, c: Point) -> tuple[float, float, float] | None:
    """Return ``(cx, cy, radius)`` of the circle through three points, or ``None``
    if the points are collinear (a degenerate arc)."""
    ax, ay, bx, by, cx, cy = float(a.x), float(a.y), float(b.x), float(b.y), float(c.x), float(c.y)
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    a2 = ax * ax + ay * ay
    b2 = bx * bx + by * by
    c2 = cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    return ux, uy, math.hypot(ax - ux, ay - uy)


def arc_sweep_deg(start: Point, mid: Point, end: Point) -> float | None:
    """Signed sweep angle (degrees, math convention in Y-up space) of the arc
    ``start -> mid -> end``; ``None`` for a degenerate (collinear) arc."""
    circle = circle_from_three_points(start, mid, end)
    if circle is None:
        return None
    cx, cy, _ = circle

    def ang(p: Point) -> float:
        # Flip Y so the angle uses the conventional Y-up orientation.
        return math.atan2(-(p.y - cy), p.x - cx)

    a0, am, a1 = ang(start), ang(mid), ang(end)
    two_pi = 2.0 * math.pi
    ccw = (a1 - a0) % two_pi
    ccw_mid = (am - a0) % two_pi
    sweep = ccw if ccw_mid <= ccw else ccw - two_pi
    return math.degrees(sweep)


def arc_length(start: Point, mid: Point, end: Point) -> float:
    """Length of the circular arc through three points; falls back to the chord
    length for degenerate (collinear) input."""
    circle = circle_from_three_points(start, mid, end)
    sweep = arc_sweep_deg(start, mid, end)
    if circle is None or sweep is None:
        return start.distance_to(end)
    return abs(math.radians(sweep)) * circle[2]


def arc_points(start: Point, mid: Point, end: Point, max_segment_deg: float = 10.0) -> list[Point]:
    """Approximate the arc through three points as a polyline (for rendering/bounds)."""
    circle = circle_from_three_points(start, mid, end)
    sweep = arc_sweep_deg(start, mid, end)
    if circle is None or sweep is None:
        return [start, end]
    cx, cy, r = circle
    a0 = math.atan2(-(start.y - cy), start.x - cx)
    steps = max(2, math.ceil(abs(sweep) / max_segment_deg))
    pts = [start]
    for i in range(1, steps):
        a = a0 + math.radians(sweep) * i / steps
        pts.append(Point(round(cx + r * math.cos(a)), round(cy - r * math.sin(a))))
    pts.append(end)
    return pts


def arc_mid_from_center(center: Point, start: Point, angle_deg: float) -> tuple[Point, Point]:
    """Convert a center/start/sweep arc (KiCad 5 ``gr_arc`` form) into ``(mid, end)``.

    KiCad 5 stores ``(start <center>) (end <arc start>) (angle <deg>)`` where the
    angle is clockwise-positive on screen (the opposite of our rotation helper).
    """
    mid = rotate_point(start, -angle_deg / 2.0, center)
    end = rotate_point(start, -angle_deg, center)
    return mid, end
