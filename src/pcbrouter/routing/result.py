"""Router results, scores and failure explanations."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pcbrouter.domain.units import Nm, format_mm
from pcbrouter.routing.proposal import RouteProposal
from pcbrouter.routing.validator import RouteValidationResult


class RouteStatus(Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"  # some connections of the net routed, others not
    NO_ROUTE = "NO_ROUTE"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    INVALID_REQUEST = "INVALID_REQUEST"
    RULE_UNKNOWN = "RULE_UNKNOWN"
    ALREADY_CONNECTED = "ALREADY_CONNECTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class FailureReason(Enum):
    NO_ESCAPE = "NO_ESCAPE"  # the source copper has no legal cell to leave from
    NO_PATH = "NO_PATH"
    VIA_LIMIT = "VIA_LIMIT"  # a route exists only with more vias than allowed
    LAYER_RESTRICTION = "LAYER_RESTRICTION"
    CONGESTION = "CONGESTION"  # blocked by other (router-generated) routes
    RULE_UNKNOWN = "RULE_UNKNOWN"
    TIMEOUT = "TIMEOUT"
    VALIDATION = "VALIDATION"  # search path kept failing the exact validator


@dataclass(frozen=True, slots=True)
class RouteScore:
    """The router's optimisation metric — not a claim of electrical quality."""

    length_nm: float
    vias: int
    bends: int
    layer_changes: int
    min_clearance_margin_nm: float | None  # observed gap minus required (None = n/a)
    congestion_exposure: float  # mean congestion (0..1) along the route, 0 if unknown
    cost: float

    def describe(self) -> str:
        margin = (
            "n/a"
            if self.min_clearance_margin_nm is None
            else format_mm(round(self.min_clearance_margin_nm))
        )
        return (
            f"length {format_mm(round(self.length_nm))}, {self.vias} via(s), "
            f"{self.bends} bend(s), clearance margin {margin}, cost {self.cost / 1e6:.2f}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "length_mm": round(self.length_nm / 1e6, 4),
            "vias": self.vias,
            "bends": self.bends,
            "layer_changes": self.layer_changes,
            "min_clearance_margin_mm": (
                None
                if self.min_clearance_margin_nm is None
                else round(self.min_clearance_margin_nm / 1e6, 4)
            ),
            "congestion_exposure": round(self.congestion_exposure, 3),
            "cost": round(self.cost, 1),
        }


@dataclass
class RouteCandidate:
    label: str  # "Best", "Alternative 1", ...
    proposal: RouteProposal
    validation: RouteValidationResult
    score: RouteScore
    connections: int = 1
    warnings: list[str] = field(default_factory=list)

    @property
    def legal(self) -> bool:
        return self.validation.legal


@dataclass
class RouteMetrics:
    expanded_nodes: int = 0
    searches: int = 0
    repairs: int = 0
    elapsed_s: float = 0.0
    grid_cells: int = 0
    backend: str = "cpu"

    def to_dict(self) -> dict[str, Any]:
        return {
            "expanded_nodes": self.expanded_nodes,
            "searches": self.searches,
            "repairs": self.repairs,
            "elapsed_s": round(self.elapsed_s, 3),
            "grid_cells": self.grid_cells,
            "backend": self.backend,
        }


@dataclass
class RouteResult:
    request_id: str
    net: str
    status: RouteStatus
    candidates: list[RouteCandidate] = field(default_factory=list)
    reason: FailureReason | None = None
    message: str = ""
    details: list[str] = field(default_factory=list)
    metrics: RouteMetrics = field(default_factory=RouteMetrics)
    width: Nm | None = None
    connections_total: int = 0
    connections_routed: int = 0
    #: approximate blocking statistics for failure explanations
    blockers: dict[str, int] = field(default_factory=dict)

    @property
    def best(self) -> RouteCandidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def success(self) -> bool:
        return self.status in (RouteStatus.SUCCESS, RouteStatus.PARTIAL) and bool(self.candidates)

    def summary(self) -> str:
        head = f"ROUTER RESULT {self.status.value}: {self.net}"
        if self.reason is not None:
            head += f" ({self.reason.value})"
        if self.best is not None:
            head += f" — {self.best.score.describe()}"
        if self.message:
            head += f" — {self.message}"
        return head

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "net": self.net,
            "status": self.status.value,
            "reason": self.reason.value if self.reason else None,
            "message": self.message,
            "details": list(self.details),
            "connections": {"total": self.connections_total, "routed": self.connections_routed},
            "candidates": [
                {
                    "label": c.label,
                    "score": c.score.to_dict(),
                    "validation": c.validation.status.value,
                    "segments": len(c.proposal.segments),
                    "vias": len(c.proposal.vias),
                }
                for c in self.candidates
            ],
            "metrics": self.metrics.to_dict(),
            "blockers": dict(self.blockers),
        }
