from __future__ import annotations

from pcbrouter.domain import BoardOutline, BoundingBox, OutlineSegment, OutlineShape, Point
from tests.unit.test_domain_models import MM, make_board


def test_bounds_prefer_outline_over_content() -> None:
    outline = BoardOutline(
        (
            OutlineSegment(OutlineShape.LINE, Point(0, 0), Point(100 * MM, 0)),
            OutlineSegment(OutlineShape.LINE, Point(100 * MM, 0), Point(100 * MM, 50 * MM)),
        )
    )
    board = make_board(outline)
    assert board.bounds == BoundingBox(0, 0, 100 * MM, 50 * MM)
    assert board.statistics.width == 100 * MM
    assert board.statistics.height == 50 * MM


def test_bounds_fall_back_to_content_without_outline() -> None:
    board = make_board()
    bounds = board.bounds
    assert bounds is not None
    # Footprint body is 8..12 x 9..11 mm; tracks reach (12, 24) mm plus half width.
    assert bounds.min_x <= 8 * MM
    assert bounds.max_y >= 24 * MM
    assert board.statistics.width is None  # no outline => size unknown, not invented


def test_arc_and_circle_outline_bounds() -> None:
    arc = OutlineSegment(OutlineShape.ARC, Point(0, 0), Point(0, 20 * MM), Point(-10 * MM, 10 * MM))
    circle = OutlineSegment(OutlineShape.CIRCLE, Point(50 * MM, 0), Point(55 * MM, 0))
    bounds = BoardOutline((arc, circle)).bounds
    assert bounds is not None
    assert bounds.min_x == -10 * MM
    assert bounds.max_x == 55 * MM
    assert bounds.min_y == -5 * MM
    assert bounds.max_y == 20 * MM
