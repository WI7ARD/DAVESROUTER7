"""Copper zones and rule areas (keepouts).

A KiCad zone is either a copper pour (net + outline + optionally the *filled*
polygons KiCad computed when it last filled the zone) or a rule area / keepout
(an outline plus per-item permissions). Both are kept as plain domain data here;
:mod:`pcbrouter.geometry` turns them into shapes.

Fill semantics (documented, see docs/geometry_engine.md): the filled polygons are
exactly what KiCad stored at its last fill. They can be stale if the board was
edited without refilling. When a zone has no stored fill, only the outline is known
and the copper extent is *unknown* — it is never assumed to equal the outline.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pcbrouter.domain.geometry import BoundingBox, Point
from pcbrouter.domain.units import Nm


@dataclass(frozen=True, slots=True)
class KeepoutRules:
    """Per-item permissions of a rule area. ``True`` means *not allowed*.

    Each permission is independent (a keepout may forbid tracks but allow vias).
    """

    tracks: bool = False
    vias: bool = False
    pads: bool = False
    copper_pour: bool = False
    footprints: bool = False

    @property
    def forbids_anything(self) -> bool:
        return self.tracks or self.vias or self.pads or self.copper_pour or self.footprints

    def describe(self) -> str:
        names = [
            name
            for name, flag in (
                ("tracks", self.tracks),
                ("vias", self.vias),
                ("pads", self.pads),
                ("copper pour", self.copper_pour),
                ("footprints", self.footprints),
            )
            if flag
        ]
        return "no " + ", no ".join(names) if names else "nothing forbidden"


class ZoneFillState(Enum):
    FILLED = "filled"  # filled polygons stored in the file
    UNFILLED = "unfilled"  # a copper zone with no stored fill: extent unknown
    NOT_APPLICABLE = "n/a"  # keepouts / rule areas


@dataclass(frozen=True, slots=True)
class FilledPolygon:
    """One filled copper polygon of a zone on one layer (KiCad fractures holes into
    the outline, so each polygon is a single simple boundary)."""

    layer: str
    points: tuple[Point, ...]
    island: bool = False


@dataclass(frozen=True, slots=True)
class Zone:
    id: str
    layers: tuple[str, ...]  # expanded layer names
    outline: tuple[Point, ...]  # main outline (closed implicitly)
    net_name: str | None = None
    name: str | None = None
    keepout: KeepoutRules | None = None  # set => rule area / keepout
    filled: tuple[FilledPolygon, ...] = ()
    fill_state: ZoneFillState = ZoneFillState.NOT_APPLICABLE
    priority: int = 0
    local_clearance: Nm | None = None  # (connect_pads (clearance ...))
    footprint_ref: str | None = None  # set for zones defined inside a footprint
    locked: bool = False
    #: Extra outline loops (KiCad allows several polygons per zone).
    extra_outlines: tuple[tuple[Point, ...], ...] = ()

    @property
    def is_keepout(self) -> bool:
        return self.keepout is not None

    @property
    def copper_layers(self) -> tuple[str, ...]:
        return tuple(layer for layer in self.layers if layer.endswith(".Cu"))

    @property
    def bounds(self) -> BoundingBox | None:
        pts = [*self.outline, *(p for loop in self.extra_outlines for p in loop)]
        return BoundingBox.from_points(pts)
