"""The individual internal geometry checks.

All checks share one :class:`CheckContext`, use the spatial index for every
neighbour search (no all-pairs scans), and resolve rules through the same
:class:`~pcbrouter.rules.resolver.RuleResolver` the route validator uses — so the
DRC and the validator can never disagree about what is legal.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from pcbrouter.domain.geometry import Point
from pcbrouter.domain.units import format_mm
from pcbrouter.drc.violation import DRCViolation, Severity, ViolationKind, violation_id
from pcbrouter.geometry.board import BoardGeometry, CopperItem, ItemKind, RegionStatus
from pcbrouter.geometry.clearance import (
    approximate_contact_point,
    check_clearance,
    overlaps,
)
from pcbrouter.geometry.shapes import ShapeAccuracy
from pcbrouter.routing.collision import item_type_of
from pcbrouter.routing.connectivity import BoardConnectivity
from pcbrouter.rules.model import ItemType
from pcbrouter.rules.resolver import RuleResolver


@dataclass
class CheckContext:
    geo: BoardGeometry
    resolver: RuleResolver
    connectivity: BoardConnectivity | None
    search_margin: int
    violations: list[DRCViolation] = field(default_factory=list)
    checks: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    #: unsupported rule name -> (count, first violation example)
    possibly: dict[str, int] = field(default_factory=dict)

    def emit(self, v: DRCViolation) -> None:
        self.violations.append(v)

    def skip(self, reason: str, n: int = 1) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + n


def _approx(*items: CopperItem) -> ShapeAccuracy:
    return ShapeAccuracy.worst(*(i.accuracy for i in items))


def _approx_suffix(acc: ShapeAccuracy) -> str:
    return "" if acc is ShapeAccuracy.EXACT else f" [geometry: {acc.label}]"


def _pair_kind(a: CopperItem, b: CopperItem) -> ViolationKind:
    kinds = {a.kind, b.kind}
    if ItemKind.ZONE_FILL in kinds:
        return ViolationKind.ZONE_CLEARANCE
    if kinds == {ItemKind.TRACK}:
        return ViolationKind.TRACK_TRACK_CLEARANCE
    if kinds == {ItemKind.TRACK, ItemKind.PAD}:
        return ViolationKind.TRACK_PAD_CLEARANCE
    if kinds == {ItemKind.TRACK, ItemKind.VIA}:
        return ViolationKind.TRACK_VIA_CLEARANCE
    if ItemKind.VIA in kinds:
        return ViolationKind.VIA_COPPER_CLEARANCE
    return ViolationKind.PAD_PAD_CLEARANCE


# ------------------------------------------------------------------ copper clearance
def check_copper_clearance(ctx: CheckContext) -> None:
    geo, resolver = ctx.geo, ctx.resolver
    for a in geo.copper.values():
        seen: set[str] = set()
        for layer in sorted(a.layers):
            box = a.bounds.expanded(ctx.search_margin)
            for b in geo.copper_near(layer, box):
                if b.uid <= a.uid or b.uid in seen:
                    continue
                seen.add(b.uid)
                if a.net is not None and a.net == b.net:
                    continue
                if a.kind is ItemKind.ZONE_FILL and b.kind is ItemKind.ZONE_FILL:
                    continue  # KiCad zone priorities resolve fill-vs-fill overlap
                if (
                    a.kind is ItemKind.PAD
                    and b.kind is ItemKind.PAD
                    and a.footprint_ref is not None
                    and a.footprint_ref == b.footprint_ref
                ):
                    ctx.skip("pad pairs inside one footprint (footprint design, not routing)")
                    continue
                shared = sorted(a.layers & b.layers)
                ctx.checks += 1
                _check_pair(ctx, resolver, a, b, shared[0])


def _check_pair(
    ctx: CheckContext, resolver: RuleResolver, a: CopperItem, b: CopperItem, layer: str
) -> None:
    req = resolver.resolve_clearance(
        a.net,
        b.net,
        item_type_of(a.kind),
        item_type_of(b.kind),
        layer,
        a.local_clearance,
        b.local_clearance,
        a.label,
        b.label,
    )
    acc = _approx(a, b)
    if req.value is None:
        for sa in a.shapes:
            for sb in b.shapes:
                if overlaps(sa, sb):
                    _emit_pair(
                        ctx,
                        ViolationKind.SHORT,
                        a,
                        b,
                        layer,
                        None,
                        None,
                        "no clearance rule; overlap is always illegal",
                        acc,
                        approximate_contact_point(sa, sb),
                    )
                    return
        ctx.skip("clearance rule unknown (only shorts checked)")
        return
    worst = None
    for sa in a.shapes:
        for sb in b.shapes:
            g = check_clearance(sa, sb, req.value)
            if not g.ok and (worst is None or (g.gap or 0) < (worst.gap or 0)):
                worst = g
    if worst is not None:
        kind = ViolationKind.SHORT if worst.overlap else _pair_kind(a, b)
        _emit_pair(
            ctx, kind, a, b, layer, worst.gap, req.value, req.source.describe(), acc, worst.location
        )
    elif req.possibly_stricter is not None:
        for name in req.possibly_stricter_rules:
            ctx.possibly[name] = ctx.possibly.get(name, 0) + 1


def _emit_pair(
    ctx: CheckContext,
    kind: ViolationKind,
    a: CopperItem,
    b: CopperItem,
    layer: str,
    gap: float | None,
    required: int | None,
    source: str | None,
    acc: ShapeAccuracy,
    location: Point | None,
) -> None:
    what = "overlaps" if kind is ViolationKind.SHORT else "too close to"
    msg = f"{a.label} ({a.net or 'no net'}) {what} {b.label} ({b.net or 'no net'})"
    if gap is not None and kind is not ViolationKind.SHORT:
        msg += f": {format_mm(round(gap))} < {format_mm(required or 0)}"
    if source:
        msg += f". Rule: {source}"
    # Zone fills are stored results of KiCad's filler: a stale fill is a warning.
    severity = Severity.WARNING if ItemKind.ZONE_FILL in (a.kind, b.kind) else Severity.ERROR
    ctx.emit(
        DRCViolation(
            violation_id(kind, a.uid, b.uid),
            kind,
            severity,
            msg + _approx_suffix(acc),
            a.uid,
            b.uid,
            layer,
            location,
            gap,
            required,
            source,
            a.net,
            b.net,
            acc,
        )
    )


# ------------------------------------------------------------------ widths / vias
def check_widths_and_vias(ctx: CheckContext) -> None:
    resolver = ctx.resolver
    for item in ctx.geo.copper.values():
        if item.kind is ItemKind.TRACK and item.width is not None:
            ctx.checks += 1
            layer = next(iter(item.layers))
            rules = resolver.width_rules(item.net, layer)
            fab = rules.fab_minimum
            if fab.value is None:
                ctx.skip("minimum track width unknown")
            elif item.width < fab.value:
                _emit_item(
                    ctx,
                    ViolationKind.MIN_TRACK_WIDTH,
                    item,
                    float(item.width),
                    fab.value,
                    fab.source.describe(),
                    f"track width {format_mm(item.width)} < {format_mm(fab.value)}",
                )
            mx = rules.maximum
            if mx.value is not None and item.width > mx.value:
                _emit_item(
                    ctx,
                    ViolationKind.MAX_TRACK_WIDTH,
                    item,
                    float(item.width),
                    mx.value,
                    mx.source.describe(),
                    f"track width {format_mm(item.width)} > {format_mm(mx.value)}",
                )
        elif item.kind is ItemKind.VIA and item.diameter is not None:
            ctx.checks += 1
            via = resolver.resolve_via_rules(item.net)
            for rule, actual, kind, what in (
                (
                    via.fab_min_diameter,
                    item.diameter,
                    ViolationKind.MIN_VIA_DIAMETER,
                    "via diameter",
                ),
                (via.fab_min_drill, item.drill, ViolationKind.MIN_DRILL, "via drill"),
                (
                    via.min_annular_width,
                    (item.diameter - item.drill) // 2 if item.drill else None,
                    ViolationKind.MIN_ANNULAR,
                    "annular ring",
                ),
            ):
                if actual is None:
                    continue
                if rule.value is None:
                    ctx.skip(f"minimum {what} unknown")
                elif actual < rule.value:
                    _emit_item(
                        ctx,
                        kind,
                        item,
                        float(actual),
                        rule.value,
                        rule.source.describe(),
                        f"{what} {format_mm(actual)} < {format_mm(rule.value)}",
                    )


def _emit_item(
    ctx: CheckContext,
    kind: ViolationKind,
    item: CopperItem,
    actual: float | None,
    required: int | None,
    source: str | None,
    detail: str,
    severity: Severity = Severity.ERROR,
    other: str | None = None,
    location: Point | None = None,
    *,
    show_source: bool = True,
) -> None:
    msg = f"{item.label} ({item.net or 'no net'}): {detail}"
    if source and show_source:
        msg += f". Rule: {source}"
    loc = location if location is not None else item.bounds.center
    ctx.emit(
        DRCViolation(
            violation_id(kind, item.uid, other),
            kind,
            severity,
            msg + _approx_suffix(item.accuracy),
            item.uid,
            other,
            sorted(item.layers)[0] if item.layers else None,
            loc,
            actual,
            required,
            source,
            item.net,
            None,
            item.accuracy,
        )
    )


# ------------------------------------------------------------------ board edge
def check_board_edge(ctx: CheckContext) -> None:
    geo, resolver = ctx.geo, ctx.resolver
    if geo.region.status is not RegionStatus.KNOWN:
        ctx.emit(
            DRCViolation(
                violation_id(ViolationKind.GEOMETRY_NOTE, "outline"),
                ViolationKind.GEOMETRY_NOTE,
                Severity.WARNING,
                f"board outline {geo.region.status.value}: board-edge checks skipped",
            )
        )
        ctx.skip("board-edge checks (outline unknown)", len(geo.copper))
        return
    for item in geo.copper.values():
        ctx.checks += 1
        req = resolver.resolve_edge_clearance(item.net, item_type_of(item.kind))
        margin = max(req.value or 0, ctx.search_margin)
        # Pads at the edge are often intentional (edge connectors, castellations).
        severity = Severity.WARNING if item.kind is ItemKind.PAD else Severity.ERROR
        crossing = None
        worst = None
        for edge in geo.edges_near(item.bounds.expanded(margin)):
            for s in item.shapes:
                if overlaps(s, edge.shape):
                    crossing = approximate_contact_point(s, edge.shape)
                    break
                if req.value is not None:
                    g = check_clearance(s, edge.shape, req.value)
                    if not g.ok and (worst is None or (g.gap or 0) < (worst.gap or 0)):
                        worst = g
            if crossing is not None:
                break
        if crossing is not None:
            _emit_item(
                ctx,
                ViolationKind.EDGE_CROSSING,
                item,
                None,
                None,
                None,
                "copper crosses the board edge",
                severity,
                "edge",
                crossing,
            )
            continue
        if worst is not None and req.value is not None:
            _emit_item(
                ctx,
                ViolationKind.EDGE_CLEARANCE,
                item,
                worst.gap,
                req.value,
                req.source.describe(),
                f"{format_mm(round(worst.gap or 0))} from the board edge < {format_mm(req.value)}",
                severity,
                "edge",
                worst.location,
            )
            continue
        if req.value is None:
            ctx.skip("board-edge clearance unknown (only crossings checked)")
        rep = item.shapes[0].core.representative()
        if not geo.region.contains(rep):
            _emit_item(
                ctx,
                ViolationKind.OUTSIDE_BOARD,
                item,
                None,
                None,
                None,
                "copper outside the board outline or inside a cutout",
                Severity.ERROR,
                "outline",
                rep,
            )


# ------------------------------------------------------------------ keepouts
def check_keepouts(ctx: CheckContext) -> None:
    geo = ctx.geo
    for k in geo.keepouts.values():
        done: set[str] = set()  # an item on several keepout layers is reported once
        for layer in sorted(k.layers):
            for item in geo.copper_near(layer, k.bounds):
                if item.uid in done:
                    continue
                if item.kind is ItemKind.TRACK and k.rules.tracks:
                    kind = ViolationKind.TRACK_IN_KEEPOUT
                elif item.kind is ItemKind.VIA and k.rules.vias:
                    kind = ViolationKind.VIA_IN_KEEPOUT
                elif (
                    item.kind is ItemKind.PAD
                    and k.rules.pads
                    and item.footprint_ref != k.footprint_ref
                ):
                    kind = ViolationKind.PAD_IN_KEEPOUT
                else:
                    continue
                ctx.checks += 1
                for s in item.shapes:
                    if overlaps(s, k.shape):
                        done.add(item.uid)
                        _emit_item(
                            ctx,
                            kind,
                            item,
                            None,
                            None,
                            k.label,
                            f"inside {k.label}",
                            Severity.ERROR,
                            k.uid,
                            approximate_contact_point(s, k.shape),
                            show_source=False,
                        )
                        break


# ------------------------------------------------------------------ holes
def check_holes(ctx: CheckContext) -> None:
    geo, resolver = ctx.geo, ctx.resolver
    h2h = resolver.resolve_hole_to_hole()
    holes = sorted(geo.holes.values(), key=lambda h: h.uid)
    for h in holes:
        for other in geo.holes_near(h.bounds.expanded(ctx.search_margin)):
            if other.uid <= h.uid:
                continue
            ctx.checks += 1
            if overlaps(h.shape, other.shape):
                _emit_hole(
                    ctx,
                    ViolationKind.HOLE_TO_HOLE,
                    h.uid,
                    other.uid,
                    f"{h.label} overlaps {other.label}",
                    None,
                    None,
                    None,
                    approximate_contact_point(h.shape, other.shape),
                )
            elif h2h.value is not None:
                g = check_clearance(h.shape, other.shape, h2h.value)
                if not g.ok:
                    _emit_hole(
                        ctx,
                        ViolationKind.HOLE_TO_HOLE,
                        h.uid,
                        other.uid,
                        f"{h.label} is {format_mm(round(g.gap or 0))} from {other.label} "
                        f"< {format_mm(h2h.value)}",
                        g.gap,
                        h2h.value,
                        h2h.source.describe(),
                        g.location,
                    )
            else:
                ctx.skip("hole-to-hole minimum unknown")
    # Copper vs mechanical (NPTH) holes and foreign holes without their own copper.
    for h in holes:
        if h.plated:
            continue
        for layer in geo.copper_layers:
            for item in geo.copper_near(layer, h.bounds.expanded(ctx.search_margin)):
                ctx.checks += 1
                req = resolver.resolve_hole_clearance(item.net, item_type_of(item.kind), layer)
                for s in item.shapes:
                    if overlaps(s, h.shape):
                        _emit_hole(
                            ctx,
                            ViolationKind.HOLE_CLEARANCE,
                            item.uid,
                            h.uid,
                            f"{item.label} overlaps {h.label}",
                            None,
                            None,
                            None,
                            approximate_contact_point(s, h.shape),
                        )
                        break
                    if req.value is None:
                        ctx.skip("copper-to-hole clearance unknown")
                        break
                    g = check_clearance(s, h.shape, req.value)
                    if not g.ok:
                        _emit_hole(
                            ctx,
                            ViolationKind.HOLE_CLEARANCE,
                            item.uid,
                            h.uid,
                            f"{item.label} is {format_mm(round(g.gap or 0))} from {h.label} "
                            f"< {format_mm(req.value)}",
                            g.gap,
                            req.value,
                            req.source.describe(),
                            g.location,
                        )
                        break


def _emit_hole(
    ctx: CheckContext,
    kind: ViolationKind,
    a: str,
    b: str,
    msg: str,
    gap: float | None,
    required: int | None,
    source: str | None,
    location: Point | None,
) -> None:
    if source:
        msg += f". Rule: {source}"
    if any(v.id == violation_id(kind, a, b) for v in ctx.violations):
        return
    ctx.emit(
        DRCViolation(
            violation_id(kind, a, b),
            kind,
            Severity.ERROR,
            msg,
            a,
            b,
            None,
            location,
            gap,
            required,
            source,
        )
    )


# ------------------------------------------------------------------ disallow / connectivity / rules
def check_disallowed(ctx: CheckContext) -> None:
    for item in ctx.geo.copper.values():
        if item.kind not in (ItemKind.TRACK, ItemKind.VIA):
            continue
        what = "track" if item.kind is ItemKind.TRACK else "via"
        d = ctx.resolver.disallowed(
            what, item.net, next(iter(item.layers)) if item.kind is ItemKind.TRACK else None
        )
        ctx.checks += 1
        if d.forbidden:
            _emit_item(
                ctx,
                ViolationKind.DISALLOWED,
                item,
                None,
                None,
                "Custom rule " + ", ".join(d.rules),
                f"{what}s are disallowed",
            )


def check_unconnected(ctx: CheckContext) -> None:
    if ctx.connectivity is None:
        return
    for wire in ctx.connectivity.airwires:
        ctx.checks += 1
        a = ctx.geo.copper[wire.from_uid]
        b = ctx.geo.copper[wire.to_uid]
        ctx.emit(
            DRCViolation(
                violation_id(ViolationKind.UNCONNECTED, wire.from_uid, wire.to_uid),
                ViolationKind.UNCONNECTED,
                Severity.INFO,
                f"{wire.net}: {a.label} is not connected to {b.label} "
                f"({format_mm(round(wire.length))} airwire)",
                wire.from_uid,
                wire.to_uid,
                None,
                wire.midpoint,
                wire.length,
                None,
                None,
                wire.net,
                wire.net,
            )
        )


def report_rules_and_geometry(ctx: CheckContext) -> None:
    rs = ctx.resolver.ruleset
    for u in rs.unsupported:
        sev = Severity.WARNING if u.critical else Severity.INFO
        text = f"Unsupported deterministic rule detected: {u.describe()}." + (
            " Routing validation affected by it reports RULE_UNKNOWN until supported."
            if u.critical
            else ""
        )
        ctx.emit(
            DRCViolation(
                violation_id(ViolationKind.UNSUPPORTED_RULE, u.name, u.location),
                ViolationKind.UNSUPPORTED_RULE,
                sev,
                text,
                rule_source=u.location,
            )
        )
    for name, count in sorted(ctx.possibly.items()):
        ctx.emit(
            DRCViolation(
                violation_id(ViolationKind.RULE_UNKNOWN, name),
                ViolationKind.RULE_UNKNOWN,
                Severity.WARNING,
                f"{count} copper pair(s) pass the known rules but may violate "
                f"unsupported rule '{name}'",
                rule_source=name,
            )
        )
    # Checks that could not run because no rule states the value: never report
    # "PASS" for something that was not verified.
    for reason, count in sorted(ctx.skipped.items()):
        if "unknown" not in reason:
            continue
        ctx.emit(
            DRCViolation(
                violation_id(ViolationKind.RULE_UNKNOWN, reason),
                ViolationKind.RULE_UNKNOWN,
                Severity.WARNING,
                f"{count} check(s) not verified: {reason}. Add the rule in KiCad (Board Setup) "
                "or set a routing-rule override.",
            )
        )
    for note in ctx.geo.unfilled_zones:
        ctx.emit(
            DRCViolation(
                violation_id(ViolationKind.GEOMETRY_NOTE, note),
                ViolationKind.GEOMETRY_NOTE,
                Severity.INFO,
                f"{note} has no stored fill: its copper is unknown and not checked",
            )
        )


ALL_CHECKS: tuple[tuple[str, Callable[[CheckContext], None]], ...] = (
    ("copper clearance", check_copper_clearance),
    ("track width / via size", check_widths_and_vias),
    ("board edge", check_board_edge),
    ("keepouts", check_keepouts),
    ("holes", check_holes),
    ("disallow rules", check_disallowed),
    ("unconnected items", check_unconnected),
    ("rules and geometry notes", report_rules_and_geometry),
)

__all__ = ["ALL_CHECKS", "CheckContext", "ItemType"]
