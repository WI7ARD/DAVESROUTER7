"""The fast polygon rasteriser must give exactly the cells of the reference path."""

from __future__ import annotations

import math
import random

import numpy as np

from pcbrouter.domain.geometry import Point
from pcbrouter.geometry.distance import PolygonCore
from pcbrouter.geometry.polygon import Polygon
from pcbrouter.geometry.raster import (
    centers,
    distance_field,
    polygon_inside,
    polygon_inside_grid,
    polygon_near_mask,
)


def _star(rng: random.Random, n: int) -> Polygon:
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        r = 1_000_000 + 2_500_000 * rng.random()
        pts.append(Point(int(5_000_000 + r * math.cos(a)), int(4_000_000 + r * math.sin(a))))
    return Polygon(pts)


def test_fast_polygon_raster_is_cell_exact() -> None:
    rng = random.Random(7)
    for _ in range(20):
        poly = _star(rng, rng.choice([70, 150, 400, 900]))
        cell = rng.choice([50_000, 100_000, 127_000])
        radius = rng.choice([0, 100_000])
        reach = rng.choice([0.0, 125_000.0, 333_333.0])
        box = poly.bounds.expanded(math.ceil(reach + radius) + cell)
        xc = centers(box.min_x, cell, box.width // cell + 1)
        yc = centers(box.min_y, cell, box.height // cell + 1)
        xs, ys = np.meshgrid(xc, yc)
        ref = (distance_field(PolygonCore(poly), xs, ys, box) - radius) <= reach
        assert (polygon_near_mask(xc, yc, poly, float(radius), reach) == ref).all()
        assert (polygon_inside_grid(xc, yc, poly) == polygon_inside(xs, ys, poly)).all()
