"""Footprint pads."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pcbrouter.domain.geometry import ORIGIN, BoundingBox, Point, rotate_point
from pcbrouter.domain.units import Nm


class PadShape(Enum):
    CIRCLE = "circle"
    RECT = "rect"
    OVAL = "oval"
    ROUNDRECT = "roundrect"
    TRAPEZOID = "trapezoid"
    CUSTOM = "custom"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, text: str) -> PadShape:
        try:
            return cls(text)
        except ValueError:
            return cls.UNKNOWN


class PadType(Enum):
    SMD = "smd"
    THROUGH_HOLE = "thru_hole"
    NPTH = "np_thru_hole"
    CONNECT = "connect"  # edge connector / copper-only, no paste
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, text: str) -> PadType:
        try:
            return cls(text)
        except ValueError:
            return cls.UNKNOWN


class PrimitiveKind(Enum):
    POLYGON = "polygon"
    CIRCLE = "circle"
    LINE = "line"
    ARC = "arc"  # points = (start, mid, end)
    RECT = "rect"
    CURVE = "curve"  # Bezier: control points only (approximated downstream)


@dataclass(frozen=True, slots=True)
class PadPrimitive:
    """One graphic primitive of a custom-shaped pad, in the pad's *local* frame
    (relative to the pad position, before pad rotation).

    * POLYGON / RECT / ARC / CURVE: ``points`` is the outline or polyline
    * CIRCLE: ``points = (center, point_on_circumference)``
    * LINE: ``points = (start, end)``

    ``width`` is the stroke width; ``filled`` tells whether the interior is copper.
    """

    kind: PrimitiveKind
    points: tuple[Point, ...]
    width: Nm = 0
    filled: bool = True


@dataclass(frozen=True, slots=True)
class Pad:
    """A pad with **absolute** board coordinates.

    ``rotation_deg`` is the pad's absolute orientation (KiCad stores it that way).
    ``size`` is ``(width, height)`` in the pad's own (unrotated) frame.
    """

    id: str
    number: str
    footprint_ref: str
    position: Point
    size: tuple[Nm, Nm]
    shape: PadShape
    pad_type: PadType
    layers: tuple[str, ...]  # expanded concrete layer names
    net_name: str | None  # None => pad has no net assignment
    rotation_deg: float = 0.0
    drill: Nm | None = None  # round drill diameter (or slot width) when present
    roundrect_ratio: float | None = None
    #: Copper-shape offset from the drill centre, in the pad frame (KiCad "shape offset").
    offset: Point = ORIGIN
    #: ``(width, height)`` of the hole; unequal for oval slots. ``None`` if no drill.
    drill_size: tuple[Nm, Nm] | None = None
    #: Pad-level clearance override (``None`` = inherit footprint/net class).
    local_clearance: Nm | None = None
    chamfer_ratio: float | None = None
    chamfer_corners: tuple[str, ...] = ()  # e.g. ("top_left", "bottom_right")
    #: Trapezoid ``rect_delta`` (dx, dy) for TRAPEZOID pads.
    trapezoid_delta: tuple[Nm, Nm] | None = None
    #: Custom pads: anchor shape (CIRCLE/RECT) plus primitives in the pad frame.
    custom_anchor: PadShape | None = None
    primitives: tuple[PadPrimitive, ...] = ()
    #: KiCad may remove unconnected inner-layer copper of THT pads; kept conservatively.
    remove_unused_layers: bool = False

    @property
    def copper_layers(self) -> tuple[str, ...]:
        return tuple(layer for layer in self.layers if layer.endswith(".Cu"))

    @property
    def is_through_hole(self) -> bool:
        return self.pad_type in (PadType.THROUGH_HOLE, PadType.NPTH)

    @property
    def is_plated(self) -> bool:
        return self.pad_type is not PadType.NPTH

    @property
    def bounds(self) -> BoundingBox:
        """Conservative axis-aligned bounds of the rotated pad rectangle (including a
        shape offset and custom primitives)."""
        hw, hh = self.size[0] // 2, self.size[1] // 2
        local = [
            Point(self.offset.x + dx, self.offset.y + dy)
            for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))
        ]
        for prim in self.primitives:
            if prim.kind is PrimitiveKind.CIRCLE and len(prim.points) == 2:
                c = prim.points[0]
                r = round(c.distance_to(prim.points[1])) + prim.width // 2
                local += [Point(c.x - r, c.y - r), Point(c.x + r, c.y + r)]
            else:
                w = prim.width // 2
                for p in prim.points:
                    local += [Point(p.x - w, p.y - w), Point(p.x + w, p.y + w)]
        corners = [
            rotate_point(
                Point(self.position.x + p.x, self.position.y + p.y),
                self.rotation_deg,
                self.position,
            )
            for p in local
        ]
        box = BoundingBox.from_points(corners)
        assert box is not None  # four corners always produce a box
        return box
