"""Occupancy raster: same-net tie-breaking and hard-state precedence."""

from __future__ import annotations

import numpy as np

from pcbrouter.domain.geometry import Point
from pcbrouter.domain.units import mm_to_internal
from pcbrouter.geometry.shapes import rectangle
from pcbrouter.routing.occupancy import CellState, GridSpec, _Rasterizer

MM = mm_to_internal


def _raster() -> tuple[_Rasterizer, np.ndarray]:
    spec = GridSpec(0, 0, MM(0.1), 20, 20, "F.Cu")
    cells = np.zeros((20, 20), dtype=np.uint8)
    return _Rasterizer(spec, cells), cells


def test_same_net_wins_foreign_ties() -> None:
    """Own copper stays passable inside a foreign clearance band (escape)."""
    rast, cells = _raster()
    rast.mark(
        rectangle(Point(MM(1.0), MM(1.0)), MM(1.0), MM(1.0)),
        MM(0.3),
        CellState.FOREIGN_NET,
    )
    assert cells[10, 10] == CellState.FOREIGN_NET
    rast.mark(
        rectangle(Point(MM(1.0), MM(1.0)), MM(0.4), MM(0.4)),
        MM(0.125),
        CellState.SAME_NET,
        overwrite_below=CellState.FOREIGN_NET,
    )
    assert cells[10, 10] == CellState.SAME_NET


def test_harder_states_survive_same_net() -> None:
    rast, cells = _raster()
    rast.mark(
        rectangle(Point(MM(1.0), MM(1.0)), MM(1.0), MM(1.0)),
        MM(0.3),
        CellState.BLOCKED,
    )
    rast.mark(
        rectangle(Point(MM(1.0), MM(1.0)), MM(0.4), MM(0.4)),
        MM(0.125),
        CellState.SAME_NET,
        overwrite_below=CellState.FOREIGN_NET,
    )
    assert cells[10, 10] == CellState.BLOCKED


def test_plain_maximum_still_applies_without_overwrite() -> None:
    rast, cells = _raster()
    rast.mark(
        rectangle(Point(MM(1.0), MM(1.0)), MM(0.4), MM(0.4)),
        MM(0.125),
        CellState.SAME_NET,
    )
    rast.mark(
        rectangle(Point(MM(1.0), MM(1.0)), MM(1.0), MM(1.0)),
        MM(0.3),
        CellState.FOREIGN_NET,
    )
    assert cells[10, 10] == CellState.FOREIGN_NET
