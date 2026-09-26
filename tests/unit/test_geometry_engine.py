"""Stage 3 geometry kernel: primitives, transforms, exact distances, boundaries."""

from __future__ import annotations

import math

import numpy as np
import pytest

from pcbrouter.domain.geometry import BoundingBox, Point
from pcbrouter.domain.units import (
    NM_PER_MIL,
    internal_to_mm,
    mil_to_internal,
    mm_to_internal,
)
from pcbrouter.geometry import (
    GEOMETRY_TOLERANCE_NM,
    Polygon,
    ShapeAccuracy,
    capsule,
    check_clearance,
    circle,
    gap_between,
    meets_clearance,
    oval,
    overlaps,
    polygon,
    rectangle,
    touches,
)
from pcbrouter.geometry.distance import PointCore, PolygonCore, SegmentCore, core_distance2
from pcbrouter.geometry.errors import InvalidGeometryError
from pcbrouter.geometry.exact import (
    Rational,
    point_segment_d2,
    segment_segment_d2,
    segments_intersect,
)
from pcbrouter.geometry.path import ARC_MAX_SAGITTA_NM, arc_capsules, arc_chords
from pcbrouter.geometry.polygon import convex_hull
from pcbrouter.geometry.primitives import (
    Arc,
    Capsule,
    Circle,
    Line,
    OrientedRectangle,
    Rectangle,
    RoundedRectangle,
    Segment,
    Vector,
)
from pcbrouter.geometry.raster import centers, distance_field
from pcbrouter.geometry.shapes import outline_points
from pcbrouter.geometry.transforms import Transform, pad_local_to_board

MM = 1_000_000
P = Point


# ------------------------------------------------------------------ units
def test_unit_conversions_are_exact() -> None:
    assert mm_to_internal(0.1) == 100_000
    assert mm_to_internal("0.2" if False else 0.2) == 200_000
    assert internal_to_mm(250_000) == 0.25
    assert mil_to_internal(1) == NM_PER_MIL == 25_400
    assert mil_to_internal(8) == 203_200


def test_internal_to_mil_round_trip() -> None:
    from pcbrouter.domain.units import internal_to_mil

    assert internal_to_mil(mil_to_internal(10)) == 10
    assert internal_to_mil(254_000) == 10


# ------------------------------------------------------------------ exact predicates
def test_rational_is_exact() -> None:
    assert Rational(1, 2) == Rational(2, 4)
    assert Rational(1, 3) < Rational(1, 2)
    assert Rational(9, 1).at_least_square_of(3)
    assert not Rational(8, 1).at_least_square_of(3)
    assert Rational(0, 1).at_least_square_of(-5)  # negative requirement always met


def test_point_segment_distance_regions() -> None:
    a, b = P(0, 0), P(10, 0)
    assert point_segment_d2(P(5, 3), a, b).sqrt() == 3  # interior
    assert point_segment_d2(P(-3, 4), a, b).sqrt() == 5  # before start
    assert point_segment_d2(P(13, 4), a, b).sqrt() == 5  # after end
    assert point_segment_d2(P(3, 3), a, a).sqrt() == pytest.approx(math.hypot(3, 3))


def test_segment_intersection_cases() -> None:
    assert segments_intersect(P(0, 0), P(10, 10), P(0, 10), P(10, 0))  # cross
    assert segments_intersect(P(0, 0), P(10, 0), P(10, 0), P(20, 5))  # touching end
    assert segments_intersect(P(0, 0), P(10, 0), P(5, 0), P(15, 0))  # collinear overlap
    assert not segments_intersect(P(0, 0), P(10, 0), P(11, 0), P(15, 0))  # collinear gap
    assert not segments_intersect(P(0, 0), P(10, 0), P(0, 1), P(10, 1))  # parallel
    assert segment_segment_d2(P(0, 0), P(10, 0), P(0, 3), P(10, 3)).sqrt() == 3


