"""Hypothetical route geometry (``RouteProposal``) — candidate copper for testing.

A proposal is *not* board state: it is never added to the :class:`Board` or the
:class:`~pcbrouter.geometry.board.BoardGeometry`. Stage 3 only validates proposals;
Stage 4 will generate them and Stage 9 may one day apply accepted ones.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

from pcbrouter.domain.geometry import Point
from pcbrouter.domain.units import Nm


class ProposalSource(Enum):
    MANUAL_TEST = "manual_test"
    CPU_ROUTER = "cpu_router"
    GPU_ROUTER = "gpu_router"
    OPTIMIZER = "optimizer"
    USER_EDIT = "user_edit"


@dataclass(frozen=True, slots=True)
class RouteSegment:
    start: Point
    end: Point
    layer: str
    width: Nm
    #: Optional cost annotation from a router (not used by validation).
    source_cost: float | None = None

    @property
    def length(self) -> float:
        return self.start.distance_to(self.end)


@dataclass(frozen=True, slots=True)
class RouteVia:
    position: Point
    start_layer: str
    end_layer: str
    diameter: Nm
    drill: Nm


def new_proposal_id() -> str:
    return f"route-{uuid.uuid4().hex[:10]}"


@dataclass(frozen=True, slots=True)
class RouteProposal:
    net: str
    segments: tuple[RouteSegment, ...] = ()
    vias: tuple[RouteVia, ...] = ()
    source: ProposalSource = ProposalSource.MANUAL_TEST
    proposal_id: str = field(default_factory=new_proposal_id)
    metadata: MappingProxyType[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def width(self) -> Nm | None:
        """The common segment width, or ``None`` if segments differ."""
        widths = {s.width for s in self.segments}
        return widths.pop() if len(widths) == 1 else None

    @property
    def layers(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for s in self.segments:
            seen[s.layer] = None
        for v in self.vias:
            seen[v.start_layer] = None
            seen[v.end_layer] = None
        return tuple(seen)

    @property
    def length(self) -> float:
        return sum(s.length for s in self.segments)
