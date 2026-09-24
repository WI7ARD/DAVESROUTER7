"""Grid path → clean octilinear track geometry.

1. Split the cell path into per-layer runs; a layer change is a via at that cell.
2. Collapse collinear cells: 100 cells in a row become one segment.
3. Shortcut: from each corner, jump to the farthest later corner reachable by one
   or two octilinear segments (straight + 45°, either order) that the *exact*
   validator accepts and that keeps every turn at 90° or less. This removes grid
   staircases while never producing an acute angle or unchecked geometry.
4. Snap the route ends to the centre of the pad they start/end in when that
   segment is legal too (clean KiCad connections).

Every segment emitted here is validated again as part of the final route check.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass

from pcbrouter.domain.geometry import Point
from pcbrouter.routing.search.grid import SearchGrid

MAX_LOOKAHEAD = 48

SegmentCheck = Callable[[str, Point, Point], bool]


@dataclass(frozen=True, slots=True)
class LayerRun:
    layer: str
    points: tuple[Point, ...]  # corners, first/last are run ends


def _sign(v: int) -> int:
    return (v > 0) - (v < 0)


def _direction(a: Point, b: Point) -> tuple[int, int]:
    return _sign(b.x - a.x), _sign(b.y - a.y)


def turn_ok(d1: tuple[int, int], d2: tuple[int, int]) -> bool:
    """Turn between two octilinear unit directions is at most 90 degrees."""
    return d1[0] * d2[0] + d1[1] * d2[1] >= 0


def runs_from_path(grid: SearchGrid, path: list[tuple[int, int]]) -> list[LayerRun]:
    runs: list[LayerRun] = []
    current: list[Point] = []
    layer_idx = path[0][0]
    for li, idx in path:
        p = grid.center(idx)
        if li != layer_idx:
            runs.append(LayerRun(grid.layers[layer_idx], tuple(current)))
            current = [p]
            layer_idx = li
            continue
        current.append(p)
    runs.append(LayerRun(grid.layers[layer_idx], tuple(current)))
    return [LayerRun(r.layer, collapse_collinear(r.points)) for r in runs]


def collapse_collinear(points: tuple[Point, ...] | list[Point]) -> tuple[Point, ...]:
    pts = [p for i, p in enumerate(points) if i == 0 or p != points[i - 1]]
    if len(pts) <= 2:
        return tuple(pts)
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        if _direction(out[-1], pts[i]) != _direction(pts[i], pts[i + 1]):
            out.append(pts[i])
    out.append(pts[-1])
    return tuple(out)


def octilinear_links(a: Point, b: Point) -> list[list[Point]]:
    """Ways to join a→b with one or two octilinear segments (as point lists)."""
    dx, dy = b.x - a.x, b.y - a.y
    if dx == 0 or dy == 0 or abs(dx) == abs(dy):
        return [[a, b]]
    sx, sy = _sign(dx), _sign(dy)
    m = min(abs(dx), abs(dy))
    diag_first = Point(a.x + sx * m, a.y + sy * m)
    straight_first = Point(b.x - sx * m, b.y - sy * m)
    return [[a, diag_first, b], [a, straight_first, b]]


def shortcut(points: tuple[Point, ...], layer: str, check: SegmentCheck) -> tuple[Point, ...]:
    """Greedy farthest-reachable octilinear shortcutting (see module docstring)."""
    if len(points) <= 2:
        return points
    out: list[Point] = [points[0]]
    i = 0
    n = len(points)
    while i < n - 1:
        advanced = False
        for j in range(min(n - 1, i + MAX_LOOKAHEAD), i, -1):
            for link in octilinear_links(points[i], points[j]):
                if not _turns_ok(out, link, points[j + 1] if j + 1 < n else None):
                    continue
                if all(check(layer, a, b) for a, b in itertools.pairwise(link)):
                    out.extend(link[1:])
                    i = j
                    advanced = True
                    break
            if advanced:
                break
        if not advanced:  # keep the grid segment; the final validation will judge it
            out.append(points[i + 1])
            i += 1
    return collapse_collinear(out)


def _turns_ok(prefix: list[Point], link: list[Point], after: Point | None) -> bool:
    seq = [*prefix[-1:], *link[1:]]
    if len(prefix) >= 2:
        seq = [prefix[-2], *seq]
    if after is not None:
        seq.append(after)
    dirs = [_direction(a, b) for a, b in itertools.pairwise(seq) if a != b]
    return all(turn_ok(d1, d2) for d1, d2 in itertools.pairwise(dirs))


def snap_end(
    points: tuple[Point, ...], anchor: Point, layer: str, check: SegmentCheck, at_start: bool
) -> tuple[Point, ...]:
    """Replace the first (or last) point by ``anchor`` when that stays legal and
    octilinear with no sharp turn."""
    if len(points) < 2:
        return points
    pts = list(points) if at_start else list(reversed(points))
    if pts[0] == anchor:
        return points
    nxt = pts[1]
    dx, dy = nxt.x - anchor.x, nxt.y - anchor.y
    if not (dx == 0 or dy == 0 or abs(dx) == abs(dy)):
        return points
    candidate = [anchor, *pts[1:]]
    dirs = [_direction(a, b) for a, b in itertools.pairwise(candidate)]
    if len(dirs) == 2 and not turn_ok(dirs[0], dirs[1]):
        return points
    if not check(layer, anchor, nxt):
        return points
    out = tuple(candidate) if at_start else tuple(reversed(candidate))
    return collapse_collinear(out)
