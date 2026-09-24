"""Pin escape analysis: which directions can a track leave a pad in?

For each copper layer of the pad, eight probe segments (0°, 45°, ... 315°) start at
the pad centre and extend past the pad edge; each is checked with the exact
collision engine at the net's preferred width. The result also reports the nearest
foreign copper, nearby same-net pads and the distance to the board edge.

This only *describes* the neighbourhood (for Stage 4 start directions and for AI
context). It generates no route.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from pcbrouter.domain.geometry import Point
from pcbrouter.domain.units import Nm
from pcbrouter.geometry.board import BoardGeometry, CopperItem, ItemKind
from pcbrouter.geometry.clearance import gap_between
from pcbrouter.routing.collision import CollisionEngine, ValidationStatus

DIRECTIONS_DEG = (0, 45, 90, 135, 180, 225, 270, 315)
PROBE_BEYOND_PAD_NM: Nm = 1_000_000
NEARBY_RADIUS_NM: Nm = 5_000_000


@dataclass(frozen=True, slots=True)
class DirectionResult:
    angle_deg: int
    free: bool
    status: ValidationStatus
    blocker: str | None = None


@dataclass
class PinEscape:
    pad_uid: str
    label: str
    net: str | None
    probe_width: Nm | None
    directions: dict[str, list[DirectionResult]] = field(default_factory=dict)
    nearest_foreign: tuple[float, str] | None = None  # (gap nm, label)
    same_net_nearby: list[str] = field(default_factory=list)
    edge_distance: float | None = None
    notes: list[str] = field(default_factory=list)

    def free_directions(self, layer: str) -> list[int]:
        return [d.angle_deg for d in self.directions.get(layer, []) if d.free]


def analyse_pin_escape(geo: BoardGeometry, engine: CollisionEngine, pad: CopperItem) -> PinEscape:
    if pad.kind is not ItemKind.PAD:
        raise ValueError(f"{pad.uid} is not a pad")
    width_rule = engine.resolver.resolve_trace_width(pad.net)
    result = PinEscape(pad.uid, pad.label, pad.net, width_rule.value)
    center = pad.bounds.center
    reach = max(pad.bounds.width, pad.bounds.height) // 2 + PROBE_BEYOND_PAD_NM
    if width_rule.value is None:
        result.notes.append("no track width rule for this net: escape directions not evaluated")
    else:
        for layer in sorted(pad.layers):
            results: list[DirectionResult] = []
            for angle in DIRECTIONS_DEG:
                rad = math.radians(angle)
                end = Point(
                    center.x + round(reach * math.cos(rad)), center.y - round(reach * math.sin(rad))
                )
                check = engine.check_segment(pad.net, layer, center, end, width_rule.value)
                blocker = None
                if check.collisions:
                    first = check.collisions[0]
                    blocker = first.object_label or first.violation.label
                results.append(DirectionResult(angle, check.legal, check.status, blocker))
            result.directions[layer] = results
    box = pad.bounds.expanded(NEARBY_RADIUS_NM)
    best: tuple[float, str] | None = None
    same: list[str] = []
    for layer in sorted(pad.layers):
        for item in geo.copper_near(layer, box):
            if item.uid == pad.uid:
                continue
            gap = min(gap_between(a, b) for a in pad.shapes for b in item.shapes)
            if item.net == pad.net and pad.net is not None:
                if item.kind is ItemKind.PAD and item.label not in same:
                    same.append(item.label)
            elif best is None or gap < best[0]:
                best = (gap, item.label)
    result.nearest_foreign = best
    result.same_net_nearby = same
    edges = geo.edges_near(box)
    if edges:
        result.edge_distance = min(gap_between(s, e.shape) for s in pad.shapes for e in edges)
    return result