# ------------------------------------------------------------------ polygon
def test_polygon_validation_and_basics() -> None:
    with pytest.raises(InvalidGeometryError):
        Polygon([P(0, 0), P(1, 1)])
    sq = Polygon([P(0, 0), P(10, 0), P(10, 10), P(0, 10), P(0, 0)])  # closing vertex dropped
    assert len(sq.points) == 4
    assert sq.area == 100
    assert sq.contains(P(5, 5)) and sq.contains(P(0, 5)) and sq.contains(P(10, 10))
    assert not sq.contains(P(11, 5)) and not sq.contains(P(-1, -1))


def test_concave_polygon_containment() -> None:
    u = Polygon(
        [P(0, 0), P(30, 0), P(30, 30), P(20, 30), P(20, 10), P(10, 10), P(10, 30), P(0, 30)]
    )
    assert u.contains(P(5, 20)) and u.contains(P(25, 20))
    assert not u.contains(P(15, 20))  # inside the notch


def test_large_polygon_acceleration_matches_bruteforce() -> None:
    n = 400
    pts = [
        P(round(1000 * math.cos(2 * math.pi * i / n)), round(1000 * math.sin(2 * math.pi * i / n)))
        for i in range(n)
    ]
    big = Polygon(pts)
    for probe in (P(0, 0), P(999, 0), P(1001, 0), P(700, 700), P(710, 710), P(-5, 998)):
        brute = sum(1 for _ in big.edges())
        assert brute == n
        expected = math.hypot(probe.x, probe.y) <= 999.9
        assert big.contains(probe) == expected, probe
    box = BoundingBox(900, -50, 1100, 50)
    near = list(big.edges_near(box))
    assert 0 < len(near) < n


def test_convex_hull() -> None:
    hull = convex_hull([P(0, 0), P(10, 0), P(5, 5), P(10, 10), P(0, 10), P(5, 2)])
    assert set(hull) == {P(0, 0), P(10, 0), P(10, 10), P(0, 10)}


# ------------------------------------------------------------------ shapes
def test_capsule_models_full_track_width() -> None:
    track = capsule(P(0, 0), P(10 * MM, 0), 200_000)  # 0.40 mm wide
    assert track.bounds == BoundingBox(-200_000, -200_000, 10 * MM + 200_000, 200_000)
    assert track.contains(P(5 * MM, 200_000))  # edge of the copper
    assert not track.contains(P(5 * MM, 200_001))
    assert track.contains(P(-200_000, 0))  # round end cap


def test_zero_length_track_is_a_disk() -> None:
    dot = capsule(P(5, 5), P(5, 5), 100)
    assert isinstance(dot.core, PointCore) and dot.contains(P(105, 5))


def test_rectangle_rotation_and_rounding() -> None:
    r = rectangle(P(0, 0), 2 * MM, 1 * MM)
    assert r.contains(P(MM, 500_000)) and not r.contains(P(MM + 1, 0))
    rot = rectangle(P(0, 0), 2 * MM, 1 * MM, 90)
    assert rot.contains(P(0, MM)) and not rot.contains(P(MM, 0))
    rr = rectangle(P(0, 0), 2 * MM, 1 * MM, corner_radius=250_000)
    assert rr.radius == 250_000 and rr.bounds == BoundingBox(-MM, -500_000, MM, 500_000)
    assert not rr.contains(P(990_000, 490_000))  # cut by the rounded corner


def test_oval_is_exact_capsule() -> None:
    o = oval(P(0, 0), 3 * MM, 1 * MM)
    assert isinstance(o.core, SegmentCore) and o.radius == 500_000
    assert o.core.a == P(-MM, 0) and o.core.b == P(MM, 0)
    vertical = oval(P(0, 0), 1 * MM, 3 * MM)
    assert isinstance(vertical.core, SegmentCore) and vertical.core.a.x == 0
    assert isinstance(oval(P(0, 0), MM, MM).core, PointCore)  # round


