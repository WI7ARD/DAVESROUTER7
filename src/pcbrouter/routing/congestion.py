"""Geometric congestion estimate per copper layer.

**This is a geometric congestion estimate, not a guarantee of routing difficulty.**
Each tile blends three normalised measures:

* blocked area — share of in-board cells an occupancy map at the reference width
  marks as not free (weight 0.6);
* pin density — pads with copper on the layer per tile (weight 0.25);
* track density — existing track length per tile (weight 0.15).

0.0 = open, 1.0 = severely congested. Levels: Low < 0.33 <= Medium < 0.66 <= High.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from pcbrouter.domain.geometry import Point
from pcbrouter.domain.units import Nm
from pcbrouter.geometry.board import BoardGeometry, ItemKind
from pcbrouter.geometry.distance import SegmentCore
from pcbrouter.routing.occupancy import CellState, build_occupancy
from pcbrouter.rules.resolver import RuleResolver

LABEL = "geometric congestion estimate"
WEIGHTS = (0.6, 0.25, 0.15)
DEFAULT_TILE_NM: Nm = 2_000_000
#: Reference track width when no rule states one (documented, used only here).
FALLBACK_REFERENCE_WIDTH_NM: Nm = 200_000


@dataclass
class CongestionMap:
    layer: str
    origin_x: Nm
    origin_y: Nm
    tile: Nm
    values: npt.NDArray[np.float32]  # [row, col] in 0..1
    reference_width: Nm
    reference_width_source: str
    elapsed_s: float = 0.0
    label: str = LABEL

    def value_at(self, p: Point) -> float | None:
        col = (p.x - self.origin_x) // self.tile
        row = (p.y - self.origin_y) // self.tile
        if 0 <= row < self.values.shape[0] and 0 <= col < self.values.shape[1]:
            return float(self.values[row, col])
        return None

    def level_at(self, p: Point) -> str:
        return level(self.value_at(p))

    @property
    def mean(self) -> float:
        return float(self.values.mean()) if self.values.size else 0.0


def level(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 0.33:
        return "Low"
    if value < 0.66:
        return "Medium"
    return "High"


def build_congestion(
    geo: BoardGeometry, resolver: RuleResolver, layer: str, tile: Nm = DEFAULT_TILE_NM
) -> CongestionMap:
    t0 = time.perf_counter()
    width_rule = resolver.resolve_trace_width(None, layer)
    width = width_rule.value or FALLBACK_REFERENCE_WIDTH_NM
    source = width_rule.source.describe() if width_rule.value else "fallback 0.2 mm (no rule)"
    cell = max(50_000, tile // 20)
    occ = build_occupancy(geo, resolver, layer, None, width, cell)
    spec = occ.spec
    per = max(1, tile // cell)  # cells per tile side
    nty = math.ceil(spec.ny / per)
    ntx = math.ceil(spec.nx / per)
    pad = np.full((nty * per, ntx * per), CellState.OUTSIDE_BOARD, dtype=np.uint8)
    pad[: spec.ny, : spec.nx] = occ.cells
    blocks = pad.reshape(nty, per, ntx, per)
    inside = (blocks != CellState.OUTSIDE_BOARD).sum(axis=(1, 3))
    blocked = ((blocks != CellState.FREE) & (blocks != CellState.OUTSIDE_BOARD)).sum(axis=(1, 3))
    blocked_frac = np.divide(blocked, inside, out=np.zeros(blocked.shape), where=inside > 0)

    tile_nm = per * cell
    pins = np.zeros((nty, ntx))
    tracks = np.zeros((nty, ntx))
    for item in geo.copper_near(layer, spec.bounds):
        c = item.bounds.center
        row = (c.y - spec.origin_y) // tile_nm
        col = (c.x - spec.origin_x) // tile_nm
        if not (0 <= row < nty and 0 <= col < ntx):
            continue
        if item.kind is ItemKind.PAD:
            pins[row, col] += 1
        elif item.kind is ItemKind.TRACK:
            tracks[row, col] += sum(
                s.core.a.distance_to(s.core.b)
                for s in item.shapes
                if isinstance(s.core, SegmentCore)
            )
    pin_norm = pins / pins.max() if pins.max() > 0 else pins
    track_norm = np.clip(tracks / (2.0 * tile_nm), 0.0, 1.0)
    w_b, w_p, w_t = WEIGHTS
    values = np.clip(w_b * blocked_frac + w_p * pin_norm + w_t * track_norm, 0.0, 1.0)
    values[inside == 0] = 0.0
    return CongestionMap(
        layer, spec.origin_x, spec.origin_y, tile_nm, values.astype(np.float32), width,
        source, time.perf_counter() - t0,
    )  # fmt: skip
