"""Paths: polylines of capsules, and conservative arc handling.

Arcs (arc tracks, arc board edges, arc pad primitives) are represented as chords.
A chord lies *inside* its arc's bulge by at most the sagitta, so each chord capsule
is grown by the sagitta bound: the result covers the real arc copper (conservative)
and is labelled :attr:`ShapeAccuracy.CONSERVATIVE`. With the default 1 µm bound the
over-estimate is negligible for routing.
"""

from __future__ import annotations

import math
from itertools import pairwise

from pcbrouter.domain.geometry import Point, circle_from_three_points
from pcbrouter.domain.units import Nm
from pcbrouter.geometry.shapes import Shape, ShapeAccuracy, capsule

#: Maximum distance between a real arc and the chords used to represent it.
ARC_MAX_SAGITTA_NM: Nm = 1_000


def arc_chords(
    start: Point, mid: Point, end: Point, max_sagitta: Nm = ARC_MAX_SAGITTA_NM
) -> tuple[list[Point], Nm]:
    """Polyline through the arc ``start -> mid -> end`` whose chords deviate from the
    arc by at most ``max_sagitta``; returns ``(points, sagitta_bound)``.

    Degenerate (collinear) arcs are returned as the straight segment with bound 0.
    """
    circle = circle_from_three_points(start, mid, end)
    if circle is None:
        return [start, end], 0
    cx, cy, r = circle

    def ang(p: Point) -> float:
        return math.atan2(p.y - cy, p.x - cx)

    a0, am, a1 = ang(start), ang(mid), ang(end)
    two_pi = 2 * math.pi
    ccw = (a1 - a0) % two_pi
    sweep = ccw if (am - a0) % two_pi <= ccw else ccw - two_pi
    # Chord angle for the requested sagitta: s = r (1 - cos(t/2)).
    step = math.pi / 2 if r <= max_sagitta else 2 * math.acos(1 - max_sagitta / r)
    n = max(1, math.ceil(abs(sweep) / step))
    pts = [start]
    for i in range(1, n):
        t = a0 + sweep * i / n
        pts.append(Point(round(cx + r * math.cos(t)), round(cy + r * math.sin(t))))
    pts.append(end)
    achieved = r * (1 - math.cos(abs(sweep) / n / 2))
    # +1 nm covers the rounding of the intermediate points to the nm grid.
    return pts, math.ceil(achieved) + 1


def polyline_capsules(
    points: list[Point],
    radius: Nm,
    accuracy: ShapeAccuracy = ShapeAccuracy.EXACT,
    note: str | None = None,
) -> list[Shape]:
    """One capsule per polyline segment (consecutive duplicates skipped)."""
    shapes: list[Shape] = []
    for a, b in pairwise(points):
        s = capsule(a, b, radius, accuracy)
        shapes.append(Shape(s.core, s.radius, s.accuracy, note) if note else s)
    if not shapes and points:
        shapes.append(capsule(points[0], points[0], radius, accuracy))
    return shapes


def arc_capsules(start: Point, mid: Point, end: Point, radius: Nm) -> list[Shape]:
    """Conservative capsules covering an arc track of half-width ``radius``."""
    pts, sagitta = arc_chords(start, mid, end)
    if sagitta == 0:
        return polyline_capsules(pts, radius)
    return polyline_capsules(
        pts,
        radius + sagitta,
        ShapeAccuracy.CONSERVATIVE,
        f"arc approximated by chords (+{sagitta} nm margin)",
    )
