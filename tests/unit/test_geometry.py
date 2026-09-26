from __future__ import annotations

import math

import pytest

from pcbrouter.domain.geometry import (
    BoundingBox,
    Point,
    arc_length,
    arc_mid_from_center,
    arc_points,
    arc_sweep_deg,
    circle_from_three_points,
    rotate_point,
    union_all,
)

MM = 1_000_000


class TestRotatePoint:
    def test_quarter_turns_follow_kicad_ccw_on_screen(self) -> None:
        # Y points down, so rotating +X by +90 (CCW on screen) points "up" = -Y.
        assert rotate_point(Point(MM, 0), 90) == Point(0, -MM)
        assert rotate_point(Point(MM, 0), 180) == Point(-MM, 0)
        assert rotate_point(Point(MM, 0), 270) == Point(0, MM)
        assert rotate_point(Point(MM, 0), -90) == Point(0, MM)
        assert rotate_point(Point(MM, 0), 360) == Point(MM, 0)

    def test_quarter_turns_are_exact(self) -> None:
        p = Point(123_457, -987_653)
        assert rotate_point(rotate_point(p, 90), -90) == p

    def test_about_center(self) -> None:
        c = Point(10 * MM, 10 * MM)
        assert rotate_point(Point(11 * MM, 10 * MM), 90, c) == Point(10 * MM, 9 * MM)

    def test_arbitrary_angle(self) -> None:
        r = rotate_point(Point(MM, 0), 45)
        assert r.x == round(MM / math.sqrt(2))
        assert r.y == -round(MM / math.sqrt(2))


class TestBoundingBox:
    def test_from_points(self) -> None:
        box = BoundingBox.from_points([Point(1, 5), Point(-2, 3), Point(4, -1)])
        assert box == BoundingBox(-2, -1, 4, 5)
        assert box is not None and box.width == 6 and box.height == 6

    def test_empty(self) -> None:
        assert BoundingBox.from_points([]) is None
        assert union_all([None, None]) is None

    def test_invalid_rejected(self) -> None:
        with pytest.raises(ValueError):
            BoundingBox(5, 0, 1, 1)

    def test_union_expand_contains_intersects(self) -> None:
        a = BoundingBox(0, 0, 10, 10)
        b = BoundingBox(5, 5, 20, 20)
        assert a.union(b) == BoundingBox(0, 0, 20, 20)
        assert union_all([a, None, b]) == BoundingBox(0, 0, 20, 20)
        assert a.expanded(2) == BoundingBox(-2, -2, 12, 12)
        assert a.contains(Point(10, 10)) and not a.contains(Point(11, 0))
        assert a.intersects(b)
        assert not a.intersects(BoundingBox(11, 11, 12, 12))
        assert a.center == Point(5, 5)

    def test_around(self) -> None:
        assert BoundingBox.around(Point(0, 0), 3) == BoundingBox(-3, -3, 3, 3)


class TestArcs:
    def test_semicircle_length(self) -> None:
        start, mid, end = Point(0, 0), Point(-10 * MM, 10 * MM), Point(0, 20 * MM)
        assert arc_length(start, mid, end) == pytest.approx(math.pi * 10 * MM)

    def test_quarter_arc_sweep_sign(self) -> None:
        # (r,0) -> (0,-r) on screen is CCW (upwards) => positive sweep in Y-up terms.
        start, end = Point(MM, 0), Point(0, -MM)
        mid = rotate_point(start, 45)
        sweep = arc_sweep_deg(start, mid, end)
        assert sweep == pytest.approx(90, abs=1e-3)

    def test_large_arc_goes_the_long_way(self) -> None:
        start, end = Point(MM, 0), Point(0, -MM)
        mid = rotate_point(start, -135)  # through the far side
        sweep = arc_sweep_deg(start, mid, end)
        assert sweep is not None and abs(sweep) == pytest.approx(270, abs=1e-3)

    def test_collinear_is_degenerate(self) -> None:
        a, b, c = Point(0, 0), Point(1, 0), Point(2, 0)
        assert circle_from_three_points(a, b, c) is None
        assert arc_length(a, b, c) == pytest.approx(2)
        assert arc_points(a, b, c) == [a, c]

    def test_arc_points_endpoints_and_radius(self) -> None:
        start, mid, end = Point(0, 0), Point(-10 * MM, 10 * MM), Point(0, 20 * MM)
        pts = arc_points(start, mid, end)
        assert pts[0] == start and pts[-1] == end
        for p in pts:
            assert p.distance_to(Point(0, 10 * MM)) == pytest.approx(10 * MM, rel=1e-6)

    def test_kicad5_center_form(self) -> None:
        center, start = Point(40 * MM, 50 * MM), Point(40 * MM, 40 * MM)
        mid, end = arc_mid_from_center(center, start, -180)
        assert end == Point(40 * MM, 60 * MM)
        assert mid == Point(30 * MM, 50 * MM)
