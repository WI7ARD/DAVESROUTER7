"""Differential pairs (Stage 5 foundation).

What is implemented — *routing intent*, measured honestly:

* detection of pairs by name (``X_P/X_N``, ``X+/X-``, ``XP/XN``, ``X_DP/X_DN``);
* the second net of a pair is routed with a soft corridor along its partner's
  existing route (prefer factor inside boxes around the partner's segments, same
  layers preferred), so the two traces tend to run together;
* :func:`pair_metrics`: length skew, via-count difference, layers, and the sampled
  gap between the two routes (min/mean).

What is **not** claimed: controlled impedance. That needs stack-up, dielectric and
copper geometry this application does not model — impedance is reported as
``UNKNOWN``. True coupled (centre-line + offset) routing is future work.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from pcbrouter.domain.board import Board
from pcbrouter.domain.geometry import BoundingBox
from pcbrouter.domain.units import Nm
from pcbrouter.geometry.clearance import gap_between
from pcbrouter.geometry.shapes import capsule
from pcbrouter.routing.request import RouteRequest, SoftRegion, SoftRegionKind
from pcbrouter.routing.working_board import WorkingBoard

IMPEDANCE = "UNKNOWN"  # never computed without stack-up data
MAX_CORRIDOR_BOXES = 40


@dataclass(frozen=True, slots=True)
class PairMetrics:
    p: str
    n: str
    length_p_nm: float
    length_n_nm: float
    vias_p: int
    vias_n: int
    layers_p: tuple[str, ...]
    layers_n: tuple[str, ...]
    min_gap_nm: float | None
    mean_gap_nm: float | None
    impedance: str = IMPEDANCE

    @property
    def skew_nm(self) -> float:
        return abs(self.length_p_nm - self.length_n_nm)

    @property
    def via_difference(self) -> int:
        return abs(self.vias_p - self.vias_n)


def pair_request(wb: WorkingBoard, request: RouteRequest, partner: str) -> RouteRequest:
    """Bias ``request`` to run alongside the partner's existing route."""
    tracks = [t for t in wb.board.tracks if t.net_name == partner]
    if not tracks:
        return request
    engine = wb.engine
    width = engine.resolver.resolve_trace_width(request.net).value or 0
    gap = engine.resolver.resolve_clearance(request.net, partner).value or 0
    pitch: Nm = width + gap
    regions = list(request.soft_regions)
    for t in tracks[:MAX_CORRIDOR_BOXES]:
        box = BoundingBox.from_points([t.start, t.end])
        assert box is not None
        regions.append(SoftRegion(SoftRegionKind.PREFER, box.expanded(pitch + width), (t.layer,)))
    layers = tuple(dict.fromkeys(t.layer for t in tracks))
    return replace(request, soft_regions=tuple(regions), preferred_layers=layers)


def pair_metrics(board: Board, p: str, n: str, samples: int = 64) -> PairMetrics:
    tp = [t for t in board.tracks if t.net_name == p]
    tn = [t for t in board.tracks if t.net_name == n]
    shapes_n = [capsule(t.start, t.end, t.width // 2) for t in tn]
    gaps: list[float] = []
    for t in tp:
        steps = max(1, min(samples, int(t.length // 250_000)))
        for k in range(steps + 1):
            x = t.start.x + (t.end.x - t.start.x) * k // steps
            y = t.start.y + (t.end.y - t.start.y) * k // steps
            from pcbrouter.domain.geometry import Point

            probe = capsule(Point(x, y), Point(x, y), t.width // 2)
            same_layer = [s for s, tt in zip(shapes_n, tn, strict=True) if tt.layer == t.layer]
            if same_layer:
                gaps.append(min(gap_between(probe, s) for s in same_layer))
    return PairMetrics(
        p,
        n,
        sum(t.length for t in tp),
        sum(t.length for t in tn),
        sum(1 for v in board.vias if v.net_name == p),
        sum(1 for v in board.vias if v.net_name == n),
        tuple(dict.fromkeys(t.layer for t in tp)),
        tuple(dict.fromkeys(t.layer for t in tn)),
        min(gaps) if gaps else None,
        sum(gaps) / len(gaps) if gaps else None,
    )
