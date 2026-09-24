"""Inflated obstacles for a candidate track (debug overlays and Stage 4 input).

Each foreign copper object is grown by ``track half-width + resolved clearance``;
the track *centreline* must stay outside every inflated obstacle. The inflation is
the exact Minkowski sum of the core+radius representation (radius arithmetic).
"""

from __future__ import annotations

from dataclasses import dataclass

from pcbrouter.domain.geometry import BoundingBox
from pcbrouter.domain.units import Nm
from pcbrouter.geometry.board import BoardGeometry, ItemKind
from pcbrouter.geometry.shapes import Shape
from pcbrouter.routing.collision import item_type_of
from pcbrouter.rules.model import ItemType
from pcbrouter.rules.resolver import RuleResolver


@dataclass(frozen=True, slots=True)
class InflatedObstacle:
    uid: str
    label: str
    raw: tuple[Shape, ...]
    inflated: tuple[Shape, ...]
    clearance: Nm | None  # None = unknown rule (inflated by half-width only)
    clearance_source: str


def inflated_obstacles(
    geo: BoardGeometry,
    resolver: RuleResolver,
    layer: str,
    net: str | None,
    width: Nm,
    area: BoundingBox | None = None,
) -> list[InflatedObstacle]:
    box = area or geo.board.bounds
    if box is None:
        return []
    half = width // 2
    out: list[InflatedObstacle] = []
    for item in geo.copper_near(layer, box):
        if (net is not None and item.net == net) or item.kind is ItemKind.ZONE_FILL:
            continue
        req = resolver.resolve_clearance(
            net, item.net, ItemType.TRACK, item_type_of(item.kind), layer,
            None, item.local_clearance, None, item.label,
        )  # fmt: skip
        grow = half + (req.value or 0)
        out.append(
            InflatedObstacle(
                item.uid, item.label, item.shapes,
                tuple(s.inflated(grow) for s in item.shapes), req.value, req.source.describe(),
            )
        )  # fmt: skip
    return out
