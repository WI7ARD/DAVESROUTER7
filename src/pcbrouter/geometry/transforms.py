"""Rigid 2D transforms (translate, rotate, mirror) on integer points.

Board convention (KiCad): X right, Y *down*, positive angles counter-clockwise on
screen. Placement transforms compose as ``board = T(position) * R(rotation) * M(mirror)
* local``.

Note on back-side footprints: KiCad writes a flipped footprint's pads and graphics
into the board file *already mirrored* (and on B.* layers), with the pad angle
stored absolute. The adapter therefore maps local -> board with translate+rotate
only; :attr:`Transform.mirror_x` exists for footprint-library geometry (not used by
the board loader) and is tested so later stages can rely on it.
"""

from __future__ import annotations

from dataclasses import dataclass

from pcbrouter.domain.geometry import ORIGIN, Point, rotate_point


@dataclass(frozen=True, slots=True)
class Transform:
    translation: Point = ORIGIN
    rotation_deg: float = 0.0
    #: Mirror about the local Y axis (x -> -x) before rotating (KiCad "flip").
    mirror_x: bool = False

    def apply(self, p: Point) -> Point:
        local = Point(-p.x, p.y) if self.mirror_x else p
        rotated = rotate_point(local, self.rotation_deg, ORIGIN)
        return Point(rotated.x + self.translation.x, rotated.y + self.translation.y)

    def apply_all(self, points: list[Point] | tuple[Point, ...]) -> list[Point]:
        return [self.apply(p) for p in points]

    def inverse_apply(self, p: Point) -> Point:
        local = Point(p.x - self.translation.x, p.y - self.translation.y)
        unrotated = rotate_point(local, -self.rotation_deg, ORIGIN)
        return Point(-unrotated.x, unrotated.y) if self.mirror_x else unrotated

    def then(self, outer: Transform) -> Transform:
        """``outer`` applied after ``self`` (only for non-mirrored composition)."""
        if self.mirror_x or outer.mirror_x:
            raise ValueError("composition of mirrored transforms is not supported")
        moved = outer.apply(self.translation)
        return Transform(moved, (self.rotation_deg + outer.rotation_deg) % 360.0)


def pad_local_to_board(pad_position: Point, pad_rotation_deg: float, local: Point) -> Point:
    """A point given in a pad's own frame (relative to the pad position, before pad
    rotation) -> absolute board coordinates. Pad angles in board files are absolute."""
    return rotate_point(
        Point(pad_position.x + local.x, pad_position.y + local.y), pad_rotation_deg, pad_position
    )
