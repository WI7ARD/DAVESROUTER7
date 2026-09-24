"""Normalised rule objects with traceable sources.

Every value the rule engine hands out is a :class:`ResolvedValue`: the value (or
``None`` when unknown — never invented), *where it came from* (:class:`RuleSource`),
and whether an unsupported rule might impose something stricter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from pcbrouter.domain.units import Nm, format_mm


class RuleError(Exception):
    """Base class for rule-engine failures."""

    def __init__(self, message: str, user_message: str | None = None) -> None:
        super().__init__(message)
        self.user_message = user_message or message


class RuleResolutionError(RuleError):
    """A rule query could not be answered (e.g. unknown layer)."""


class UnsupportedRuleError(RuleError):
    """A recognised rule cannot be evaluated by this engine version."""


class RuleSourceKind(Enum):
    USER_OVERRIDE = "user_override"
    AI_CONSTRAINT = "approved_ai_constraint"
    CUSTOM_RULE = "custom_rule"
    NET_CLASS = "net_class"
    BOARD_MINIMUM = "board_minimum"
    BOARD_STACKUP = "board_stackup"
    ITEM_LOCAL = "item_local"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RuleSource:
    kind: RuleSourceKind
    name: str | None = None
    detail: str | None = None  # file and line, e.g. "board.kicad_dru:12"

    def describe(self) -> str:
        k = self.kind
        if k is RuleSourceKind.NET_CLASS:
            text = f'Net class "{self.name}"'
        elif k is RuleSourceKind.CUSTOM_RULE:
            text = f'Custom rule "{self.name}"'
        elif k is RuleSourceKind.BOARD_MINIMUM:
            text = "Board minimum" + (f" ({self.name})" if self.name else "")
        elif k is RuleSourceKind.USER_OVERRIDE:
            text = "User override" + (f" ({self.name})" if self.name else "")
        elif k is RuleSourceKind.AI_CONSTRAINT:
            text = f"Approved AI constraint {self.name}" if self.name else "Approved AI constraint"
        elif k is RuleSourceKind.BOARD_STACKUP:
            text = "Board copper stack-up"
        elif k is RuleSourceKind.ITEM_LOCAL:
            text = f"Local clearance of {self.name}" if self.name else "Local clearance"
        else:
            text = "Unknown (no rule found)"
        return f"{text} [{self.detail}]" if self.detail else text


UNKNOWN_SOURCE = RuleSource(RuleSourceKind.UNKNOWN)


class RuleStatus(Enum):
    KNOWN = "known"
    UNKNOWN = "unknown"  # no applicable rule: callers must not guess


@dataclass(frozen=True, slots=True)
class ResolvedValue:
    value: Nm | None
    source: RuleSource = UNKNOWN_SOURCE
    #: An unsupported rule *might* require up to this (stricter) value.
    possibly_stricter: Nm | None = None
    possibly_stricter_rules: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def status(self) -> RuleStatus:
        return RuleStatus.KNOWN if self.value is not None else RuleStatus.UNKNOWN

    @property
    def known(self) -> bool:
        return self.value is not None

    def describe(self) -> str:
        if self.value is None:
            return f"unknown — {self.source.describe()}"
        text = f"{format_mm(self.value)} — {self.source.describe()}"
        if self.possibly_stricter is not None:
            text += (
                f" (unsupported rule {', '.join(self.possibly_stricter_rules)} may require "
                f"{format_mm(self.possibly_stricter)})"
            )
        return text


def unknown(note: str | None = None) -> ResolvedValue:
    return ResolvedValue(None, UNKNOWN_SOURCE, notes=(note,) if note else ())


class ItemType(Enum):
    """Item types as KiCad rule conditions name them (``A.Type == 'Via'``)."""

    TRACK = "Track"
    VIA = "Via"
    PAD = "Pad"
    ZONE = "Zone"
    HOLE = "Hole"


#: Constraint kinds this engine evaluates.
SUPPORTED_CONSTRAINTS = frozenset(
    {
        "clearance",
        "track_width",
        "via_diameter",
        "hole_size",
        "hole_clearance",
        "edge_clearance",
        "hole_to_hole",
        "annular_width",
        "disallow",
    }
)
#: Constraint kinds that affect whether copper is geometrically legal. When one of
#: these cannot be evaluated, affected checks become RULE_UNKNOWN (never "pass").
CRITICAL_CONSTRAINTS = SUPPORTED_CONSTRAINTS | {"physical_clearance", "physical_hole_clearance"}
#: Recognised KiCad constraint kinds that are not about copper legality (or belong to
#: later stages). They are listed in the snapshot but do not block routing.
KNOWN_NONCRITICAL = frozenset(
    {
        "courtyard_clearance",
        "silk_clearance",
        "text_height",
        "text_thickness",
        "thermal_relief_gap",
        "thermal_spoke_width",
        "zone_connection",
        "min_resolved_spokes",
        "length",
        "skew",
        "diff_pair_gap",
        "diff_pair_uncoupled",
        "via_count",
        "assertion",
        "connection_width",
        "creepage",
        "bridged_mask",
        "solder_mask_expansion",
        "solder_paste_abs_margin",
        "solder_paste_rel_margin",
    }
)


@dataclass(frozen=True, slots=True)
class Constraint:
    kind: str
    min: Nm | None = None
    opt: Nm | None = None
    max: Nm | None = None
    items: tuple[str, ...] = ()  # disallow targets


#: "Might require anything": used when an unsupported rule's value itself could not
#: be read, so every check it might affect becomes RULE_UNKNOWN.
UNBOUNDED: Nm = 10**12


@dataclass(frozen=True, slots=True)
class UnsupportedRule:
    name: str
    reason: str
    constraint_kinds: tuple[str, ...]
    critical: bool
    location: str | None = None  # "file:line"
    #: (constraint kind, min, max) as far as they could be read (None = unreadable).
    limits: tuple[tuple[str, Nm | None, Nm | None], ...] = ()
    disallow_items: tuple[str, ...] = ()

    def min_for(self, *kinds: str) -> Nm | None:
        """Largest minimum this rule might impose for ``kinds`` (UNBOUNDED if a
        relevant value could not be read)."""
        found: Nm | None = None
        for kind, lo, _hi in self.limits:
            if kind in kinds:
                value = UNBOUNDED if lo is None else lo
                found = value if found is None else max(found, value)
        return found

    def max_for(self, kind: str) -> Nm | None:
        found: Nm | None = None
        for k, _lo, hi in self.limits:
            if k == kind and hi is not None:
                found = hi if found is None else min(found, hi)
        return found

    def describe(self) -> str:
        where = f" [{self.location}]" if self.location else ""
        kinds = ", ".join(self.constraint_kinds) or "no constraints"
        return f'"{self.name}" ({kinds}): {self.reason}{where}'


@dataclass(frozen=True, slots=True)
class NetClassRules:
    """A net class with values resolved against the Default class (KiCad 9 lets
    classes leave fields empty and inherit them from Default)."""

    name: str
    clearance: ResolvedValue
    track_width: ResolvedValue
    via_diameter: ResolvedValue
    via_drill: ResolvedValue
    microvia_diameter: ResolvedValue
    microvia_drill: ResolvedValue


@dataclass(frozen=True, slots=True)
class ViaRules:
    diameter: ResolvedValue  # preferred
    min_diameter: ResolvedValue  # routing minimum (see docs/rules_engine.md)
    fab_min_diameter: ResolvedValue  # fabrication minimum (DRC of existing vias)
    drill: ResolvedValue
    min_drill: ResolvedValue
    fab_min_drill: ResolvedValue
    min_annular_width: ResolvedValue
    microvia_diameter: ResolvedValue
    microvia_drill: ResolvedValue


@dataclass(frozen=True, slots=True)
class LayerRules:
    allowed: tuple[str, ...]
    source: RuleSource
    forbidden: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WidthRules:
    preferred: ResolvedValue
    minimum: ResolvedValue  # routing minimum
    fab_minimum: ResolvedValue  # fabrication minimum
    maximum: ResolvedValue


@dataclass(frozen=True, slots=True)
class NetRuleSummary:
    """Everything the Rules Inspector / AI context shows for one net."""

    net: str
    net_classes: tuple[str, ...]
    class_source: RuleSource
    width: WidthRules
    clearance: ResolvedValue
    via: ViaRules
    layers: LayerRules
    max_vias: ResolvedValue
    edge_clearance: ResolvedValue
    notes: tuple[str, ...] = field(default_factory=tuple)
