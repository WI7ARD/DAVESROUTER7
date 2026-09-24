"""Gap / clearance predicates between shapes — the single definition of boundary
semantics for the whole engine.

Definitions (all lengths in integer nm):

* ``gap(a, b) = dist(core_a, core_b) - r_a - r_b``. Negative = copper overlaps.
* A pair **meets** a clearance ``c`` when ``gap >= c - GEOMETRY_TOLERANCE_NM``.
  Equality is legal: an observed 0.200000 mm against a required 0.200000 mm passes.
* Two same-net shapes **touch** (are electrically joined) when
  ``gap <= GEOMETRY_TOLERANCE_NM``.

Why a 1 nm tolerance: board files have 1 nm resolution, and shapes rotated by
non-multiples of 90° have their vertices rounded to the nearest nm (<= 0.71 nm
error). 1 nm absorbs that rounding so results never flip on numerical jitter, yet
it is ~10,000x below any fabrication tolerance. All comparisons are otherwise
exact (integer/rational arithmetic), so results are fully deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass

from pcbrouter.domain.geometry import Point
from pcbrouter.domain.units import Nm
from pcbrouter.geometry.distance import PointCore, PolygonCore, SegmentCore, core_distance2
from pcbrouter.geometry.exact import closest_point_on_segment
from pcbrouter.geometry.shapes import Shape

#: Absorbs 1 nm file resolution / rotation rounding. See module docstring.
GEOMETRY_TOLERANCE_NM: Nm = 1


@dataclass(frozen=True, slots=True)
class GapResult:
    """Outcome of comparing two shapes against a required clearance."""

    ok: bool
    #: Observed gap in nm (negative = overlap). ``None`` if farther than the query
    #: limit (then ``ok`` is True and the exact value was not needed).
    gap: float | None
    required: Nm
    #: A point near the closest approach, for locating violations (approximate).
    location: Point | None = None

    @property
    def overlap(self) -> bool:
        return self.gap is not None and self.gap < -GEOMETRY_TOLERANCE_NM


def meets_clearance(a: Shape, b: Shape, required: Nm, tol: Nm = GEOMETRY_TOLERANCE_NM) -> bool:
    """Exact: ``gap(a, b) >= required - tol``."""
    threshold = required - tol + a.radius + b.radius  # on the core distance
    if threshold <= 0:
        return True
    d2 = core_distance2(a.core, b.core, limit=threshold)
    return d2 is None or d2.at_least_square_of(threshold)


def check_clearance(a: Shape, b: Shape, required: Nm, tol: Nm = GEOMETRY_TOLERANCE_NM) -> GapResult:
    """Like :func:`meets_clearance` but also reports the observed gap and a location."""
    threshold = required - tol + a.radius + b.radius
    limit = max(threshold, 0)
    d2 = core_distance2(a.core, b.core, limit=limit)
    if d2 is None:
        return GapResult(True, None, required)
    ok = d2.at_least_square_of(threshold) if threshold > 0 else True
    gap = d2.sqrt() - a.radius - b.radius
    return GapResult(ok, gap, required, approximate_contact_point(a, b))


def gap_between(a: Shape, b: Shape) -> float:
    """Observed gap in nm (reporting; negative = overlap)."""
    d2 = core_distance2(a.core, b.core)
    assert d2 is not None
    return d2.sqrt() - a.radius - b.radius


def touches(a: Shape, b: Shape, tol: Nm = GEOMETRY_TOLERANCE_NM) -> bool:
    """Copper touches or overlaps (``gap <= tol``). Exact; used for connectivity."""
    reach = a.radius + b.radius + tol  # on the core distance
    d2 = core_distance2(a.core, b.core, limit=reach)
    return d2 is not None and d2.num <= reach * reach * d2.den


def overlaps(a: Shape, b: Shape, tol: Nm = GEOMETRY_TOLERANCE_NM) -> bool:
    """Copper genuinely overlaps (``gap < -tol``): a short between different nets."""
    reach = a.radius + b.radius - tol
    if reach <= 0:
        return False  # the core distance can never be negative
    d2 = core_distance2(a.core, b.core, limit=reach)
    return d2 is not None and d2.below_square_of(reach)


def _anchor(shape: Shape) -> Point:
    core = shape.core
    if isinstance(core, PointCore):
        return core.p
    if isinstance(core, SegmentCore):
        return Point((core.a.x + core.b.x) // 2, (core.a.y + core.b.y) // 2)
    return core.polygon.bounds.center


def approximate_contact_point(a: Shape, b: Shape) -> Point:
    """A point between the two shapes near their closest approach (for markers)."""
    pa, pb = _anchor(a), _anchor(b)
    if isinstance(a.core, SegmentCore):
        pa = closest_point_on_segment(pb, a.core.a, a.core.b)
    if isinstance(b.core, SegmentCore):
        pb = closest_point_on_segment(pa, b.core.a, b.core.b)
    if isinstance(a.core, PolygonCore) and not isinstance(b.core, PolygonCore):
        return pb
    if isinstance(b.core, PolygonCore) and not isinstance(a.core, PolygonCore):
        return pa
    return Point((pa.x + pb.x) // 2, (pa.y + pb.y) // 2)
