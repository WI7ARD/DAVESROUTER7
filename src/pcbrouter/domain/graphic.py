"""Graphics and text on copper layers.

KiCad treats a ``gr_line`` / ``gr_poly`` / ``gr_text`` ... (or a footprint's
``fp_*`` / visible text) on a copper layer as copper: other nets must keep their
clearance from it (DRC "Clearance violation ... PCB Text 'X'"). They carry no
net for routing purposes here, so they are obstacles only, never connections.
"""

from __future__ import annotations

from dataclasses import dataclass

from pcbrouter.domain.geometry import Point
from pcbrouter.domain.units import Nm


@dataclass(frozen=True, slots=True)
class CopperGraphic:
    id: str
    layer: str
    kind: str  # "line" | "arc" | "circle" | "rect" | "poly" | "curve" | "text"
    #: Stroke path (open), or the outline of a filled shape (implicitly closed).
    #: Arcs keep ``(start, mid, end)``; circles ``(center, point on circle)``.
    points: tuple[Point, ...]
    width: Nm  # stroke width; 0 for a filled outline without a stroke
    filled: bool
    label: str
    footprint_ref: str | None = None
    #: The shape over-covers the real copper (text bounding box, Bezier hull).
    conservative: bool = False
