"""Structured PCB command schema — the *only* thing an LLM is allowed to produce.

Pipeline (future stages)::

    LLM text --parse_command()--> PCBCommand   (syntactic validation, this module)
             --validate_against_board()-->     (semantic validation vs. the Board)
             --> CommandBus --> deterministic planner/router --> DRC --> preview
             --> user accepts --> KiCad writer

The models are deliberately strict:

* ``extra="forbid"`` everywhere: unknown keys are errors, not silently ignored;
* every numeric value is bounded;
* lengths use explicit ``*_mm`` names (humans and LLMs think in mm) and are
  converted to internal nanometres only via :func:`pcbrouter.domain.units.mm_to_internal`;
* nothing in a command can express raw geometry edits. Commands describe *intent
  and constraints*; the deterministic router decides the copper.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from pcbrouter.domain.board import Board

SCHEMA_VERSION = 1
MAX_COMMAND_JSON_BYTES = 100_000

NetName = Annotated[
    str, StringConstraints(min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f]+$")
]
CopperLayerName = Annotated[str, StringConstraints(pattern=r"^(F|B|In[1-9][0-9]?)\.Cu$")]
Reference = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[^\s\x00-\x1f\x7f]+$")
]
ObjectId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Priority = Literal["low", "normal", "high", "critical"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _unique(values: list[str], field_name: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} contains duplicates")


class RoutingConstraints(_Strict):
    max_vias: int | None = Field(default=None, ge=0, le=64)
    preferred_layers: list[CopperLayerName] = Field(default_factory=list, max_length=32)
    avoid_layers: list[CopperLayerName] = Field(default_factory=list, max_length=32)
    move_components: bool = False
    preserve_existing_routes: bool = True
    track_width_mm: float | None = Field(default=None, gt=0.0, le=20.0)
    clearance_mm: float | None = Field(default=None, gt=0.0, le=20.0)
    max_length_mm: float | None = Field(default=None, gt=0.0, le=10_000.0)
    priority: Priority = "normal"

    @model_validator(mode="after")
    def _check_layers(self) -> Self:
        _unique(self.preferred_layers, "preferred_layers")
        _unique(self.avoid_layers, "avoid_layers")
        overlap = set(self.preferred_layers) & set(self.avoid_layers)
        if overlap:
            raise ValueError(f"layers both preferred and avoided: {sorted(overlap)}")
        return self


class AreaMM(_Strict):
    """Axis-aligned rectangle in board millimetres."""

    x_min_mm: float = Field(ge=-10_000, le=10_000)
    y_min_mm: float = Field(ge=-10_000, le=10_000)
    x_max_mm: float = Field(ge=-10_000, le=10_000)
    y_max_mm: float = Field(ge=-10_000, le=10_000)

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.x_min_mm >= self.x_max_mm or self.y_min_mm >= self.y_max_mm:
            raise ValueError("area min must be strictly less than max on both axes")
        return self


class AnalyzeBoard(_Strict):
    operation: Literal["analyze_board"]
    focus: Literal["overview", "nets", "components", "routing_status"] = "overview"


class RouteNet(_Strict):
    operation: Literal["route_net"]
    target: NetName
    constraints: RoutingConstraints = Field(default_factory=RoutingConstraints)


class RouteGroup(_Strict):
    operation: Literal["route_group"]
    targets: list[NetName] = Field(min_length=1, max_length=512)
    constraints: RoutingConstraints = Field(default_factory=RoutingConstraints)

    @model_validator(mode="after")
    def _check_targets(self) -> Self:
        _unique(self.targets, "targets")
        return self


class RouteBoard(_Strict):
    operation: Literal["route_board"]
    exclude_nets: list[NetName] = Field(default_factory=list, max_length=4096)
    constraints: RoutingConstraints = Field(default_factory=RoutingConstraints)


class SetConstraint(_Strict):
    operation: Literal["set_constraint"]
    target: NetName
    constraints: RoutingConstraints


class LockComponent(_Strict):
    operation: Literal["lock_component"]
    reference: Reference


class LockTrack(_Strict):
    operation: Literal["lock_track"]
    net: NetName | None = None
    track_ids: list[ObjectId] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def _check_target(self) -> Self:
        if self.net is None and not self.track_ids:
            raise ValueError("lock_track needs 'net' or at least one entry in 'track_ids'")
        return self


class ProtectArea(_Strict):
    operation: Literal["protect_area"]
    area: AreaMM
    layers: list[CopperLayerName] = Field(default_factory=list, max_length=32)  # [] = all
    reason: str = Field(default="", max_length=500)


class OptimizeRoute(_Strict):
    operation: Literal["optimize_route"]
    target: NetName | None = None  # None = every routed net
    goals: list[Literal["length", "vias", "corners"]] = Field(min_length=1, max_length=3)


class ReduceVias(_Strict):
    operation: Literal["reduce_vias"]
    target: NetName | None = None
    max_vias_per_net: int | None = Field(default=None, ge=0, le=64)


PCBCommand = Annotated[
    AnalyzeBoard
    | RouteNet
    | RouteGroup
    | RouteBoard
    | SetConstraint
    | LockComponent
    | LockTrack
    | ProtectArea
    | OptimizeRoute
    | ReduceVias,
    Field(discriminator="operation"),
]

_ADAPTER: TypeAdapter[PCBCommand] = TypeAdapter(PCBCommand)

#: Operations that would change board copper/placement if executed.
MODIFYING_OPERATIONS = frozenset(
    {"route_net", "route_group", "route_board", "optimize_route", "reduce_vias"}
)


class CommandValidationError(ValueError):
    """The input is not a valid PCB command. ``errors`` holds readable reasons."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def parse_command(data: str | bytes | Mapping[str, Any]) -> PCBCommand:
    """Parse and validate one command from JSON text or a mapping."""
    if isinstance(data, str | bytes):
        if len(data) > MAX_COMMAND_JSON_BYTES:
            raise CommandValidationError(["command JSON is too large"])
        try:
            obj = json.loads(data)
        except json.JSONDecodeError as exc:
            raise CommandValidationError([f"not valid JSON: {exc.msg}"]) from exc
    else:
        obj = dict(data)
    if not isinstance(obj, dict):
        raise CommandValidationError(["command must be a JSON object"])
    try:
        return _ADAPTER.validate_python(obj)
    except ValidationError as exc:
        raise CommandValidationError(
            [f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()]
        ) from exc


