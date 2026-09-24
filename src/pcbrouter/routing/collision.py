"""Deterministic collision / clearance engine for candidate copper.

Answers "may this segment / via exist?" against the board's geometry and rules:

* **net-aware**: same-net copper may touch (it is recorded as a contact); foreign
  copper (and no-net copper) must keep the resolved clearance;
* **layer-aware**: only objects on the queried copper layer are considered
  (through-hole pads and vias are indexed on every layer they occupy);
* **spatially indexed**: every query touches only nearby objects;
* **rule-traceable**: every violation names the rule (and file) it came from;
* **never guesses**: unknown rules yield ``RULE_UNKNOWN`` findings, not a pass.

Zone fills of *other* nets are reported as warnings by default (KiCad refills zones
around new copper); set ``zone_fills_are_obstacles`` to treat them as hard copper.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from pcbrouter.domain.geometry import Point
from pcbrouter.domain.units import Nm, format_mm
from pcbrouter.geometry.board import BoardGeometry, CopperItem, ItemKind, RegionStatus
from pcbrouter.geometry.clearance import (
    GEOMETRY_TOLERANCE_NM,
    approximate_contact_point,
    check_clearance,
    overlaps,
    touches,
)
from pcbrouter.geometry.shapes import Shape, ShapeAccuracy, capsule, circle
from pcbrouter.rules.model import ItemType, ResolvedValue
from pcbrouter.rules.resolver import RuleResolver


class ViolationType(Enum):
    SHORT = "short"  # overlaps foreign copper
    CLEARANCE = "clearance"
    HOLE_CLEARANCE = "hole_clearance"
    HOLE_OVERLAP = "hole_overlap"  # copper through a drill
    HOLE_TO_HOLE = "hole_to_hole"
    EDGE_CROSSING = "edge_crossing"
    EDGE_CLEARANCE = "edge_clearance"
    OUTSIDE_BOARD = "outside_board"
    IN_CUTOUT = "in_cutout"
    KEEPOUT = "keepout"
    DISALLOWED = "disallowed"
    MIN_WIDTH = "min_width"
    MAX_WIDTH = "max_width"
    MIN_VIA_DIAMETER = "min_via_diameter"
    MIN_DRILL = "min_drill"
    MIN_ANNULAR = "min_annular_ring"
    INVALID_GEOMETRY = "invalid_geometry"
    INVALID_LAYER = "invalid_layer"
    LAYER_NOT_ALLOWED = "layer_not_allowed"
    VIA_SPAN = "via_span"
    NET_UNKNOWN = "net_unknown"
    MAX_VIAS = "max_vias"
    DISCONTINUOUS = "discontinuous"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ")


class ValidationStatus(Enum):
    VALID = "VALID"
    VALID_WITH_WARNINGS = "VALID_WITH_WARNINGS"
    INVALID = "INVALID"
    RULE_UNKNOWN = "RULE_UNKNOWN"


@dataclass(frozen=True, slots=True)
class Collision:
    violation: ViolationType
    message: str
    object_type: str | None = None  # "pad", "track", "via", "zone fill", "hole", ...
    object_id: str | None = None
    object_label: str | None = None
    net: str | None = None
    layer: str | None = None
    distance: float | None = None  # observed nm (negative = overlap)
    required: Nm | None = None
    rule_source: str | None = None
    location: Point | None = None
    accuracy: ShapeAccuracy = ShapeAccuracy.EXACT


@dataclass(frozen=True, slots=True)
class Finding:
    """A non-violation outcome: an unknown rule or a warning."""

    kind: str  # "rule_unknown" | "warning"
    message: str
    rule_source: str | None = None


@dataclass
class CollisionResult:
    status: ValidationStatus = ValidationStatus.VALID
    collisions: list[Collision] = field(default_factory=list)
    unknowns: list[Finding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    same_net_contacts: list[str] = field(default_factory=list)
    minimum_observed_clearance: float | None = None
    required_clearance: Nm | None = None
    checks: int = 0
    elapsed_s: float = 0.0

    @property
    def legal(self) -> bool:
        return self.status in (ValidationStatus.VALID, ValidationStatus.VALID_WITH_WARNINGS)

    def messages(self) -> list[str]:
        return (
            [c.message for c in self.collisions]
            + [f"RULE UNKNOWN: {u.message}" for u in self.unknowns]
            + [f"warning: {w}" for w in self.warnings]
        )


def item_type_of(kind: ItemKind) -> ItemType:
    return {
        ItemKind.PAD: ItemType.PAD,
        ItemKind.TRACK: ItemType.TRACK,
        ItemKind.VIA: ItemType.VIA,
        ItemKind.ZONE_FILL: ItemType.ZONE,
    }[kind]


def _mm(nm: float | None) -> str:
    return "unknown" if nm is None else format_mm(round(nm))


class CollisionEngine:
    """Stateless queries over one :class:`BoardGeometry` + :class:`RuleResolver`."""

    def __init__(
        self,
        geometry: BoardGeometry,
        resolver: RuleResolver,
        *,
        zone_fills_are_obstacles: bool = False,
    ) -> None:
        self.geometry = geometry
        self.resolver = resolver
        self.zone_fills_are_obstacles = zone_fills_are_obstacles
        self.board_nets = frozenset(n.name for n in geometry.board.nets if n.name)
        self.search_margin = self._max_clearance()

    def _max_clearance(self) -> Nm:
        """Upper bound of any resolvable clearance — the neighbour search radius."""
        rs = self.resolver.ruleset
        values = [rs.board.min_clearance or 0, rs.board.min_hole_clearance or 0,
                  rs.board.min_copper_edge_clearance or 0,
                  rs.board.min_hole_to_hole or 0]  # fmt: skip
        for nc in rs.classes.classes.values():
            values.append(nc.clearance or 0)
        for rule in rs.custom_rules:
            for c in rule.constraints:
                if c.kind in ("clearance", "hole_clearance", "edge_clearance", "hole_to_hole"):
                    values.append(c.min or 0)
        for u in rs.unsupported:
            bound = u.min_for("clearance", "physical_clearance", "hole_clearance", "edge_clearance")
            if bound is not None and bound < 10**9:
                values.append(bound)
        ov = self.resolver.overrides
        values += [ov.board.clearance or 0, ov.board.edge_clearance or 0]
        values += [n.clearance or 0 for n in ov.nets.values()]
        for item in self.geometry.copper.values():
            if item.local_clearance:
                values.append(item.local_clearance)
        return max(values) + GEOMETRY_TOLERANCE_NM

    # ------------------------------------------------------------ public API
    def check_segment(
        self, net: str | None, layer: str, start: Point, end: Point, width: Nm
    ) -> CollisionResult:
        t0 = time.perf_counter()
        result = CollisionResult()
        if width <= 0:
            self._violation(
                result,
                ViolationType.INVALID_GEOMETRY,
                f"track width must be positive (got {width} nm)",
            )
            return self._finish(result, t0)
        if not self._layer_ok(result, net, layer):
            return self._finish(result, t0)
        self._net_ok(result, net)
        self._width_rules(result, net, layer, width)
        self._disallow(result, "track", net, layer)
        shape = capsule(start, end, width // 2)
        self._copper(result, net, layer, shape, ItemType.TRACK)
        self._holes(result, net, [layer], shape, ItemType.TRACK)
        self._keepouts(result, [layer], shape, tracks=True)
        self._board_edge(result, net, shape, ItemType.TRACK, start)
        return self._finish(result, t0)

    def check_via(
        self,
        net: str | None,
        position: Point,
        start_layer: str,
        end_layer: str,
        diameter: Nm,
        drill: Nm,
    ) -> CollisionResult:
        t0 = time.perf_counter()
        result = CollisionResult()
        geo = self.geometry
        for layer in (start_layer, end_layer):
            if not geo.is_copper_layer(layer):
                self._violation(
                    result,
                    ViolationType.INVALID_LAYER,
                    f"{layer} is not a copper layer of this board",
                )
        if result.collisions:
            return self._finish(result, t0)
        if start_layer == end_layer:
            self._violation(
                result, ViolationType.VIA_SPAN, "a via must connect two different copper layers"
            )
            return self._finish(result, t0)
        if diameter <= 0 or drill <= 0 or drill >= diameter:
            self._violation(
                result, ViolationType.INVALID_GEOMETRY,
                f"via drill {_mm(drill)} must be positive and smaller than its "
                f"diameter {_mm(diameter)}",
            )  # fmt: skip
            return self._finish(result, t0)
        span = geo.layer_span(start_layer, end_layer)
        if len(span) != len(geo.copper_layers):
            result.warnings.append(
                f"blind/buried via ({start_layer}–{end_layer}): fabrication capability is not "
                "verified by the engine"
            )
        layer_rules = self.resolver.resolve_allowed_layers(net)
        for layer in (start_layer, end_layer):
            if layer not in layer_rules.allowed:
                self._violation(
                    result, ViolationType.LAYER_NOT_ALLOWED,
                    f"{layer} is not allowed for net {net} ({layer_rules.source.describe()})",
                    layer=layer, rule_source=layer_rules.source.describe(),
                )  # fmt: skip
        self._net_ok(result, net)
        self._via_size_rules(result, net, diameter, drill)
        self._disallow(result, "via", net, None)
        shape = circle(position, diameter // 2)
        for layer in span:
            self._copper(result, net, layer, shape, ItemType.VIA)
        self._hole_to_hole(result, circle(position, drill // 2))
        self._keepouts(result, span, shape, vias=True)
        self._board_edge(result, net, shape, ItemType.VIA, position)
        return self._finish(result, t0)

    # ------------------------------------------------------------ checks
    def _violation(
        self, result: CollisionResult, vtype: ViolationType, message: str, **kw: object
    ) -> None:
        result.collisions.append(Collision(vtype, message, **kw))  # type: ignore[arg-type]

    def _unknown(self, result: CollisionResult, message: str, source: str | None = None) -> None:
        if not any(u.message == message for u in result.unknowns):
            result.unknowns.append(Finding("rule_unknown", message, source))

    def _layer_ok(self, result: CollisionResult, net: str | None, layer: str) -> bool:
        if not self.geometry.is_copper_layer(layer):
            self._violation(
                result,
                ViolationType.INVALID_LAYER,
                f"{layer} is not a copper layer of this board",
                layer=layer,
            )
            return False
        rules = self.resolver.resolve_allowed_layers(net)
        if layer not in rules.allowed:
            self._violation(
                result, ViolationType.LAYER_NOT_ALLOWED,
                f"{layer} is not allowed for net {net} ({rules.source.describe()})",
                layer=layer, rule_source=rules.source.describe(),
            )  # fmt: skip
            return False
        return True

    def _net_ok(self, result: CollisionResult, net: str | None) -> None:
        if net is not None and net not in self.board_nets:
            self._violation(
                result,
                ViolationType.NET_UNKNOWN,
                f"net {net!r} does not exist on this board",
                net=net,
            )

    def _minimum(
        self,
        result: CollisionResult,
        value: ResolvedValue,
        actual: Nm,
        vtype: ViolationType,
        what: str,
    ) -> None:
        if value.value is None:
            self._unknown(
                result, f"{what}: no rule states a minimum ({actual / 1e6:g} mm cannot be verified)"
            )
            return
        if actual < value.value:
            self._violation(
                result, vtype,
                f"{what} {format_mm(actual)} is below the minimum {format_mm(value.value)} "
                f"({value.source.describe()})",
                distance=float(actual), required=value.value, rule_source=value.source.describe(),
            )  # fmt: skip
        elif value.possibly_stricter is not None and actual < value.possibly_stricter:
            self._unknown(
                result,
                f"{what} {format_mm(actual)}: unsupported rule "
                f"{', '.join(value.possibly_stricter_rules)} may require more",
            )

    def _width_rules(self, result: CollisionResult, net: str | None, layer: str, width: Nm) -> None:
        rules = self.resolver.width_rules(net, layer)
        self._minimum(result, rules.minimum, width, ViolationType.MIN_WIDTH, "track width")
        mx = rules.maximum
        if mx.value is not None and width > mx.value:
            self._violation(
                result, ViolationType.MAX_WIDTH,
                f"track width {format_mm(width)} exceeds the maximum "
                f"{format_mm(mx.value)} ({mx.source.describe()})",
                distance=float(width), required=mx.value, rule_source=mx.source.describe(),
            )  # fmt: skip
        elif mx.possibly_stricter is not None and width > mx.possibly_stricter:
            self._unknown(
                result,
                f"track width {format_mm(width)}: unsupported rule "
                f"{', '.join(mx.possibly_stricter_rules)} may cap it",
            )

    def _via_size_rules(
        self, result: CollisionResult, net: str | None, diameter: Nm, drill: Nm
    ) -> None:
        rules = self.resolver.resolve_via_rules(net)
        self._minimum(
            result, rules.min_diameter, diameter, ViolationType.MIN_VIA_DIAMETER, "via diameter"
        )
        self._minimum(result, rules.min_drill, drill, ViolationType.MIN_DRILL, "via drill")
        annular = (diameter - drill) // 2
        if (
            rules.min_annular_width.value is not None
            or rules.min_annular_width.possibly_stricter is not None
        ):
            self._minimum(
                result,
                rules.min_annular_width,
                annular,
                ViolationType.MIN_ANNULAR,
                "via annular ring",
            )

    def _disallow(
        self, result: CollisionResult, item: str, net: str | None, layer: str | None
    ) -> None:
        d = self.resolver.disallowed(item, net, layer)
        if d.forbidden:
            names = ", ".join(d.rules)
            self._violation(
                result, ViolationType.DISALLOWED,
                f"{item}s are not allowed for net {net} (custom rule {names})",
                rule_source=f"Custom rule {names}",
            )  # fmt: skip
        for rule in d.unknown_rules:
            self._unknown(result, f"unsupported rule '{rule}' may disallow {item}s here")

    def _copper(
        self,
        result: CollisionResult,
        net: str | None,
        layer: str,
        shape: Shape,
        item_type: ItemType,
    ) -> None:
        box = shape.bounds.expanded(self.search_margin)
        for item in self.geometry.copper_near(layer, box):
            result.checks += 1
            if net is not None and item.net == net:
                if any(touches(shape, s) for s in item.shapes):
                    result.same_net_contacts.append(item.uid)
                continue
            self._pair(result, net, layer, shape, item_type, item)

    def _pair(
        self,
        result: CollisionResult,
        net: str | None,
        layer: str,
        shape: Shape,
        item_type: ItemType,
        item: CopperItem,
    ) -> None:
        is_zone = item.kind is ItemKind.ZONE_FILL
        req = self.resolver.resolve_clearance(
            net, item.net, item_type, item_type_of(item.kind), layer,
            None, item.local_clearance, None, item.label,
        )  # fmt: skip
        if is_zone and not self.zone_fills_are_obstacles:
            if any(overlaps(shape, s) for s in item.shapes) or (
                req.value is not None
                and any(not check_clearance(shape, s, req.value).ok for s in item.shapes)
            ):
                result.warnings.append(
                    f"crosses the {item.label} (net {item.net}); "
                    "the zone must be refilled around new copper"
                )
            return
        if req.value is None:
            for s in item.shapes:
                if overlaps(shape, s):
                    self._record(
                        result,
                        ViolationType.SHORT,
                        item,
                        layer,
                        shape,
                        s,
                        None,
                        "no clearance rule (overlap is always illegal)",
                    )
                    return
            self._unknown(result, f"no clearance rule for {net} vs {item.net or 'no-net'} copper")
            return
        worst = None
        worst_shape = None
        pair_min: float | None = None
        for s in item.shapes:
            gap = check_clearance(shape, s, req.value)
            if gap.gap is not None:
                pair_min = gap.gap if pair_min is None else min(pair_min, gap.gap)
                if (
                    result.minimum_observed_clearance is None
                    or gap.gap < result.minimum_observed_clearance
                ):
                    result.minimum_observed_clearance = gap.gap
                    result.required_clearance = req.value
            if not gap.ok and (worst is None or (gap.gap or 0) < (worst.gap or 0)):
                worst, worst_shape = gap, s
        if worst is not None and worst_shape is not None:
            vtype = ViolationType.SHORT if worst.overlap else ViolationType.CLEARANCE
            self._record(result, vtype, item, layer, shape, worst_shape, req, None, worst.gap)
        elif req.possibly_stricter is not None:
            if pair_min is None or pair_min < req.possibly_stricter:
                self._unknown(
                    result,
                    f"clearance to {item.label}: unsupported rule "
                    f"{', '.join(req.possibly_stricter_rules)} may require more",
                )

    def _record(
        self,
        result: CollisionResult,
        vtype: ViolationType,
        item: CopperItem,
        layer: str,
        shape: Shape,
        other: Shape,
        req: ResolvedValue | None,
        note: str | None,
        gap: float | None = None,
    ) -> None:
        required = req.value if req is not None else None
        source = req.source.describe() if req is not None else note
        what = "overlaps" if vtype is ViolationType.SHORT else "violates clearance to"
        approx = "" if item.accuracy is ShapeAccuracy.EXACT else f" [{item.accuracy.label}]"
        where = "" if layer in item.label else f" on {layer}"
        msg = f"{what} {item.label} (net {item.net or 'none'}){where}"
        if gap is not None:
            msg += f". Observed {_mm(max(gap, 0.0) if vtype is not ViolationType.SHORT else gap)}"
        if required is not None:
            msg += f", required {format_mm(required)}"
        if source:
            msg += f". Rule: {source}"
        result.collisions.append(
            Collision(
                vtype, msg + approx, item.kind.label, item.uid, item.label, item.net, layer,
                gap, required, source, approximate_contact_point(shape, other), item.accuracy,
            )
        )  # fmt: skip

    def _holes(
        self,
        result: CollisionResult,
        net: str | None,
        layers: list[str],
        shape: Shape,
        item_type: ItemType,
    ) -> None:
        box = shape.bounds.expanded(self.search_margin)
        for hole in self.geometry.holes_near(box):
            result.checks += 1
            if hole.plated and hole.owner_uid is not None:
                owner = self.geometry.copper.get(hole.owner_uid)
                # A plated hole is inside its own copper on these layers: copper rules cover it.
                if owner is not None and (
                    owner.net == net or any(lay in owner.layers for lay in layers)
                ):
                    continue
            req = self.resolver.resolve_hole_clearance(net, item_type)
            if overlaps(shape, hole.shape):
                self._violation(
                    result, ViolationType.HOLE_OVERLAP,
                    f"copper passes through {hole.label}",
                    object_type="hole", object_id=hole.uid, object_label=hole.label,
                    location=approximate_contact_point(shape, hole.shape),
                )  # fmt: skip
                continue
            if req.value is None:
                self._unknown(result, "copper-to-hole clearance is not stated by any rule")
                continue
            gap = check_clearance(shape, hole.shape, req.value)
            if not gap.ok:
                self._violation(
                    result, ViolationType.HOLE_CLEARANCE,
                    f"copper {_mm(gap.gap)} from {hole.label}, required "
                    f"{format_mm(req.value)}. Rule: {req.source.describe()}",
                    object_type="hole", object_id=hole.uid, object_label=hole.label,
                    distance=gap.gap,
                    required=req.value, rule_source=req.source.describe(), location=gap.location,
                )  # fmt: skip

    def _hole_to_hole(self, result: CollisionResult, hole_shape: Shape) -> None:
        req = self.resolver.resolve_hole_to_hole()
        box = hole_shape.bounds.expanded(self.search_margin)
        for hole in self.geometry.holes_near(box):
            result.checks += 1
            if overlaps(hole_shape, hole.shape):
                self._violation(
                    result, ViolationType.HOLE_TO_HOLE, f"drill overlaps {hole.label}",
                    object_type="hole", object_id=hole.uid, object_label=hole.label,
                )  # fmt: skip
                continue
            if req.value is None:
                continue
            gap = check_clearance(hole_shape, hole.shape, req.value)
            if not gap.ok:
                self._violation(
                    result, ViolationType.HOLE_TO_HOLE,
                    f"drill {_mm(gap.gap)} from {hole.label}, required "
                    f"{format_mm(req.value)}. Rule: {req.source.describe()}",
                    object_type="hole", object_id=hole.uid, object_label=hole.label,
                    distance=gap.gap,
                    required=req.value, rule_source=req.source.describe(), location=gap.location,
                )  # fmt: skip

    def _keepouts(
        self,
        result: CollisionResult,
        layers: list[str],
        shape: Shape,
        *,
        tracks: bool = False,
        vias: bool = False,
    ) -> None:
        seen: set[str] = set()
        for layer in layers:
            for k in self.geometry.keepouts_near(layer, shape.bounds):
                if k.uid in seen:
                    continue
                forbidden = (tracks and k.rules.tracks) or (vias and k.rules.vias)
                if not forbidden:
                    continue
                seen.add(k.uid)
                result.checks += 1
                if overlaps(shape, k.shape):
                    what = "track" if tracks else "via"
                    self._violation(
                        result, ViolationType.KEEPOUT, f"{what} inside {k.label}",
                        object_type="keepout", object_id=k.uid, object_label=k.label, layer=layer,
                        location=approximate_contact_point(shape, k.shape),
                    )  # fmt: skip

    def _board_edge(
        self,
        result: CollisionResult,
        net: str | None,
        shape: Shape,
        item_type: ItemType,
        probe: Point,
    ) -> None:
        geo = self.geometry
        if geo.region.status is not RegionStatus.KNOWN:
            self._unknown(
                result,
                f"board outline unknown ({geo.region.status.value}): "
                "inside-board check not possible",
            )
            return
        req = self.resolver.resolve_edge_clearance(net, item_type)
        margin = max(req.value or 0, self.search_margin)
        crossing = False
        worst: tuple[float, Point | None] | None = None
        for edge in geo.edges_near(shape.bounds.expanded(margin)):
            result.checks += 1
            if overlaps(shape, edge.shape):
                crossing = True
                self._violation(
                    result, ViolationType.EDGE_CROSSING,
                    "copper crosses the board edge (Edge.Cuts)",
                    object_type="board edge", object_id=edge.uid,
                    location=approximate_contact_point(shape, edge.shape),
                )  # fmt: skip
                break
            if req.value is not None:
                gap = check_clearance(shape, edge.shape, req.value)
                if not gap.ok and gap.gap is not None and (worst is None or gap.gap < worst[0]):
                    worst = (gap.gap, gap.location)
        if not crossing and worst is not None and req.value is not None:
            self._violation(
                result, ViolationType.EDGE_CLEARANCE,
                f"copper {_mm(worst[0])} from the board edge, required "
                f"{format_mm(req.value)}. Rule: {req.source.describe()}",
                object_type="board edge", distance=worst[0], required=req.value,
                rule_source=req.source.describe(), location=worst[1],
            )  # fmt: skip
        if req.value is None:
            self._unknown(result, "copper-to-edge clearance is not stated by any rule")
        if not crossing:
            depth = sum(1 for loop in geo.region.loops if loop.contains(probe))
            if depth == 0:
                self._violation(
                    result,
                    ViolationType.OUTSIDE_BOARD,
                    "copper is outside the board outline",
                    location=probe,
                )
            elif depth % 2 == 0:
                self._violation(
                    result,
                    ViolationType.IN_CUTOUT,
                    "copper is inside a board cutout",
                    location=probe,
                )

    def _finish(self, result: CollisionResult, t0: float) -> CollisionResult:
        if result.collisions:
            result.status = ValidationStatus.INVALID
        elif result.unknowns:
            result.status = (
                ValidationStatus.RULE_UNKNOWN
                if self.resolver.conservative
                else ValidationStatus.VALID_WITH_WARNINGS
            )
            if not self.resolver.conservative:
                result.warnings.extend(
                    f"(conservative handling OFF) {u.message}" for u in result.unknowns
                )
        elif result.warnings:
            result.status = ValidationStatus.VALID_WITH_WARNINGS
        else:
            result.status = ValidationStatus.VALID
        result.elapsed_s = time.perf_counter() - t0
        return result
