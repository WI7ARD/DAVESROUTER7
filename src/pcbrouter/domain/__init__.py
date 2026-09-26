"""Internal PCB domain model.

Everything outside :mod:`pcbrouter.kicad` works exclusively with these types.
Third-party or KiCad-specific objects must never leak past the KiCad adapter.
"""

from __future__ import annotations

from pcbrouter.domain.board import (
    Board,
    BoardIndex,
    BoardMetadata,
    BoardOutline,
    BoardStatistics,
    OutlineSegment,
    OutlineShape,
)
from pcbrouter.domain.component import Component
from pcbrouter.domain.footprint import BoardSide, Footprint
from pcbrouter.domain.geometry import BoundingBox, Point
from pcbrouter.domain.layer import CopperLayerType, Layer, LayerKind
from pcbrouter.domain.net import Net, NetStatistics
from pcbrouter.domain.pad import Pad, PadShape, PadType
from pcbrouter.domain.rules import DesignRules, NetClassDef, NetClassPattern
from pcbrouter.domain.track import Track
from pcbrouter.domain.units import Nm, internal_to_mm, mm_to_internal
from pcbrouter.domain.via import Via, ViaType
from pcbrouter.domain.zone import FilledPolygon, KeepoutRules, Zone, ZoneFillState

__all__ = [
    "Board",
    "BoardIndex",
    "BoardMetadata",
    "BoardOutline",
    "BoardSide",
    "BoardStatistics",
    "BoundingBox",
    "Component",
    "CopperLayerType",
    "DesignRules",
    "FilledPolygon",
    "Footprint",
    "KeepoutRules",
    "Layer",
    "LayerKind",
    "Net",
    "NetClassDef",
    "NetClassPattern",
    "NetStatistics",
    "Nm",
    "OutlineSegment",
    "OutlineShape",
    "Pad",
    "PadShape",
    "PadType",
    "Point",
    "Track",
    "Via",
    "ViaType",
    "Zone",
    "ZoneFillState",
    "internal_to_mm",
    "mm_to_internal",
]
