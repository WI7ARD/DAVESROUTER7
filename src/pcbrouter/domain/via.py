"""Vias."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pcbrouter.domain.geometry import BoundingBox, Point
from pcbrouter.domain.units import Nm


class ViaType(Enum):
    THROUGH = "through"
    BLIND_BURIED = "blind_buried"
    MICRO = "micro"


@dataclass(frozen=True, slots=True)
class Via:
    """A via. ``start_layer``/``end_layer`` are ``None`` when the file omitted them."""

    id: str
    position: Point
    diameter: Nm
    drill: Nm | None
    net_name: str | None
    start_layer: str | None
    end_layer: str | None
    via_type: ViaType = ViaType.THROUGH
    locked: bool = False

    @property
    def bounds(self) -> BoundingBox:
        return BoundingBox.around(self.position, self.diameter // 2)

    def spans_layer(self, layer: str, copper_order: list[str]) -> bool:
        """Whether the via has copper on ``layer`` given the board's copper stack order."""
        if self.start_layer is None or self.end_layer is None:
            return layer in copper_order
        try:
            a = copper_order.index(self.start_layer)
            b = copper_order.index(self.end_layer)
            idx = copper_order.index(layer)
        except ValueError:
            return False
        lo, hi = min(a, b), max(a, b)
        return lo <= idx <= hi
