"""Structured PCB command schema (v2) — the only output an LLM may produce.

Pipeline::

    model text --command_parser--> PlannerResponse / AICommand  (syntax, this schema)
               --command_validator--> ValidationReport          (semantics vs. board)
               --> CommandProposal --> user approval --> history
               --> (Stage 4+) deterministic router

Design rules:

* ``extra="forbid"`` everywhere: unknown keys (e.g. ``"execute_shell"``) are errors.
* Every number is bounded; every list is length-limited; strings reject control chars.
* Lengths are explicit ``*_mm`` floats (humans and LLMs think in millimetres) and are
  converted to integer nanometres only via :mod:`pcbrouter.domain.units`.
* Optional fields default to ``None`` = *not specified by the model*. Safe defaults
  are applied by the ``effective_*`` properties, never by the model.
* No field can carry coordinates for copper, KiCad text or code. Commands express
  intent and constraints; the deterministic router (Stage 4+) decides the copper.

Stage 1's compact form (``{"operation": "route_net", "target": "GND"}``) is still
accepted by :func:`pcbrouter.ai.command_parser.parse_command_payload`, which converts it
to this schema.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

SCHEMA_VERSION = 2
MAX_COMMANDS_PER_RESPONSE = 10

_TEXT = r"^[^\x00-\x08\x0b-\x1f\x7f]*$"  # printable text (newline/tab allowed)
_NAME = r"^[^\x00-\x1f\x7f]+$"  # single-line identifier

EntityName = Annotated[str, StringConstraints(min_length=1, max_length=255, pattern=_NAME)]
LayerName = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=_NAME)]
ShortText = Annotated[str, StringConstraints(max_length=600, pattern=_TEXT)]
LongText = Annotated[str, StringConstraints(max_length=4000, pattern=_TEXT)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


# ======================================================================= enums
class Operation(StrEnum):
    ANALYZE_BOARD = "analyze_board"
    ANALYZE_NET = "analyze_net"
    ANALYZE_COMPONENT = "analyze_component"
    ROUTE_NET = "route_net"
    ROUTE_GROUP = "route_group"
    ROUTE_BOARD = "route_board"
    OPTIMIZE_NET = "optimize_net"
    REDUCE_VIAS = "reduce_vias"
    SET_NET_CONSTRAINT = "set_net_constraint"
    SET_ROUTING_PRIORITY = "set_routing_priority"
    LOCK_COMPONENT = "lock_component"
    LOCK_NET = "lock_net"
    LOCK_TRACK = "lock_track"
    PROTECT_AREA = "protect_area"
    EXPLAIN_ROUTE = "explain_route"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()

    @property
    def category(self) -> OperationCategory:
        return _CATEGORY[self]


class OperationCategory(StrEnum):
    READ_ONLY = "read_only"  # analysis/explanation; completes on approval
    CONSTRAINT = "constraint"  # changes the planning constraint set, not geometry
    ROUTING = "routing"  # would change copper: needs the Stage 4+ router


_CATEGORY = {
    Operation.ANALYZE_BOARD: OperationCategory.READ_ONLY,
    Operation.ANALYZE_NET: OperationCategory.READ_ONLY,
    Operation.ANALYZE_COMPONENT: OperationCategory.READ_ONLY,
    Operation.EXPLAIN_ROUTE: OperationCategory.READ_ONLY,
    Operation.SET_NET_CONSTRAINT: OperationCategory.CONSTRAINT,
    Operation.SET_ROUTING_PRIORITY: OperationCategory.CONSTRAINT,
    Operation.LOCK_COMPONENT: OperationCategory.CONSTRAINT,
    Operation.LOCK_NET: OperationCategory.CONSTRAINT,
    Operation.LOCK_TRACK: OperationCategory.CONSTRAINT,
    Operation.PROTECT_AREA: OperationCategory.CONSTRAINT,
    Operation.ROUTE_NET: OperationCategory.ROUTING,
    Operation.ROUTE_GROUP: OperationCategory.ROUTING,
    Operation.ROUTE_BOARD: OperationCategory.ROUTING,
    Operation.OPTIMIZE_NET: OperationCategory.ROUTING,
    Operation.REDUCE_VIAS: OperationCategory.ROUTING,
}

Priority = Literal["low", "normal", "high", "critical"]
Criticality = Literal["low", "normal", "high", "safety_critical"]
ViaType = Literal["through", "blind_buried", "micro"]
Shielding = Literal["none", "prefer_ground_reference", "prefer_guard_traces"]
Confidence = Literal["low", "medium", "high"]


# ======================================================================= targets
class NetTarget(_Strict):
    type: Literal["net"]
    name: EntityName


class NetGroupTarget(_Strict):
    type: Literal["net_group"]
    names: list[EntityName] = Field(min_length=2, max_length=64)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.names)) != len(self.names):
            raise ValueError("net_group contains duplicate net names")
        return self


class ComponentTarget(_Strict):
    type: Literal["component"]
    reference: EntityName


class AreaTarget(_Strict):
    """Axis-aligned rectangle in board millimetres (KiCad coordinates, Y down)."""

    type: Literal["area"]
    x_min_mm: float = Field(ge=-10_000, le=10_000)
    y_min_mm: float = Field(ge=-10_000, le=10_000)
    x_max_mm: float = Field(ge=-10_000, le=10_000)
    y_max_mm: float = Field(ge=-10_000, le=10_000)
    layers: list[LayerName] | None = Field(default=None, max_length=32)  # None = all copper

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.x_min_mm >= self.x_max_mm or self.y_min_mm >= self.y_max_mm:
            raise ValueError("area min must be strictly less than max on both axes")
        return self


class BoardTarget(_Strict):
    type: Literal["board"]


Target = Annotated[
    NetTarget | NetGroupTarget | ComponentTarget | AreaTarget | BoardTarget,
    Field(discriminator="type"),
]


def target_key(
    target: NetTarget | NetGroupTarget | ComponentTarget | AreaTarget | BoardTarget,
) -> str:
    match target:
        case NetTarget(name=n):
            return f"net:{n}"
        case NetGroupTarget(names=ns):
            return "net_group:" + ",".join(sorted(ns))
        case ComponentTarget(reference=r):
            return f"component:{r}"
        case AreaTarget():
            return f"area:{target.x_min_mm},{target.y_min_mm},{target.x_max_mm},{target.y_max_mm}"
        case BoardTarget():
            return "board"
    raise AssertionError("unreachable")  # pragma: no cover


# ======================================================================= constraints
class EntityRef(_Strict):
    kind: Literal["net", "component"]
    name: EntityName


class DifferentialPairSpec(_Strict):
    positive_net: EntityName
    negative_net: EntityName

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if self.positive_net == self.negative_net:
            raise ValueError("differential pair nets must be different")
        return self


def _nonneg(le: float) -> float | None:
    result: float | None = Field(default=None, gt=0.0, le=le)
    return result


class RoutingConstraints(_Strict):
    """Explicit, typed routing constraints. ``None`` = not specified."""

    preferred_layers: list[LayerName] | None = Field(default=None, max_length=32)
    forbidden_layers: list[LayerName] | None = Field(default=None, max_length=32)

    min_trace_width_mm: float | None = _nonneg(20.0)
    preferred_trace_width_mm: float | None = _nonneg(20.0)
    max_trace_width_mm: float | None = _nonneg(20.0)
    min_clearance_mm: float | None = _nonneg(20.0)

    max_vias: int | None = Field(default=None, ge=0, le=256)
    minimize_vias: bool | None = None
    preferred_via_type: ViaType | None = None

    preserve_existing_routes: bool | None = None
    allow_ripup: bool | None = None
    allow_component_movement: bool | None = None

    priority: Priority | None = None
    criticality: Criticality | None = None

    max_length_mm: float | None = _nonneg(10_000.0)
    target_length_mm: float | None = _nonneg(10_000.0)
    length_tolerance_mm: float | None = Field(default=None, ge=0.0, le=1_000.0)

    avoid_nets: list[EntityName] | None = Field(default=None, max_length=256)
    avoid_net_classes: list[EntityName] | None = Field(default=None, max_length=64)
    keep_near: list[EntityRef] | None = Field(default=None, max_length=64)
    keep_away_from: list[EntityRef] | None = Field(default=None, max_length=64)

    differential_pair: DifferentialPairSpec | None = None
    pair_gap_mm: float | None = _nonneg(20.0)
    pair_skew_tolerance_mm: float | None = Field(default=None, ge=0.0, le=100.0)
    impedance_target_ohm: float | None = Field(default=None, gt=0.0, le=1_000.0)
    shielding_preference: Shielding | None = None

    #: Informational only; never interpreted as an instruction by the application.
    additional_notes: ShortText | None = None

    @model_validator(mode="after")
    def _lists_unique(self) -> Self:
        for name in ("preferred_layers", "forbidden_layers", "avoid_nets", "avoid_net_classes"):
            values = getattr(self, name)
            if values is not None and len(set(values)) != len(values):
                raise ValueError(f"{name} contains duplicates")
        return self

    # -- safe defaults (applied by the application, never assumed from the model)
    @property
    def effective_preserve_existing_routes(self) -> bool:
        return self.preserve_existing_routes is not False

    @property
    def effective_allow_ripup(self) -> bool:
        return self.allow_ripup is True

    @property
    def effective_allow_component_movement(self) -> bool:
        return self.allow_component_movement is True

    def specified_fields(self) -> dict[str, object]:
        return self.model_dump(exclude_none=True)

    def is_empty(self) -> bool:
        return not self.specified_fields()


# ======================================================================= commands
class AICommand(_Strict):
    """One structured intent proposed by the planner (the command envelope)."""

    operation: Operation
    targets: list[Target] = Field(default_factory=list, max_length=64)
    constraints: RoutingConstraints | None = None
    #: A brief engineering justification — not hidden chain-of-thought.
    reasoning_summary: ShortText | None = None
    warnings: list[ShortText] | None = Field(default=None, max_length=20)
    confidence: Confidence | None = None
    requires_user_confirmation: bool | None = None

    @model_validator(mode="after")
    def _unique_targets(self) -> Self:
        keys = [target_key(t) for t in self.targets]
        if len(set(keys)) != len(keys):
            raise ValueError("targets contain duplicates")
        return self

    @property
    def effective_constraints(self) -> RoutingConstraints:
        return self.constraints or RoutingConstraints()


class AnalysisPriority(_Strict):
    item: ShortText
    rationale: ShortText | None = None


class AIAnalysis(_Strict):
    """ANALYZE/EXPLAIN output. These are AI *observations*, not verified facts."""

    summary: LongText
    observations: list[ShortText] | None = Field(default=None, max_length=30)
    potential_issues: list[ShortText] | None = Field(default=None, max_length=30)
    recommended_priorities: list[AnalysisPriority] | None = Field(default=None, max_length=30)
    unknowns: list[ShortText] | None = Field(default=None, max_length=30)
    warnings: list[ShortText] | None = Field(default=None, max_length=30)


class PlannerResponse(_Strict):
    """Top-level JSON object every model response must be."""

    schema_version: Literal[2]
    mode: Literal["analyze", "plan", "command", "explain"]
    message: LongText
    analysis: AIAnalysis | None = None
    plan_steps: list[ShortText] | None = Field(default=None, max_length=30)
    commands: list[AICommand] | None = Field(default=None, max_length=MAX_COMMANDS_PER_RESPONSE)
    clarification_needed: ShortText | None = None
    unsupported_request: ShortText | None = None
