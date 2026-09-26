"""Exact integer / rational predicates on nanometre coordinates.

Every geometric *decision* in the engine (does it intersect? is the gap at least the
required clearance?) is made here with Python integers, which never overflow and
never round. Floating point is only used to *report* a distance to a person.

Squared distances are exact rationals ``(num, den)`` with ``den > 0``: the squared
distance from a point to the interior of a segment is ``cross**2 / |d|**2``, which
is rational even though the distance itself usually is not.
"""

from __future__ import annotations

import math

from pcbrouter.domain.geometry import Point


class Rational:
    """Exact non-negative rational ``num / den`` (``den > 0``). Comparisons and
    equality are exact (``1/2 == 2/4``)."""

    __slots__ = ("den", "num")

    def __init__(self, num: int, den: int) -> None:
        self.num = num
        self.den = den

    def __repr__(self) -> str:
        return f"Rational({self.num}, {self.den})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Rational):
            return NotImplemented
        return self.num * other.den == other.num * self.den

    def __hash__(self) -> int:
        g = math.gcd(self.num, self.den) or 1
        return hash((self.num // g, self.den // g))

    def __lt__(self, other: Rational) -> bool:
        return self.num * other.den < other.num * self.den

    def __le__(self, other: Rational) -> bool:
        return self.num * other.den <= other.num * self.den

    def __gt__(self, other: Rational) -> bool:
        return self.num * other.den > other.num * self.den

    def __ge__(self, other: Rational) -> bool:
        return self.num * other.den >= other.num * self.den

    def is_zero(self) -> bool:
        return self.num == 0

    def at_least_square_of(self, value: int) -> bool:
        """``self >= value**2`` exactly (``value`` in nm, may be negative)."""
        if value <= 0:
            return True
        return self.num >= value * value * self.den

    def below_square_of(self, value: int) -> bool:
        return not self.at_least_square_of(value)

    def sqrt(self) -> float:
        """The (usually irrational) root as a float — for reporting only."""
        return math.sqrt(self.num / self.den) if self.num else 0.0


ZERO = Rational(0, 1)


def rational_min(a: Rational, b: Rational) -> Rational:
    return a if a <= b else b


def cross(o: Point, a: Point, b: Point) -> int:
    """Z of (a - o) x (b - o): > 0 counter-clockwise (in Y-up terms), 0 collinear."""
    return (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x)


def sign(v: int) -> int:
    return (v > 0) - (v < 0)


def on_segment(p: Point, a: Point, b: Point) -> bool:
    """``p`` lies on the closed segment ``a-b`` (exact)."""
    if cross(a, b, p) != 0:
        return False
    return min(a.x, b.x) <= p.x <= max(a.x, b.x) and min(a.y, b.y) <= p.y <= max(a.y, b.y)


def segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Closed segments ``a-b`` and ``c-d`` share at least one point (exact, including
    touching endpoints and collinear overlap). Degenerate segments are points."""
    d1 = sign(cross(c, d, a))
    d2 = sign(cross(c, d, b))
    d3 = sign(cross(a, b, c))
    d4 = sign(cross(a, b, d))
    if d1 * d2 < 0 and d3 * d4 < 0:
        return True
    return (
        (d1 == 0 and on_segment(a, c, d))
        or (d2 == 0 and on_segment(b, c, d))
        or (d3 == 0 and on_segment(c, a, b))
        or (d4 == 0 and on_segment(d, a, b))
    )


def point_point_d2(p: Point, q: Point) -> Rational:
    dx, dy = p.x - q.x, p.y - q.y
    return Rational(dx * dx + dy * dy, 1)


def point_segment_d2(p: Point, a: Point, b: Point) -> Rational:
    """Exact squared distance from ``p`` to the closed segment ``a-b``."""
    dx, dy = b.x - a.x, b.y - a.y
    vx, vy = p.x - a.x, p.y - a.y
    dot = vx * dx + vy * dy
    if dot <= 0:
        return Rational(vx * vx + vy * vy, 1)
    len2 = dx * dx + dy * dy
    if dot >= len2:
        wx, wy = p.x - b.x, p.y - b.y
        return Rational(wx * wx + wy * wy, 1)
    c = vx * dy - vy * dx
    return Rational(c * c, len2)


def segment_segment_d2(a: Point, b: Point, c: Point, d: Point) -> Rational:
    """Exact squared distance between closed segments (0 if they intersect)."""
    if segments_intersect(a, b, c, d):
        return ZERO
    best = point_segment_d2(a, c, d)
    for cand in (point_segment_d2(b, c, d), point_segment_d2(c, a, b), point_segment_d2(d, a, b)):
        if cand < best:
            best = cand
    return best


def closest_point_on_segment(p: Point, a: Point, b: Point) -> Point:
    """Nearest point of segment ``a-b`` to ``p``, rounded to the nm grid (reporting)."""
    dx, dy = b.x - a.x, b.y - a.y
    len2 = dx * dx + dy * dy
    if len2 == 0:
        return a
    t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2
    t = min(1.0, max(0.0, t))
    return Point(round(a.x + t * dx), round(a.y + t * dy))


def isqrt_ceil(n: int) -> int:
    """Smallest integer ``r`` with ``r*r >= n``."""
    r = math.isqrt(n)
    return r if r * r == n else r + 1
