"""Simple polygons with exact containment and accelerated edge queries.

Containment uses the even-odd rule with exact integer arithmetic; points *on* the
boundary count as inside (distance 0). Large polygons (zone fills can have thousands
of vertices) lazily build two small indexes so queries do not scan every edge:

* a uniform grid of edges, for "which edges are near this box?";
* horizontal bands of edges, for the containment ray test.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator, Sequence

from pcbrouter.domain.geometry import BoundingBox, Point, rotate_point
from pcbrouter.geometry.errors import InvalidGeometryError
from pcbrouter.geometry.exact import on_segment

#: Polygons with more edges than this build acceleration indexes on first use.
ACCELERATE_ABOVE_EDGES = 32


class Polygon:
    """An immutable simple polygon (closed implicitly; no repeated closing vertex)."""

    __slots__ = ("_bands", "_edge_grid", "bounds", "points")

    def __init__(self, points: Iterable[Point]) -> None:
        pts = list(points)
        if len(pts) >= 2 and pts[0] == pts[-1]:
            pts.pop()
        # Drop consecutive duplicates (they create zero-length edges).
        cleaned: list[Point] = []
        for p in pts:
            if not cleaned or cleaned[-1] != p:
                cleaned.append(p)
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1]:
            cleaned.pop()
        if len(cleaned) < 3:
            raise InvalidGeometryError(f"polygon needs at least 3 distinct vertices, got {pts}")
        self.points: tuple[Point, ...] = tuple(cleaned)
        box = BoundingBox.from_points(self.points)
        assert box is not None
        self.bounds: BoundingBox = box
        self._edge_grid: _EdgeGrid | None = None
        self._bands: _Bands | None = None

    def __repr__(self) -> str:
        return f"Polygon({len(self.points)} vertices, bounds={self.bounds})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Polygon) and self.points == other.points

    def __hash__(self) -> int:
        return hash(self.points)

    # ------------------------------------------------------------ basics
    @property
    def edge_count(self) -> int:
        return len(self.points)

    def edge(self, i: int) -> tuple[Point, Point]:
        pts = self.points
        return pts[i], pts[(i + 1) % len(pts)]

    def edges(self) -> Iterator[tuple[Point, Point]]:
        pts = self.points
        n = len(pts)
        for i in range(n):
            yield pts[i], pts[(i + 1) % n]

    def area2(self) -> int:
        """Twice the signed area (shoelace). Positive = counter-clockwise in Y-up."""
        pts = self.points
        n = len(pts)
        return sum(pts[i].x * pts[(i + 1) % n].y - pts[(i + 1) % n].x * pts[i].y for i in range(n))

    @property
    def area(self) -> float:
        return abs(self.area2()) / 2

    def translated(self, dx: int, dy: int) -> Polygon:
        return Polygon(Point(p.x + dx, p.y + dy) for p in self.points)

    def rotated(self, angle_deg: float, center: Point) -> Polygon:
        return Polygon(rotate_point(p, angle_deg, center) for p in self.points)

    # ------------------------------------------------------------ queries
    def contains(self, p: Point) -> bool:
        """Even-odd containment; boundary points count as inside. Exact."""
        if not self.bounds.contains(p):
            return False
        inside = False
        for a, b in self._edges_for_row(p.y):
            if on_segment(p, a, b):
                return True
            if (a.y > p.y) != (b.y > p.y):
                # Sign of (x_intersection - p.x), multiplied out to stay in integers.
                num = (a.x - p.x) * (b.y - a.y) + (p.y - a.y) * (b.x - a.x)
                if (num > 0) == (b.y - a.y > 0) and num != 0:
                    inside = not inside
        return inside

    def edges_near(self, box: BoundingBox) -> Iterable[tuple[Point, Point]]:
        """Edges whose bounds may intersect ``box`` (a superset; never misses one)."""
        if len(self.points) <= ACCELERATE_ABOVE_EDGES:
            return self.edges()
        if self._edge_grid is None:
            self._edge_grid = _EdgeGrid(self)
        return (self.edge(i) for i in self._edge_grid.query(box))

    def _edges_for_row(self, y: int) -> Iterable[tuple[Point, Point]]:
        if len(self.points) <= ACCELERATE_ABOVE_EDGES:
            return self.edges()
        if self._bands is None:
            self._bands = _Bands(self)
        return (self.edge(i) for i in self._bands.query(y))


class _EdgeGrid:
    """Uniform grid over the polygon bounds mapping cells to edge indices."""

    __slots__ = ("cell", "cells", "min_x", "min_y")

    def __init__(self, poly: Polygon) -> None:
        b = poly.bounds
        n = poly.edge_count
        side = max(b.width, b.height, 1)
        self.cell = max(1, side // max(1, int(math.sqrt(n))))
        self.min_x, self.min_y = b.min_x, b.min_y
        self.cells: dict[tuple[int, int], list[int]] = {}
        for i, (a, c) in enumerate(poly.edges()):
            for key in self._keys(min(a.x, c.x), min(a.y, c.y), max(a.x, c.x), max(a.y, c.y)):
                self.cells.setdefault(key, []).append(i)

    def _keys(self, x0: int, y0: int, x1: int, y1: int) -> Iterator[tuple[int, int]]:
        c = self.cell
        for gx in range((x0 - self.min_x) // c, (x1 - self.min_x) // c + 1):
            for gy in range((y0 - self.min_y) // c, (y1 - self.min_y) // c + 1):
                yield gx, gy

    def query(self, box: BoundingBox) -> list[int]:
        found: set[int] = set()
        for key in self._keys(box.min_x, box.min_y, box.max_x, box.max_y):
            found.update(self.cells.get(key, ()))
        return sorted(found)


class _Bands:
    """Horizontal bands mapping y ranges to the edges spanning them."""

    __slots__ = ("band", "bands", "min_y")

    def __init__(self, poly: Polygon) -> None:
        b = poly.bounds
        count = max(1, int(math.sqrt(poly.edge_count)))
        self.band = max(1, (b.height + count) // count)
        self.min_y = b.min_y
        self.bands: dict[int, list[int]] = {}
        for i, (a, c) in enumerate(poly.edges()):
            lo, hi = min(a.y, c.y), max(a.y, c.y)
            for k in range((lo - self.min_y) // self.band, (hi - self.min_y) // self.band + 1):
                self.bands.setdefault(k, []).append(i)

    def query(self, y: int) -> Sequence[int]:
        return self.bands.get((y - self.min_y) // self.band, ())


def convex_hull(points: Iterable[Point]) -> list[Point]:
    """Monotone-chain convex hull (exact). Used to bound Bezier curves."""
    pts = sorted(set(points), key=lambda p: (p.x, p.y))
    if len(pts) <= 2:
        return pts

    def half(seq: list[Point]) -> list[Point]:
        out: list[Point] = []
        for p in seq:
            while (
                len(out) >= 2
                and (out[-1].x - out[-2].x) * (p.y - out[-2].y)
                - (out[-1].y - out[-2].y) * (p.x - out[-2].x)
                <= 0
            ):
                out.pop()
            out.append(p)
        return out

    lower = half(pts)
    upper = half(list(reversed(pts)))
    return lower[:-1] + upper[:-1]
