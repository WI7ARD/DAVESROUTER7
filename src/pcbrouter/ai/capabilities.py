"""What the application really does with each AI routing constraint (1.1.1).

Every field of :class:`~pcbrouter.ai.command_schema.RoutingConstraints` is
classified per operation, so an approved command never promises more than the
deterministic router delivers:

* ``ENFORCED``       the router or rule engine guarantees it (hard limit / exact value)
* ``SOFT``           a preference: it changes costs or order, nothing is guaranteed
* ``ANALYSIS_ONLY``  measured and reported after routing; routing ignores it
* ``UNSUPPORTED``    not implemented: the validator rejects the command

The validator rejects ``UNSUPPORTED`` constraints on routing and constraint
operations, and the approval panel labels every other field with its class.
``docs/CAPABILITIES.md`` is the human-readable copy of :data:`MATRIX`; a test keeps
the two and the schema in step.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pcbrouter.ai.command_schema import Operation, RoutingConstraints


class Capability(StrEnum):
    ENFORCED = "enforced"
    SOFT = "soft"
    ANALYSIS_ONLY = "analysis_only"
    UNSUPPORTED = "unsupported"

    @property
    def label(self) -> str:
        return {
            "enforced": "enforced",
            "soft": "preference (not guaranteed)",
            "analysis_only": "reported after routing, not enforced",
            "unsupported": "not supported",
        }[self.value]


class OpKind(StrEnum):
    ROUTE_NET = "route_net"
    ROUTE_GROUP = "route_group"  # also the batch plan of several approved commands
    ROUTE_BOARD = "route_board"
    OPTIMIZE = "optimize"  # optimize_net, reduce_vias
    SET_CONSTRAINT = "set_net_constraint"
    SET_PRIORITY = "set_routing_priority"
    OTHER = "other"  # analysis, explanation, locks, protected areas


def op_kind(op: Operation) -> OpKind:
    return {
        Operation.ROUTE_NET: OpKind.ROUTE_NET,
        Operation.ROUTE_GROUP: OpKind.ROUTE_GROUP,
        Operation.ROUTE_BOARD: OpKind.ROUTE_BOARD,
        Operation.OPTIMIZE_NET: OpKind.OPTIMIZE,
        Operation.REDUCE_VIAS: OpKind.OPTIMIZE,
        Operation.SET_NET_CONSTRAINT: OpKind.SET_CONSTRAINT,
        Operation.SET_ROUTING_PRIORITY: OpKind.SET_PRIORITY,
    }.get(op, OpKind.OTHER)


@dataclass(frozen=True, slots=True)
class FieldCapability:
    capability: Capability
    how: str  # what the application does with the value (shown to the user)


E, S, A, U = (
    Capability.ENFORCED,
    Capability.SOFT,
    Capability.ANALYSIS_ONLY,
    Capability.UNSUPPORTED,
)
_RN, _RG, _RB = OpKind.ROUTE_NET, OpKind.ROUTE_GROUP, OpKind.ROUTE_BOARD
_OP, _SC, _SP = OpKind.OPTIMIZE, OpKind.SET_CONSTRAINT, OpKind.SET_PRIORITY
_ROUTE = (_RN, _RG, _RB)

_NO_CLEARANCE = (
    "the router always uses the board's clearance rules; tighten a net's clearance "
    "with set_net_constraint or a KiCad net class"
)
_BOARD_WIDTH = (
    "a board-wide width would override every net class (power nets included); "
    "give widths per net with route_net or route_group"
)
_LENGTH = "the net's routed length is measured and reported; the router does not tune length"

#: field → {operation kind → capability}. Kinds not listed are UNSUPPORTED.
MATRIX: dict[str, dict[OpKind, FieldCapability]] = {
    "preferred_layers": {
        k: FieldCapability(S, "lower cost on these layers; other allowed layers stay usable")
        for k in _ROUTE
    },
    "forbidden_layers": {
        **{k: FieldCapability(E, "no new copper on these layers") for k in _ROUTE},
        _SC: FieldCapability(E, "stored as a rule override: no copper on these layers"),
    },
    "min_trace_width_mm": {
        _RN: FieldCapability(E, "routed at exactly this width unless a preferred width is given"),
        _RG: FieldCapability(E, "routed at exactly this width unless a preferred width is given"),
        _SC: FieldCapability(E, "stored as a rule override (may only tighten rules)"),
    },
    "preferred_trace_width_mm": {
        _RN: FieldCapability(E, "routed at exactly this width (below a rule minimum: rejected)"),
        _RG: FieldCapability(E, "routed at exactly this width (below a rule minimum: rejected)"),
        _SC: FieldCapability(E, "stored as a rule override (may only tighten rules)"),
    },
    "max_trace_width_mm": {
        k: FieldCapability(E, "checked: the requested width must not exceed it")
        for k in (_RN, _RG, _SC)
    },
    "min_clearance_mm": {
        _SC: FieldCapability(E, "stored as a clearance rule override (may only tighten)"),
        _OP: FieldCapability(S, "optimisation goal: more clearance where possible"),
    },
    "max_vias": {
        **{k: FieldCapability(E, "hard limit on vias per net") for k in _ROUTE},
        _SC: FieldCapability(E, "stored as a rule override: hard via limit"),
    },
    "minimize_vias": {
        **{k: FieldCapability(S, "vias cost 4× more in the search") for k in _ROUTE},
        _OP: FieldCapability(S, "optimisation goal: fewer vias"),
    },
    "preferred_via_type": {
        k: FieldCapability(E, "the router only places through vias") for k in (*_ROUTE, _SC)
    },
    "preserve_existing_routes": {
        _RN: FieldCapability(E, "single-net routing only adds copper; no route is removed"),
        _RG: FieldCapability(E, "routes that exist before the job are never moved"),
        _RB: FieldCapability(E, "routes that exist before the job are never moved"),
        _OP: FieldCapability(E, "only the target nets' own routes are replaced"),
        _SC: FieldCapability(E, "constraint operations never change copper"),
        _SP: FieldCapability(E, "constraint operations never change copper"),
    },
    "allow_ripup": {
        _RN: FieldCapability(E, "single-net routing never rips up"),
        _RG: FieldCapability(E, "rip-up on/off for the job (Speed mode never rips up)"),
        _RB: FieldCapability(E, "rip-up on/off for the job (Speed mode never rips up)"),
        _OP: FieldCapability(E, "optimisation never rips up other nets"),
        _SC: FieldCapability(E, "constraint operations never change copper"),
        _SP: FieldCapability(E, "constraint operations never change copper"),
    },
    "allow_component_movement": {
        k: FieldCapability(E, "the router never moves components") for k in (*_ROUTE, _OP, _SC, _SP)
    },
    "priority": {
        _RN: FieldCapability(A, "shown only: a single net has no routing order"),
        _RG: FieldCapability(S, "higher priority nets are routed earlier"),
        _RB: FieldCapability(S, "higher priority nets are routed earlier"),
        _SP: FieldCapability(S, "orders these nets earlier in later AI board-routing jobs"),
    },
    "criticality": {
        _RN: FieldCapability(A, "shown only: a single net has no routing order"),
        _RG: FieldCapability(S, "more critical nets are routed earlier"),
        _RB: FieldCapability(S, "more critical nets are routed earlier"),
        _SP: FieldCapability(S, "orders these nets earlier in later AI board-routing jobs"),
    },
    "max_length_mm": {k: FieldCapability(A, _LENGTH) for k in (*_ROUTE, _OP)},
    "target_length_mm": {k: FieldCapability(A, _LENGTH) for k in (*_ROUTE, _OP)},
    "length_tolerance_mm": {k: FieldCapability(A, _LENGTH) for k in (*_ROUTE, _OP)},
    "avoid_nets": {},
    "avoid_net_classes": {},
    "keep_near": {},
    "keep_away_from": {},
    "differential_pair": {
        k: FieldCapability(
            S,
            "the two nets are routed one after the other and the second follows a soft "
            "corridor along the first; gap, skew and impedance are not controlled",
        )
        for k in (_RG, _RB)
    },
    "pair_gap_mm": {
        k: FieldCapability(A, "the pair's gap is measured and reported") for k in (_RG, _RB)
    },
    "pair_skew_tolerance_mm": {
        k: FieldCapability(A, "the pair's length skew is measured and reported") for k in (_RG, _RB)
    },
    "impedance_target_ohm": {},
    "shielding_preference": {
        k: FieldCapability(E, "no shielding is added") for k in (*_ROUTE, _OP, _SC, _SP)
    },
    "additional_notes": {
        k: FieldCapability(A, "shown to you; never interpreted by the application") for k in OpKind
    },
}

_WHY_UNSUPPORTED: dict[str, str] = {
    "preferred_layers": "layer preferences only apply when routing",
    "min_trace_width_mm": _BOARD_WIDTH,
    "preferred_trace_width_mm": _BOARD_WIDTH,
    "max_trace_width_mm": _BOARD_WIDTH,
    "min_clearance_mm": _NO_CLEARANCE,
    "avoid_nets": "the router has no net-to-net avoidance",
    "avoid_net_classes": "the router has no net-class avoidance",
    "keep_near": "the router has no attraction to other nets or parts",
    "keep_away_from": "the router has no repulsion from other nets or parts",
    "impedance_target_ohm": "impedance needs stackup data and field solving; not implemented",
    "differential_pair": "a pair needs both nets in one route_group or route_board job",
    "pair_gap_mm": "pair metrics are only reported for route_group / route_board",
    "pair_skew_tolerance_mm": "pair metrics are only reported for route_group / route_board",
}


def capability(field: str, op: Operation, c: RoutingConstraints) -> FieldCapability:
    """The class of one *specified* field for ``op`` (value-aware where it matters)."""
    kind = op_kind(op)
    value = getattr(c, field)
    entry = MATRIX[field].get(kind)
    unsupported = FieldCapability(U, _WHY_UNSUPPORTED.get(field, f"it has no effect on {op.value}"))
    if entry is None:
        return unsupported
    if field == "preferred_via_type" and value != "through":
        return FieldCapability(U, f"{value} vias are not implemented (through vias only)")
    if field == "allow_component_movement" and value is True:
        return FieldCapability(U, "component movement is not implemented")
    if field == "shielding_preference" and value != "none":
        return FieldCapability(U, "guard traces and ground references are not implemented")
    if field == "allow_ripup" and value is True and kind not in (_RG, _RB):
        return FieldCapability(
            U, f"{op.value} never rips up other routes; use route_group or route_board"
        )
    if field == "preserve_existing_routes" and value is False and kind in (_SC, _SP):
        return FieldCapability(U, "constraint operations never change copper")
    if field == "preserve_existing_routes" and kind is OpKind.OPTIMIZE and value is True:
        return FieldCapability(U, "optimisation replaces the target nets' own routes")
    if (
        field == "max_trace_width_mm"
        and c.min_trace_width_mm is None
        and c.preferred_trace_width_mm is None
    ):
        return FieldCapability(
            U, "a maximum alone is not applied: also give a preferred or minimum width"
        )
    if (
        field == "min_clearance_mm"
        and kind is OpKind.OPTIMIZE
        and (op is Operation.REDUCE_VIAS or c.minimize_vias)
    ):
        return FieldCapability(U, "the via-reduction goal takes precedence; ignored")
    return entry


def assess(op: Operation, c: RoutingConstraints) -> list[tuple[str, FieldCapability]]:
    """Every field the command specifies, with its class, in schema order."""
    return [(name, capability(name, op, c)) for name in c.specified_fields()]


def unsupported(op: Operation, c: RoutingConstraints) -> list[tuple[str, FieldCapability]]:
    return [(n, fc) for n, fc in assess(op, c) if fc.capability is Capability.UNSUPPORTED]
