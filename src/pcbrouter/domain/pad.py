"""Footprint pads."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pcbrouter.domain.geometry import BoundingBox, Point, rotate_point
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

    @property
    def copper_layers(self) -> tuple[str, ...]:
        return tuple(layer for layer in self.layers if layer.endswith(".Cu"))

    @property
    def is_through_hole(self) -> bool:
        return self.pad_type in (PadType.THROUGH_HOLE, PadType.NPTH)

    @property
    def bounds(self) -> BoundingBox:
        """Conservative axis-aligned bounds of the rotated pad rectangle."""
        hw, hh = self.size[0] // 2, self.size[1] // 2
        corners = [
            rotate_point(
                Point(self.position.x + dx, self.position.y + dy), self.rotation_deg, self.position
            )
            for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))
        ]
        box = BoundingBox.from_points(corners)
        assert box is not None  # four corners always produce a box
        return box
