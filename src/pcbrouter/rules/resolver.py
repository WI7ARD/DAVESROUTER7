"""The central rule resolver: every deterministic rule question goes through here.

Precedence (documented in docs/rules_engine.md and applied uniformly):

1. **Custom rules** (``.kicad_dru``) whose condition matches — later rules in the
   file win, as in KiCad. For pair constraints both orders (A,B)/(B,A) are tried.
2. **Local item clearance** (pad / footprint / zone) — replaces the net-class value
   for that item (clearance only).
3. **Net class** — for a pair, the larger of the two classes' values.
4. **Board minimum** — always applied as a floor to clearances and minimum sizes.
5. **Explicit user / approved-AI overrides** — may *tighten* a known value or
   *define* one that no rule states; they never weaken a rule.
6. Otherwise the value is **unknown** (never fabricated).

Width/via "routing minimum" policy: the net-class width (or via size) is the
designer's intent for new routing, so the router treats it as the minimum for new
copper unless a custom rule states an explicit ``min``. The *fabrication* minimum
(used by internal DRC of existing copper) is the board minimum / custom rule min.

Unsupported rules: when a critical rule cannot be evaluated, resolved values carry
``possibly_stricter`` so checks that pass the known rules but not the possible
stricter one report RULE_UNKNOWN rather than VALID.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pcbrouter.domain.units import Nm, internal_to_mm
from pcbrouter.rules.conditions import ItemFacts
from pcbrouter.rules.model import (
    UNBOUNDED,
    ItemType,
    LayerRules,
    NetRuleSummary,
    ResolvedValue,
    RuleSource,
    RuleSourceKind,
    ViaRules,
    WidthRules,
    unknown,
)
from pcbrouter.rules.netclass import Membership
from pcbrouter.rules.overrides import RuleOverrides
from pcbrouter.rules.ruleset import CompiledRule, RuleSet


@dataclass(frozen=True, slots=True)
class CustomMatch:
    """The highest-precedence custom rule matching a query, and its values."""

    rule: CompiledRule
    min: Nm | None
    opt: Nm | None
    max: Nm | None


@dataclass(frozen=True, slots=True)
class DisallowResult:
    forbidden: bool
    rules: tuple[str, ...] = ()
    #: Unsupported disallow rules that might forbid this item.
    unknown_rules: tuple[str, ...] = ()


class RuleResolver:
    def __init__(
        self,
        ruleset: RuleSet,
        overrides: RuleOverrides | None = None,
        *,
        conservative: bool = True,
    ) -> None:
        self.ruleset = ruleset
        self.overrides = overrides or RuleOverrides()
        self.conservative = conservative
        self._cache: dict[tuple[Any, ...], Any] = {}

    # ------------------------------------------------------------ helpers
    def facts(
        self, net: str | None, item_type: ItemType = ItemType.TRACK, layer: str | None = None
    ) -> ItemFacts:
        return ItemFacts(net, self.resolve_net_class(net).classes, item_type, layer)

    def _custom(
        self, kind: str, a: ItemFacts, b: ItemFacts | None = None, layer: str | None = None
    ) -> CustomMatch | None:
        for rule in self.ruleset.rules_with(kind):
            if not rule.applies_to_layer(layer):
                continue
            cond = rule.condition
            matched = cond is None or cond.matches(a, b) or (b is not None and cond.matches(b, a))
            if matched:
                c = rule.constraint(kind)
                assert c is not None
                return CustomMatch(rule, c.min, c.opt, c.max)
        return None

    @staticmethod
    def _custom_source(rule: CompiledRule) -> RuleSource:
        return RuleSource(RuleSourceKind.CUSTOM_RULE, rule.name, rule.location)

    def _board_min(self, attr: str) -> ResolvedValue | None:
        value = getattr(self.ruleset.board, attr)
        if value is None:
            return None
        return ResolvedValue(
            value, RuleSource(RuleSourceKind.BOARD_MINIMUM, self.ruleset.board_value_source(attr))
        )

    @staticmethod
    def _floor(value: ResolvedValue, floor: ResolvedValue | None) -> ResolvedValue:
        if floor is None or floor.value is None:
            return value
        if value.value is None or floor.value > value.value:
            return ResolvedValue(
                floor.value,
                floor.source,
                value.possibly_stricter,
                value.possibly_stricter_rules,
                value.notes,
            )
        return value

    def _with_unsupported(self, value: ResolvedValue, *kinds: str) -> ResolvedValue:
        """Attach a possibly-stricter bound from unsupported critical rules."""
        bound: Nm | None = None
        names: list[str] = []
        for rule in self.ruleset.unsupported:
            if not rule.critical:
                continue
            lo = rule.min_for(*kinds)
            if lo is None:
                continue
            if value.value is not None and lo <= value.value:
                continue
            bound = lo if bound is None else max(bound, lo)
            names.append(rule.name)
        if bound is None:
            return value
        return ResolvedValue(value.value, value.source, bound, tuple(names), value.notes)

    def _tighten(
        self, value: ResolvedValue, override: Nm | None, source: RuleSource
    ) -> ResolvedValue:
        """Apply an override that may only tighten (or define an unknown) value."""
        if override is None:
            return value
        if value.value is None or override > value.value:
            return ResolvedValue(
                override,
                source,
                value.possibly_stricter,
                value.possibly_stricter_rules,
                value.notes,
            )
        if override < value.value:
            note = (
                f"override {internal_to_mm(override):g} mm is below the rule value "
                f"{internal_to_mm(value.value):g} mm and is not applied"
            )
            return ResolvedValue(
                value.value,
                value.source,
                value.possibly_stricter,
                value.possibly_stricter_rules,
                (*value.notes, note),
            )
        return value

    def _override_source(self, net: str | None) -> RuleSource:
        ov = self.overrides.for_net(net)
        return (
            ov.source()
            if ov is not None
            else RuleSource(RuleSourceKind.USER_OVERRIDE, "board default")
        )

    # ------------------------------------------------------------ net classes
    def resolve_net_class(self, net: str | None) -> Membership:
        key = ("class", net)
        if key not in self._cache:
            self._cache[key] = self.ruleset.classes.membership(net)
        result: Membership = self._cache[key]
        return result

    # ------------------------------------------------------------ widths
    def width_rules(self, net: str | None, layer: str | None = None) -> WidthRules:
        key = ("width", net, layer)
        cached = self._cache.get(key)
        if cached is not None:
            return cached  # type: ignore[no-any-return]
        facts = self.facts(net, ItemType.TRACK, layer)
        custom = self._custom("track_width", facts, None, layer)
        class_width = self.ruleset.classes.value_for_net(net, "track_width")
        board_min = self._board_min("min_track_width")

        if custom and custom.opt is not None:
            preferred = ResolvedValue(custom.opt, self._custom_source(custom.rule))
        else:
            preferred = class_width
        if custom and custom.min is not None:
            fab_min = ResolvedValue(custom.min, self._custom_source(custom.rule))
            routing_min = fab_min
        else:
            fab_min = unknown("no minimum track width stated")
            routing_min = class_width
        fab_min = self._floor(fab_min, board_min)
        routing_min = self._floor(routing_min, board_min)
        maximum = (
            ResolvedValue(custom.max, self._custom_source(custom.rule))
            if custom and custom.max is not None
            else unknown("no maximum track width stated")
        )
        # Overrides: an explicit user width defines the minimum only when no rule does.
        net_ov = self.overrides.for_net(net)
        ov_width = (
            net_ov.width
            if net_ov and net_ov.width is not None
            else self.overrides.board.track_width
        )
        if ov_width is not None:
            src = self._override_source(net)
            if routing_min.value is None:
                routing_min = ResolvedValue(
                    ov_width, src, notes=("explicit user rule (no source rule states a width)",)
                )
            if routing_min.value is not None and ov_width >= routing_min.value:
                preferred = ResolvedValue(ov_width, src)
            elif routing_min.value is not None:
                below = f"width override {internal_to_mm(ov_width):g} mm is below the minimum"
                preferred = ResolvedValue(
                    preferred.value, preferred.source, notes=(f"{below} and is not applied",)
                )
        if preferred.value is None and routing_min.value is not None:
            preferred = ResolvedValue(
                routing_min.value,
                routing_min.source,
                notes=("no preferred width stated; minimum used",),
            )
        routing_min = self._with_unsupported(routing_min, "track_width")
        fab_min = self._with_unsupported(fab_min, "track_width")
        # Unsupported maxima: widths above them are RULE_UNKNOWN.
        lowest_max: Nm | None = None
        max_names: list[str] = []
        for rule in self.ruleset.unsupported:
            hi = rule.max_for("track_width")
            if rule.critical and hi is not None and (maximum.value is None or hi < maximum.value):
                lowest_max = hi if lowest_max is None else min(lowest_max, hi)
                max_names.append(rule.name)
        if lowest_max is not None:
            maximum = ResolvedValue(
                maximum.value, maximum.source, lowest_max, tuple(max_names), maximum.notes
            )
        result = WidthRules(preferred, routing_min, fab_min, maximum)
        self._cache[key] = result
        return result

    def resolve_trace_width(self, net: str | None, layer: str | None = None) -> ResolvedValue:
        return self.width_rules(net, layer).preferred

    def resolve_min_trace_width(self, net: str | None, layer: str | None = None) -> ResolvedValue:
        return self.width_rules(net, layer).minimum

    def resolve_max_trace_width(self, net: str | None, layer: str | None = None) -> ResolvedValue:
        return self.width_rules(net, layer).maximum

    # ------------------------------------------------------------ clearance
    def resolve_clearance(
        self,
        net_a: str | None,
        net_b: str | None,
        type_a: ItemType = ItemType.TRACK,
        type_b: ItemType = ItemType.TRACK,
        layer: str | None = None,
        local_a: Nm | None = None,
        local_b: Nm | None = None,
        label_a: str | None = None,
        label_b: str | None = None,
    ) -> ResolvedValue:
        key = ("clr", net_a, net_b, type_a, type_b, layer, local_a, local_b)
        cached = self._cache.get(key)
        if cached is not None:
            return cached  # type: ignore[no-any-return]
        a = self.facts(net_a, type_a, layer)
        b = self.facts(net_b, type_b, layer)
        custom = self._custom("clearance", a, b, layer)
        value: ResolvedValue
        if custom and custom.min is not None:
            value = ResolvedValue(custom.min, self._custom_source(custom.rule))
        else:
            candidates: list[ResolvedValue] = []
            for local, net, label in ((local_a, net_a, label_a), (local_b, net_b, label_b)):
                if local is not None:
                    candidates.append(
                        ResolvedValue(local, RuleSource(RuleSourceKind.ITEM_LOCAL, label))
                    )
                else:
                    candidates.append(self.ruleset.classes.value_for_net(net, "clearance"))
            known = [c for c in candidates if c.value is not None]
            value = (
                max(known, key=lambda c: c.value or 0)
                if known
                else unknown("no clearance rule applies")
            )
        value = self._floor(value, self._board_min("min_clearance"))
        for net in (net_a, net_b):
            ov = self.overrides.for_net(net)
            if ov is not None and ov.clearance is not None:
                value = self._tighten(value, ov.clearance, ov.source())
        if value.value is None and self.overrides.board.clearance is not None:
            value = ResolvedValue(
                self.overrides.board.clearance,
                RuleSource(RuleSourceKind.USER_OVERRIDE, "board default clearance"),
                notes=("explicit user rule (no source rule states a clearance)",),
            )
        value = self._with_unsupported(value, "clearance", "physical_clearance")
        self._cache[key] = value
        return value

    def resolve_net_clearance(self, net: str | None) -> ResolvedValue:
        """Clearance of ``net`` against another net of the same class (what the Rules
        Inspector shows; pairs with other classes use the larger class value)."""
        return self.resolve_clearance(net, net)

    def resolve_hole_clearance(
        self, net: str | None, item_type: ItemType = ItemType.TRACK, layer: str | None = None
    ) -> ResolvedValue:
        a = self.facts(net, item_type, layer)
        hole = ItemFacts(None, (), ItemType.HOLE, layer)
        custom = self._custom("hole_clearance", a, hole, layer)
        value = (
            ResolvedValue(custom.min, self._custom_source(custom.rule))
            if custom and custom.min is not None
            else unknown("no copper-to-hole clearance stated")
        )
        value = self._floor(value, self._board_min("min_hole_clearance"))
        return self._with_unsupported(value, "hole_clearance", "physical_hole_clearance")

    def resolve_hole_to_hole(self) -> ResolvedValue:
        value = self._board_min("min_hole_to_hole") or unknown("no hole-to-hole minimum stated")
        return self._with_unsupported(value, "hole_to_hole")

    def resolve_edge_clearance(
        self, net: str | None = None, item_type: ItemType = ItemType.TRACK, layer: str | None = None
    ) -> ResolvedValue:
        a = self.facts(net, item_type, layer)
        custom = self._custom("edge_clearance", a, None, layer)
        value = (
            ResolvedValue(custom.min, self._custom_source(custom.rule))
            if custom and custom.min is not None
            else unknown("no copper-to-edge clearance stated")
        )
        value = self._floor(value, self._board_min("min_copper_edge_clearance"))
        value = self._tighten(
            value,
            self.overrides.board.edge_clearance,
            RuleSource(RuleSourceKind.USER_OVERRIDE, "board-edge clearance"),
        )
        return self._with_unsupported(value, "edge_clearance")

    # ------------------------------------------------------------ vias
    def resolve_via_rules(self, net: str | None, layer: str | None = None) -> ViaRules:
        key = ("via", net, layer)
        cached = self._cache.get(key)
        if cached is not None:
            return cached  # type: ignore[no-any-return]
        facts = self.facts(net, ItemType.VIA, layer)
        classes = self.ruleset.classes
        dia_custom = self._custom("via_diameter", facts, None, layer)
        hole_custom = self._custom("hole_size", facts, None, layer)
        ann_custom = self._custom("annular_width", facts, None, layer)
        class_dia = classes.value_for_net(net, "via_diameter")
        class_drill = classes.value_for_net(net, "via_drill")

        def pick(custom: CustomMatch | None, attr: str) -> ResolvedValue | None:
            v = getattr(custom, attr) if custom is not None else None
            if custom is None or v is None:
                return None
            return ResolvedValue(v, self._custom_source(custom.rule))

        diameter = pick(dia_custom, "opt") or class_dia
        drill = pick(hole_custom, "opt") or class_drill
        fab_min_dia = self._floor(
            pick(dia_custom, "min") or unknown("no minimum via diameter stated"),
            self._board_min("min_via_diameter"),
        )
        min_dia = self._floor(
            pick(dia_custom, "min") or class_dia, self._board_min("min_via_diameter")
        )
        board_drill_min = self._board_min("min_through_hole_diameter") or self._board_min(
            "min_via_drill"
        )
        fab_min_drill = self._floor(
            pick(hole_custom, "min") or unknown("no minimum drill stated"), board_drill_min
        )
        min_drill = self._floor(pick(hole_custom, "min") or class_drill, board_drill_min)
        annular = self._floor(
            pick(ann_custom, "min") or unknown("no minimum annular ring stated"),
            self._board_min("min_via_annular_width"),
        )
        rules = ViaRules(
            diameter=diameter,
            min_diameter=self._with_unsupported(min_dia, "via_diameter"),
            fab_min_diameter=self._with_unsupported(fab_min_dia, "via_diameter"),
            drill=drill,
            min_drill=self._with_unsupported(min_drill, "hole_size"),
            fab_min_drill=self._with_unsupported(fab_min_drill, "hole_size"),
            min_annular_width=self._with_unsupported(annular, "annular_width"),
            microvia_diameter=self._floor(
                classes.value_for_net(net, "microvia_diameter"),
                self._board_min("min_microvia_diameter"),
            ),
            microvia_drill=self._floor(
                classes.value_for_net(net, "microvia_drill"), self._board_min("min_microvia_drill")
            ),
        )
        self._cache[key] = rules
        return rules

    # ------------------------------------------------------------ layers / vias count
    def resolve_allowed_layers(self, net: str | None) -> LayerRules:
        allowed = self.ruleset.copper_layers
        source = RuleSource(RuleSourceKind.BOARD_STACKUP)
        ov = self.overrides.for_net(net)
        forbidden: tuple[str, ...] = ()
        if ov is not None:
            if ov.allowed_layers is not None:
                allowed = tuple(layer for layer in allowed if layer in ov.allowed_layers)
                source = ov.source()
            forbidden = tuple(
                layer for layer in ov.forbidden_layers if layer in self.ruleset.copper_layers
            )
            if forbidden:
                allowed = tuple(layer for layer in allowed if layer not in forbidden)
                source = ov.source()
        return LayerRules(allowed, source, forbidden)

    def resolve_max_vias(self, net: str | None) -> ResolvedValue:
        ov = self.overrides.for_net(net)
        if ov is not None and ov.max_vias is not None:
            return ResolvedValue(ov.max_vias, ov.source())
        if self.overrides.board.max_vias is not None:
            return ResolvedValue(
                self.overrides.board.max_vias,
                RuleSource(RuleSourceKind.USER_OVERRIDE, "board default"),
            )
        return unknown("no via-count limit set")

    # ------------------------------------------------------------ disallow
    def disallowed(self, item: str, net: str | None, layer: str | None = None) -> DisallowResult:
        """Custom ``(constraint disallow ...)`` rules forbidding ``item`` ("track",
        "via", ...) for ``net``. Rules with unsupported conditions are reported as
        unknown (they *might* forbid it)."""
        item_type = {"track": ItemType.TRACK, "via": ItemType.VIA, "pad": ItemType.PAD}.get(
            item, ItemType.TRACK
        )
        facts = self.facts(net, item_type, layer)
        hits: list[str] = []
        for rule in self.ruleset.rules_with("disallow"):
            c = rule.constraint("disallow")
            assert c is not None
            if item not in c.items:
                continue
            if rule.applies_to_layer(layer) and (
                rule.condition is None or rule.condition.matches(facts)
            ):
                hits.append(rule.name)
        unknown_rules = tuple(
            u.name for u in self.ruleset.unsupported if u.critical and item in u.disallow_items
        )
        return DisallowResult(bool(hits), tuple(hits), unknown_rules)

    # ------------------------------------------------------------ summaries
    def summary(self, net: str | None) -> NetRuleSummary:
        membership = self.resolve_net_class(net)
        width = self.width_rules(net)
        notes: list[str] = []
        for v in (width.preferred, width.minimum):
            notes.extend(v.notes)
        return NetRuleSummary(
            net=net or "",
            net_classes=membership.classes,
            class_source=membership.source,
            width=width,
            clearance=self.resolve_net_clearance(net),
            via=self.resolve_via_rules(net),
            layers=self.resolve_allowed_layers(net),
            max_vias=self.resolve_max_vias(net),
            edge_clearance=self.resolve_edge_clearance(net),
            notes=tuple(dict.fromkeys(notes)),
        )

    def unbounded(self, value: ResolvedValue) -> bool:
        return value.possibly_stricter is not None and value.possibly_stricter >= UNBOUNDED
