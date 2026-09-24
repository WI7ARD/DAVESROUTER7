"""The router's optimisation metric — explicit and centralised.

All costs are expressed as an *equivalent track length in nanometres* so the
components are comparable and independent of the grid resolution:

====================  ==========================================================
component             meaning (default)
====================  ==========================================================
distance              1 nm of track costs 1 (x ``nonpreferred_layer_factor``
                      = 1.25 on layers not in the request's preferred list)
via                   one via costs as much as ``via_nm`` = 2.5 mm of track
                      (x ``minimize_vias_multiplier`` = 4 when the request asks
                      to minimise vias)
bend                  a 45 degree turn costs ``bend45_nm`` = 0.15 mm, a 90
                      degree turn ``bend90_nm`` = 0.5 mm; sharper turns are not
                      generated at all (no acute angles)
clearance proximity   a step through a cell adjacent to an obstacle costs an
                      extra ``proximity_factor`` = 30 % of its length (prefers
                      routes with margin; never allows less than the rule)
congestion            extra ``congestion_weight`` x congestion(0..1) x step
soft regions          user corridors: ``prefer`` multiplies step cost by
                      ``corridor_prefer_factor`` (0.7), ``avoid`` adds
                      ``corridor_avoid_factor`` (2.0) x step
alternatives          cells used by an earlier candidate cost an extra
                      ``reuse_factor`` = 1.0 x step (diversity)
====================  ==========================================================

A lower cost means "better by this metric", not "electrically superior".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any


@dataclass(frozen=True, slots=True)
class CostModel:
    via_nm: float = 2_500_000.0
    bend45_nm: float = 150_000.0
    bend90_nm: float = 500_000.0
    proximity_factor: float = 0.30
    nonpreferred_layer_factor: float = 1.25
    minimize_vias_multiplier: float = 4.0
    congestion_weight: float = 1.0
    corridor_prefer_factor: float = 0.7
    corridor_avoid_factor: float = 2.0
    reuse_factor: float = 1.0

    def __post_init__(self) -> None:
        for f in fields(self):
            if getattr(self, f.name) < 0:
                raise ValueError(f"cost component {f.name} must be >= 0")
        if self.nonpreferred_layer_factor < 1.0:
            raise ValueError("nonpreferred_layer_factor must be >= 1 (keeps A* admissible)")
        if not 0 < self.corridor_prefer_factor <= 1.0:
            raise ValueError("corridor_prefer_factor must be in (0, 1]")

    def to_dict(self) -> dict[str, float]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CostModel:
        known = {f.name for f in fields(cls)}
        return cls(**{k: float(v) for k, v in data.items() if k in known})


DEFAULT_COST_MODEL = CostModel()
