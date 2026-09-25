"""Typed routing requests (Stage 4 spec 4.2) and their normalisation.

A request says *what* to route; :func:`normalise` resolves every value through the
Stage 3 rule engine before any search starts. Values a request may tighten
(width up, fewer layers, fewer vias) are applied; values that would weaken a rule
make the request INVALID. Nothing here is guessed: an unknown width or via size
leaves the request unroutable (RULE_UNKNOWN) or vias disabled, and says so.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum

from pcbrouter.board_engine import BoardEngine
from pcbrouter.domain.geometry import BoundingBox
from pcbrouter.domain.units import Nm, format_mm
from pcbrouter.routing.cost.model import DEFAULT_COST_MODEL, CostModel

DEFAULT_GRID_NM: Nm = 100_000
DEFAULT_TIME_LIMIT_S = 30.0
DEFAULT_NODE_LIMIT = 1_500_000
MAX_CANDIDATES = 5


class RoutingStyle(Enum):
    OCTILINEAR = "45"  # horizontal, vertical and 45-degree segments
    ORTHOGONAL = "90"  # horizontal and vertical only


class SoftRegionKind(Enum):
    PREFER = "prefer"
    AVOID = "avoid"


@dataclass(frozen=True, slots=True)
class SoftRegion:
    """A user corridor: a cost field, never a hard keepout."""

    kind: SoftRegionKind
    box: BoundingBox
    layers: tuple[str, ...] = ()  # empty = all layers


class RouteRequestError(ValueError):
    """The request is invalid (bad net/layers, or it would weaken a rule)."""


class RuleUnknownError(ValueError):
    """A value needed to route is not stated by any rule (conservative refusal)."""


def new_request_id() -> str:
    return f"req-{uuid.uuid4().hex[:10]}"


@dataclass(frozen=True, slots=True)
class RouteRequest:
    net: str
    request_id: str = field(default_factory=new_request_id)
    #: geometry uids; None = automatic (largest pad group / all other groups)
    source_group: tuple[str, ...] | None = None
    target_group: tuple[str, ...] | None = None
    preferred_width: Nm | None = None
    allowed_layers: tuple[str, ...] | None = None
    preferred_layers: tuple[str, ...] = ()
    forbidden_layers: tuple[str, ...] = ()
    max_vias: int | None = None
    minimize_vias: bool = False
    preserve_existing_routes: bool = True
    allow_ripup: bool = False
    grid_resolution: Nm = DEFAULT_GRID_NM
    routing_style: RoutingStyle = RoutingStyle.OCTILINEAR
    time_limit_s: float = DEFAULT_TIME_LIMIT_S
    node_limit: int = DEFAULT_NODE_LIMIT
    candidates: int = 3
    seed: int = 0
    cost: CostModel = DEFAULT_COST_MODEL
    soft_regions: tuple[SoftRegion, ...] = ()
    via_diameter: Nm | None = None
    via_drill: Nm | None = None
    #: hard: new copper may not enter (user region locks); never board keepouts
    blocked_regions: tuple[BoundingBox, ...] = ()
    #: extra per-cell congestion (Stage 5 feedback), keyed by layer; values 0..1
    constraints_note: str = ""


@dataclass(frozen=True, slots=True)
class NormalisedRequest:
    request: RouteRequest
    net: str
    width: Nm
    width_source: str
    layers: tuple[str, ...]  # stack order
    preferred_layers: frozenset[str]
    via_diameter: Nm | None  # None = vias unavailable
    via_drill: Nm | None
    via_source: str
    max_vias: int | None
    notes: tuple[str, ...] = ()

    @property
    def vias_allowed(self) -> bool:
        return (
            self.via_diameter is not None
            and self.via_drill is not None
            and len(self.layers) > 1
            and self.max_vias != 0
        )


def with_user_constraints(
    request: RouteRequest,
    constraints: dict[str, object] | None,
    locked_regions: list[BoundingBox] | tuple[BoundingBox, ...] = (),
    corridors: list[SoftRegion] | tuple[SoftRegion, ...] = (),
) -> RouteRequest:
    """Apply the user's per-net constraints, region locks and corridors (Stage 8).
    Values are still validated by :func:`normalise` against the hard rules."""
    from dataclasses import replace

    from pcbrouter.domain.units import mm_to_internal

    c = constraints or {}
    width = c.get("width_mm")
    layers = c.get("allowed_layers")
    max_vias = c.get("max_vias")
    regions = list(request.soft_regions) + list(corridors)
    for key, kind in (("prefer_box", SoftRegionKind.PREFER), ("avoid_box", SoftRegionKind.AVOID)):
        box = c.get(key)
        if isinstance(box, (list, tuple)) and len(box) == 4:
            regions.append(SoftRegion(kind, BoundingBox(*(mm_to_internal(float(v)) for v in box))))
    return replace(
        request,
        preferred_width=(
            mm_to_internal(float(width))
            if isinstance(width, (int, float))
            else request.preferred_width
        ),
        allowed_layers=(
            tuple(layers)
            if isinstance(layers, (list, tuple)) and layers
            else request.allowed_layers
        ),
        max_vias=int(max_vias) if isinstance(max_vias, int) else request.max_vias,
        preserve_existing_routes=bool(c.get("preserve_existing", request.preserve_existing_routes)),
        soft_regions=tuple(regions),
        blocked_regions=tuple(request.blocked_regions) + tuple(locked_regions),
    )


def normalise(engine: BoardEngine, request: RouteRequest) -> NormalisedRequest:
    """Resolve a request against the rules. Raises RouteRequestError / RuleUnknownError."""
    geo = engine.geometry
    resolver = engine.resolver
    net = request.net
    if not net or net not in {n.name for n in engine.board.nets}:
        raise RouteRequestError(f"unknown net {net!r}")
    if not 1 <= request.candidates <= MAX_CANDIDATES:
        raise RouteRequestError(f"candidates must be 1..{MAX_CANDIDATES}")
    if request.grid_resolution < 10_000:
        raise RouteRequestError("grid resolution below 0.01 mm is not supported")
    if request.time_limit_s <= 0 or request.node_limit <= 0:
        raise RouteRequestError("time and node limits must be positive")
    notes: list[str] = []

    for layer in (*(request.allowed_layers or ()), *request.forbidden_layers):
        if not geo.is_copper_layer(layer):
            raise RouteRequestError(f"{layer} is not a copper layer of this board")
    rule_layers = resolver.resolve_allowed_layers(net)
    layers = [lay for lay in geo.copper_layers if lay in rule_layers.allowed]
    if request.allowed_layers is not None:
        layers = [lay for lay in layers if lay in request.allowed_layers]
    layers = [lay for lay in layers if lay not in request.forbidden_layers]
    if not layers:
        raise RouteRequestError(
            f"no copper layer is allowed for {net} (rules: {rule_layers.source.describe()})"
        )

    widths = [resolver.width_rules(net, lay) for lay in layers]
    preferred = [w.preferred for w in widths]
    minimum = [w.minimum for w in widths]
    maximum = [w.maximum for w in widths]
    if any(v.value is None for v in minimum) and request.preferred_width is None:
        raise RuleUnknownError(f"no rule states a track width for {net}")
    hard_min = max((v.value for v in minimum if v.value is not None), default=0)
    if request.preferred_width is not None:
        width = request.preferred_width
        source = "request"
        if width < hard_min:
            raise RouteRequestError(
                f"requested width {format_mm(width)} is below the {format_mm(hard_min)} "
                f"minimum for {net}"
            )
    else:
        known = [(v.value, v.source.describe()) for v in preferred if v.value is not None]
        if not known:
            raise RuleUnknownError(f"no rule states a preferred width for {net}")
        width, source = max(known)
        width = max(width, hard_min)
    for v in maximum:
        if v.value is not None and width > v.value:
            raise RouteRequestError(
                f"width {format_mm(width)} exceeds the {format_mm(v.value)} maximum "
                f"({v.source.describe()})"
            )

    via = resolver.resolve_via_rules(net)
    dia = request.via_diameter or via.diameter.value
    drill = request.via_drill or via.drill.value
    via_source = via.diameter.source.describe()
    if request.via_diameter is not None:
        via_source = "request"
    for value, rule, label in (
        (dia, via.min_diameter.value, "via diameter"),
        (drill, via.min_drill.value, "via drill"),
    ):
        if value is not None and rule is not None and value < rule:
            raise RouteRequestError(
                f"{label} {format_mm(value)} is below the {format_mm(rule)} minimum"
            )
    if dia is None or drill is None:
        notes.append("via size unknown (no rule): routing without vias")
        dia = drill = None
    elif dia <= drill:
        raise RouteRequestError("via diameter must exceed its drill")

    rule_vias = resolver.resolve_max_vias(net).value
    limits = [v for v in (rule_vias, request.max_vias) if v is not None]
    if request.max_vias is not None and request.max_vias < 0:
        raise RouteRequestError("max vias cannot be negative")
    max_vias = min(limits) if limits else None
    return NormalisedRequest(
        request=request,
        net=net,
        width=width,
        width_source=source,
        layers=tuple(layers),
        preferred_layers=frozenset(request.preferred_layers or layers),
        via_diameter=dia,
        via_drill=drill,
        via_source=via_source,
        max_vias=max_vias,
        notes=tuple(notes),
    )