def command_json_schema() -> dict[str, Any]:
    """JSON Schema for all commands (future: sent to providers for structured output)."""
    return _ADAPTER.json_schema()


def validate_against_board(command: PCBCommand, board: Board) -> list[str]:
    """Semantic checks against a loaded board. Returns problems (empty = OK)."""
    problems: list[str] = []
    nets = board.index.nets_by_name
    copper = set(board.copper_layer_names)

    def check_net(name: str | None) -> None:
        if name is not None and name not in nets:
            problems.append(f"unknown net {name!r}")

    def check_layers(layers: list[str]) -> None:
        for layer in layers:
            if layer not in copper:
                problems.append(f"layer {layer!r} is not a copper layer of this board")

    constraints: RoutingConstraints | None = getattr(command, "constraints", None)
    if constraints is not None:
        check_layers(constraints.preferred_layers)
        check_layers(constraints.avoid_layers)

    match command:
        case RouteNet(target=t) | SetConstraint(target=t):
            check_net(t)
        case RouteGroup(targets=ts):
            for t in ts:
                check_net(t)
        case RouteBoard(exclude_nets=ex):
            for t in ex:
                check_net(t)
        case OptimizeRoute(target=t) | ReduceVias(target=t):
            check_net(t)
        case LockComponent(reference=ref):
            if ref not in board.index.components_by_ref:
                problems.append(f"unknown component {ref!r}")
        case LockTrack(net=n, track_ids=ids):
            check_net(n)
            for tid in ids:
                if tid not in board.index.tracks_by_id:
                    problems.append(f"unknown track id {tid!r}")
        case ProtectArea(layers=layers):
            check_layers(layers)
        case AnalyzeBoard():
            pass  # read-only analysis: nothing to cross-check against the board
    return problems
