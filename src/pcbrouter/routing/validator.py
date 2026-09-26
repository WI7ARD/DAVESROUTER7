"""The route-legality API Stage 4 will call.

    validator.validate_segment(net, layer, start, end, width)
    validator.validate_via(net, position, start_layer, end_layer, diameter, drill)
    validator.validate_route(proposal)

Everything is deterministic and independent of any AI. Callers never touch KiCad
objects: inputs are nets, layer names and integer-nm geometry.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from pcbrouter.domain.geometry import Point
from pcbrouter.domain.units import Nm
from pcbrouter.geometry.board import BoardGeometry
from pcbrouter.geometry.clearance import GEOMETRY_TOLERANCE_NM, touches
from pcbrouter.geometry.shapes import circle
from pcbrouter.routing.collision import (
    CollisionEngine,
    CollisionResult,
    ValidationStatus,
    ViolationType,
)
from pcbrouter.routing.proposal import RouteProposal
from pcbrouter.rules.resolver import RuleResolver

_SEVERITY = {
    ValidationStatus.VALID: 0,
    ValidationStatus.VALID_WITH_WARNINGS: 1,
    ValidationStatus.RULE_UNKNOWN: 2,
    ValidationStatus.INVALID: 3,
}


@dataclass
class ElementResult:
    label: str  # "Segment 3", "Via 1"
    result: CollisionResult


@dataclass
class RouteValidationResult:
    proposal_id: str
    net: str
    status: ValidationStatus = ValidationStatus.VALID
    elements: list[ElementResult] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    checks: int = 0
    elapsed_s: float = 0.0

    @property
    def legal(self) -> bool:
        return self.status in (ValidationStatus.VALID, ValidationStatus.VALID_WITH_WARNINGS)

    def summary(self) -> str:
        head = f"{self.status.value}: route for {self.net} ({len(self.elements)} element(s))"
        return "\n".join([head, *self.messages])


def _worst(a: ValidationStatus, b: ValidationStatus) -> ValidationStatus:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def _close(a: Point, b: Point) -> bool:
    return abs(a.x - b.x) <= GEOMETRY_TOLERANCE_NM and abs(a.y - b.y) <= GEOMETRY_TOLERANCE_NM


class RouteValidator:
    def __init__(self, geometry: BoardGeometry, resolver: RuleResolver) -> None:
        self.geometry = geometry
        self.resolver = resolver
        self.engine = CollisionEngine(geometry, resolver)

    # ------------------------------------------------------------ single elements
    def validate_segment(
        self, net: str | None, layer: str, start: Point, end: Point, width: Nm
    ) -> CollisionResult:
        return self.engine.check_segment(net, layer, start, end, width)

    def validate_via(
        self,
        net: str | None,
        position: Point,
        start_layer: str,
        end_layer: str,
        diameter: Nm,
        drill: Nm,
    ) -> CollisionResult:
        return self.engine.check_via(net, position, start_layer, end_layer, diameter, drill)

    # ------------------------------------------------------------ whole proposals
    def validate_route(self, proposal: RouteProposal) -> RouteValidationResult:
        t0 = time.perf_counter()
        out = RouteValidationResult(proposal.proposal_id, proposal.net)
        if not proposal.segments and not proposal.vias:
            out.status = ValidationStatus.INVALID
            out.messages.append("INVALID: the proposal contains no geometry")
            return out
        for i, seg in enumerate(proposal.segments, start=1):
            res = self.validate_segment(proposal.net, seg.layer, seg.start, seg.end, seg.width)
            self._add(out, f"Segment {i}", res)
        for i, via in enumerate(proposal.vias, start=1):
            res = self.validate_via(
                proposal.net, via.position, via.start_layer, via.end_layer, via.diameter, via.drill
            )
            self._add(out, f"Via {i}", res)
        self._continuity(out, proposal)
        self._via_count(out, proposal)
        out.elapsed_s = time.perf_counter() - t0
        return out

    validate_path = validate_route

    def _add(self, out: RouteValidationResult, label: str, res: CollisionResult) -> None:
        out.elements.append(ElementResult(label, res))
        out.checks += res.checks
        out.status = _worst(out.status, res.status)
        for c in res.collisions:
            out.messages.append(f"INVALID: {label} {c.message}")
        for u in res.unknowns:
            out.messages.append(f"RULE UNKNOWN: {label}: {u.message}")
        for w in res.warnings:
            out.messages.append(f"warning: {label}: {w}")

    def _continuity(self, out: RouteValidationResult, proposal: RouteProposal) -> None:
        """Every segment end must meet another segment end on the same layer, a via
        spanning that layer at that point, or existing same-net copper."""
        ends: list[tuple[Point, str, str]] = []
        for i, seg in enumerate(proposal.segments, start=1):
            ends += [
                (seg.start, seg.layer, f"Segment {i} start"),
                (seg.end, seg.layer, f"Segment {i} end"),
            ]
        geo = self.geometry
        dangling: list[str] = []
        for p, layer, label in ends:
            if sum(1 for q, lay, _ in ends if lay == layer and _close(p, q)) > 1:
                continue
            if any(
                _close(v.position, p) and layer in geo.layer_span(v.start_layer, v.end_layer)
                for v in proposal.vias
                if geo.is_copper_layer(v.start_layer) and geo.is_copper_layer(v.end_layer)
            ):
                continue
            if self._touches_existing(proposal.net, layer, p):
                continue
            # A layer change without a via is a hard error; a loose end is a warning.
            if any(_close(p, q) and lay != layer for q, lay, _ in ends):
                out.status = ValidationStatus.INVALID
                out.messages.append(f"INVALID: {label} changes layer without a via")
            else:
                dangling.append(label)
        if dangling:
            out.status = _worst(out.status, ValidationStatus.VALID_WITH_WARNINGS)
            out.messages.append("warning: unconnected end(s): " + ", ".join(dangling))

    def _touches_existing(self, net: str, layer: str, p: Point) -> bool:
        probe = circle(p, 0)
        return any(
            item.net == net and any(touches(probe, s) for s in item.shapes)
            for item in self.geometry.copper_near(layer, probe.bounds)
        )

    def _via_count(self, out: RouteValidationResult, proposal: RouteProposal) -> None:
        limit = self.resolver.resolve_max_vias(proposal.net)
        if limit.value is not None and len(proposal.vias) > limit.value:
            out.status = ValidationStatus.INVALID
            out.messages.append(
                f"INVALID: {len(proposal.vias)} vias exceed the limit of {limit.value} "
                f"({limit.source.describe()})"
            )


__all__ = [
    "ElementResult",
    "RouteValidationResult",
    "RouteValidator",
    "ValidationStatus",
    "ViolationType",
]
