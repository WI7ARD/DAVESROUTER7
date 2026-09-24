"""Exact distances between shape *cores* (point, segment, polygon).

A :class:`~pcbrouter.geometry.shapes.Shape` is a core grown by a radius, so the gap
between two shapes is ``dist(core_a, core_b) - r_a - r_b``. This module computes the
*squared core distance* exactly, as a :class:`~pcbrouter.geometry.exact.Rational`.

``limit`` (optional, nm): the caller only cares whether the distance is <= limit.
Large polygons then skip edges farther away, and ``None`` is returned to mean
"certainly farther than ``limit``". Results at or below the limit are exact.
"""

from __future__ import annotations

from dataclasses import dataclass

from pcbrouter.domain.geometry import BoundingBox, Point
from pcbrouter.geometry.exact import (
    ZERO,
    Rational,
    point_point_d2,
    point_segment_d2,
    segment_segment_d2,
    segments_intersect,
)
from pcbrouter.geometry.polygon import Polygon


@dataclass(frozen=True, slots=True)
class PointCore:
    p: Point

    @property
    def bounds(self) -> BoundingBox:
        return BoundingBox(self.p.x, self.p.y, self.p.x, self.p.y)

    def representative(self) -> Point:
        return self.p


@dataclass(frozen=True, slots=True)
class SegmentCore:
    a: Point
    b: Point

    @property
    def bounds(self) -> BoundingBox:
        return BoundingBox(
            min(self.a.x, self.b.x),
            min(self.a.y, self.b.y),
            max(self.a.x, self.b.x),
            max(self.a.y, self.b.y),
        )

    def representative(self) -> Point:
        return self.a


@dataclass(frozen=True, slots=True)
class PolygonCore:
    polygon: Polygon

    @property
    def bounds(self) -> BoundingBox:
        return self.polygon.bounds

    def representative(self) -> Point:
        return self.polygon.points[0]


type Core = PointCore | SegmentCore | PolygonCore


def _box_gap_exceeds(a: BoundingBox, b: BoundingBox, limit: int) -> bool:
    """Cheap exact reject: the boxes are more than ``limit`` apart on an axis."""
    return (
        a.min_x - b.max_x > limit
        or b.min_x - a.max_x > limit
        or a.min_y - b.max_y > limit
        or b.min_y - a.max_y > limit
    )


def _poly_point(poly: Polygon, p: Point, limit: int | None) -> Rational | None:
    if poly.contains(p):
        return ZERO
    box = (
        BoundingBox(p.x - limit, p.y - limit, p.x + limit, p.y + limit)
        if limit is not None
        else poly.bounds
    )
    best: Rational | None = None
    for a, b in poly.edges_near(box):
        d = point_segment_d2(p, a, b)
        if best is None or d < best:
            best = d
    return best


def _poly_segment(poly: Polygon, a: Point, b: Point, limit: int | None) -> Rational | None:
    if poly.contains(a) or poly.contains(b):
        return ZERO
    seg_box = SegmentCore(a, b).bounds
    box = seg_box.expanded(limit) if limit is not None else poly.bounds.union(seg_box)
    best: Rational | None = None
    for c, d in poly.edges_near(box):
        if segments_intersect(a, b, c, d):
            return ZERO
        dist = segment_segment_d2(a, b, c, d)
        if best is None or dist < best:
            best = dist
    return best


def _poly_poly(p: Polygon, q: Polygon, limit: int | None) -> Rational | None:
    if p.contains(q.points[0]) or q.contains(p.points[0]):
        return ZERO
    if limit is not None:
        # Walk the smaller polygon's edges against nearby edges of the larger one.
        small, large = (p, q) if p.edge_count <= q.edge_count else (q, p)
        best: Rational | None = None
        for a, b in small.edges():
            box = SegmentCore(a, b).bounds.expanded(limit)
            for c, d in large.edges_near(box):
                if segments_intersect(a, b, c, d):
                    return ZERO
                dist = segment_segment_d2(a, b, c, d)
                if best is None or dist < best:
                    best = dist
        return best
    best_all: Rational | None = None
    for a, b in p.edges():
        for c, d in q.edges():
            if segments_intersect(a, b, c, d):
                return ZERO
            dist = segment_segment_d2(a, b, c, d)
            if best_all is None or dist < best_all:
                best_all = dist
    return best_all


def core_distance2(a: Core, b: Core, limit: int | None = None) -> Rational | None:
    """Exact squared distance between two cores (0 = touching/overlapping).

    With ``limit``: ``None`` means the distance is certainly greater than ``limit``.
    Without it the result is never ``None``.
    """
    if limit is not None and _box_gap_exceeds(a.bounds, b.bounds, limit):
        return None
    result: Rational | None
    if isinstance(a, PointCore):
        if isinstance(b, PointCore):
            result = point_point_d2(a.p, b.p)
        elif isinstance(b, SegmentCore):
            result = point_segment_d2(a.p, b.a, b.b)
        else:
            result = _poly_point(b.polygon, a.p, limit)
    elif isinstance(a, SegmentCore):
        if isinstance(b, PointCore):
            result = point_segment_d2(b.p, a.a, a.b)
        elif isinstance(b, SegmentCore):
            result = segment_segment_d2(a.a, a.b, b.a, b.b)
        else:
            result = _poly_segment(b.polygon, a.a, a.b, limit)
    else:
        if isinstance(b, PointCore):
            result = _poly_point(a.polygon, b.p, limit)
        elif isinstance(b, SegmentCore):
            result = _poly_segment(a.polygon, b.a, b.b, limit)
        else:
            result = _poly_poly(a.polygon, b.polygon, limit)
    if result is None and limit is None:  # pragma: no cover - defensive
        raise AssertionError("unbounded distance query returned no result")
    if result is not None and limit is not None and not result.below_square_of(limit + 1):
        return None
    return result


def core_contains_point(core: Core, p: Point) -> bool:
    """Point lies on the core itself (distance 0)."""
    d = core_distance2(core, PointCore(p))
    return d is not None and d.is_zero()