def test_highly_rotated_rectangle() -> None:
    r = rectangle(P(0, 0), 4 * MM, 1 * MM, 37.0)
    probe = Point(
        round(1.9 * MM * math.cos(math.radians(37))), -round(1.9 * MM * math.sin(math.radians(37)))
    )
    assert r.contains(probe)
    assert not r.contains(
        Point(
            round(1.9 * MM * math.cos(math.radians(37))),
            round(1.9 * MM * math.sin(math.radians(37))),
        )
    )


# ------------------------------------------------------------------ clearance semantics
def test_clearance_exact_boundary_is_legal() -> None:
    track = capsule(P(0, 0), P(10 * MM, 0), 200_000)
    pad = circle(P(5 * MM, 600_000), 200_000)  # gap exactly 0.200000 mm
    assert gap_between(track, pad) == pytest.approx(200_000)
    assert meets_clearance(track, pad, 200_000)  # equal => legal
    assert meets_clearance(track, pad, 200_000 + GEOMETRY_TOLERANCE_NM)  # within tolerance
    assert not meets_clearance(track, pad, 200_000 + GEOMETRY_TOLERANCE_NM + 1)
    slightly_inside = circle(P(5 * MM, 599_990), 200_000)
    assert not meets_clearance(track, slightly_inside, 200_000)
    slightly_outside = circle(P(5 * MM, 600_010), 200_000)
    assert meets_clearance(track, slightly_outside, 200_000)


def test_check_clearance_reports_values() -> None:
    track = capsule(P(0, 0), P(10 * MM, 0), 200_000)
    pad = circle(P(5 * MM, 590_000), 200_000)
    res = check_clearance(track, pad, 250_000)
    assert not res.ok and res.gap == pytest.approx(190_000) and res.required == 250_000
    assert res.location is not None and abs(res.location.x - 5 * MM) < MM
    far = check_clearance(track, circle(P(5 * MM, 50 * MM), 1), 250_000)
    assert far.ok and far.gap is None


def test_touch_and_overlap() -> None:
    a = capsule(P(0, 0), P(10, 0), 5)
    assert touches(a, circle(P(0, 10), 5))  # exactly tangent
    assert not touches(a, circle(P(0, 12), 5))
    assert overlaps(a, circle(P(0, 8), 5))
    assert not overlaps(a, circle(P(0, 10), 5))  # tangent is not a short


def test_polygon_distances() -> None:
    sq = polygon([P(0, 0), P(10, 0), P(10, 10), P(0, 10)])
    d = core_distance2(sq.core, PointCore(P(13, 14)))
    assert d is not None and d.sqrt() == 5
    inside = core_distance2(sq.core, SegmentCore(P(2, 2), P(3, 3)))
    assert inside is not None and inside.is_zero()
    other = Polygon([P(20, 0), P(30, 0), P(30, 10), P(20, 10)])
    d2 = core_distance2(sq.core, PolygonCore(other))
    assert d2 is not None and d2.sqrt() == 10
    assert core_distance2(sq.core, PolygonCore(other), limit=5) is None  # farther than limit


def test_inflation_is_exact_envelope() -> None:
    pad = rectangle(P(0, 0), MM, MM)
    env = pad.inflated(200_000)
    assert env.radius == 200_000
    assert env.contains(P(700_000, 0)) and not env.contains(P(700_001, 0))
    corner = P(500_000 + 141_421, 500_000 + 141_421)
    assert env.contains(corner)  # round corner of the envelope
    assert not env.contains(P(700_000, 700_000))


def test_outline_points_for_display() -> None:
    for s in (
        circle(P(0, 0), 100),
        capsule(P(0, 0), P(100, 0), 10),
        rectangle(P(0, 0), 100, 50, 0, 5),
    ):
        pts = outline_points(s)
        assert len(pts) >= 4


