"""Routing support (Stage 3: legality, occupancy, congestion — no path search).

The router itself arrives in Stage 4; it will generate
:class:`~pcbrouter.routing.proposal.RouteProposal` objects and ask
:class:`~pcbrouter.routing.validator.RouteValidator` whether they are legal.
"""

from __future__ import annotations

from pcbrouter.routing.collision import (
    Collision,
    CollisionEngine,
    CollisionResult,
    ValidationStatus,
    ViolationType,
)
from pcbrouter.routing.proposal import ProposalSource, RouteProposal, RouteSegment, RouteVia
from pcbrouter.routing.validator import RouteValidationResult, RouteValidator

__all__ = [
    "Collision",
    "CollisionEngine",
    "CollisionResult",
    "ProposalSource",
    "RouteProposal",
    "RouteSegment",
    "RouteValidationResult",
    "RouteValidator",
    "RouteVia",
    "ValidationStatus",
    "ViolationType",
]