# ------------------------------------------------------------------ arcs / paths
def test_arc_chords_are_conservative() -> None:
    start, mid, end = P(-MM, 0), P(0, -MM), P(MM, 0)
    pts, sagitta = arc_chords(start, mid, end)
    assert pts[0] == start and pts[-1] == end
    assert 0 < sagitta <= ARC_MAX_SAGITTA_NM + 1
    shapes = arc_capsules(start, mid, end, 100_000)
    assert all(s.accuracy is ShapeAccuracy.CONSERVATIVE for s in shapes)
    # Every point of the real arc centreline is covered by the capsules.
    for k in range(0, 181, 7):
        t = math.radians(k)
        on_arc = P(round(MM * math.cos(t)), -round(MM * math.sin(t)))
        assert any(s.contains(on_arc) for s in shapes)
    straight, s0 = arc_chords(P(0, 0), P(5, 0), P(10, 0))
    assert straight == [P(0, 0), P(10, 0)] and s0 == 0


# ------------------------------------------------------------------ transforms
def test_transform_rotation_translation_mirror() -> None:
    t = Transform(P(100, 200), 90)
    assert t.apply(P(10, 0)) == P(100, 190)  # CCW on screen (Y down)
    assert t.inverse_apply(t.apply(P(7, -3))) == P(7, -3)
    m = Transform(P(0, 0), 0, mirror_x=True)
    assert m.apply(P(10, 5)) == P(-10, 5)
    assert m.inverse_apply(m.apply(P(3, 4))) == P(3, 4)
    composed = Transform(P(10, 0), 90).then(Transform(P(0, 0), 90))
    assert composed.apply(P(1, 0)) == Transform(P(0, 0), 90).apply(
        Transform(P(10, 0), 90).apply(P(1, 0))
    )


def test_pad_local_to_board() -> None:
    # A shape offset of +1 mm along the pad X axis, pad rotated 90 degrees.
    assert pad_local_to_board(P(5 * MM, 5 * MM), 90, P(MM, 0)) == P(5 * MM, 4 * MM)


# ------------------------------------------------------------------ primitives API
def test_primitive_value_types() -> None:
    seg = Segment(P(0, 0), P(10, 0))
    c = Circle(P(5, 8), 3)
    assert seg.distance_to(c) == 5 and not seg.intersects(c)
    assert Capsule(P(0, 0), P(10, 0), 5).intersects(c)
    assert seg.translated(1, 1) == Segment(P(1, 1), P(11, 1))
    assert seg.rotated(90, P(0, 0)).b == P(0, -10)
    assert Rectangle(BoundingBox(0, 0, 10, 10)).contains(P(10, 10))
    assert OrientedRectangle(P(0, 0), 10, 4, 90).contains(P(0, 5))
    rr = RoundedRectangle(P(0, 0), 100, 50, 10)
    assert rr.bounds() == BoundingBox(-50, -25, 50, 25)
    env = rr.expanded(5)
    assert env[0].radius == 15
    assert Arc(P(-10, 0), P(0, -10), P(10, 0)).bounds().min_y <= -10
    assert Vector(3, 4).length == 5 and Vector(1, 0).cross(Vector(0, 1)) == 1
    line = Line(P(0, 0), P(10, 0))
    assert line.side(P(5, 5)) != line.side(P(5, -5)) and line.distance_to(P(3, 7)) == 7
    assert Line(P(0, 0), P(10, 10)).intersection(Line(P(0, 10), P(10, 0))) == (5.0, 5.0)


# ------------------------------------------------------------------ raster
def test_distance_field_matches_exact() -> None:
    xs1 = centers(0, 100, 20)
    ys1 = centers(0, 100, 20)
    xs, ys = np.meshgrid(xs1, ys1)
    window = BoundingBox(0, 0, 2000, 2000)
    seg = SegmentCore(P(500, 500), P(1500, 500))
    field = distance_field(seg, xs, ys, window)
    exact = point_segment_d2(P(1050, 1050), seg.a, seg.b).sqrt()
    assert field[10, 10] == pytest.approx(exact)
    square = PolygonCore(Polygon([P(400, 400), P(1600, 400), P(1600, 1600), P(400, 1600)]))
    pf = distance_field(square, xs, ys, window)
    assert pf[10, 10] == 0.0 and pf[0, 0] > 0
