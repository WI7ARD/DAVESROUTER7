"""``geometry_summary.json`` — developer export of the geometry model (spec §74).

Only produced by an explicit user action (Tools ▸ Export Geometry Summary). It lists
counts, bounds, layers, object IDs and shape *types* — not the shape coordinates,
net names or any other design content beyond object bounding boxes.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from pcbrouter import __version__
from pcbrouter.domain.geometry import BoundingBox
from pcbrouter.domain.units import internal_to_mm
from pcbrouter.geometry import GEOMETRY_ENGINE_VERSION
from pcbrouter.geometry.board import BoardGeometry

SUMMARY_FORMAT = "pcbrouter-geometry-summary/1"


def _box(b: BoundingBox | None) -> list[float] | None:
    if b is None:
        return None
    return [internal_to_mm(v) for v in (b.min_x, b.min_y, b.max_x, b.max_y)]


def geometry_summary(geo: BoardGeometry) -> dict[str, Any]:
    objects: list[dict[str, Any]] = []
    for item in sorted(geo.copper.values(), key=lambda i: i.uid):
        objects.append(
            {
                "id": item.uid,
                "kind": item.kind.value,
                "layers": sorted(item.layers),
                "shape_types": sorted(Counter(s.kind for s in item.shapes)),
                "accuracy": item.accuracy.value,
                "bounds_mm": _box(item.bounds),
            }
        )
    for hole in sorted(geo.holes.values(), key=lambda h: h.uid):
        objects.append(
            {
                "id": hole.uid,
                "kind": "hole" if hole.plated else "npth",
                "shape_types": [hole.shape.kind],
                "bounds_mm": _box(hole.bounds),
            }
        )
    for k in sorted(geo.keepouts.values(), key=lambda k: k.uid):
        objects.append(
            {
                "id": k.uid,
                "kind": "keepout",
                "layers": sorted(k.layers),
                "forbids": k.rules.describe(),
                "shape_types": [k.shape.kind],
                "bounds_mm": _box(k.bounds),
            }
        )
    return {
        "format": SUMMARY_FORMAT,
        "app_version": __version__,
        "geometry_engine_version": GEOMETRY_ENGINE_VERSION,
        "board_fingerprint": geo.fingerprint,
        "board_bounds_mm": _box(geo.board.bounds),
        "copper_layers": list(geo.copper_layers),
        "board_region": {
            "status": geo.region.status.value,
            "loops": len(geo.region.loops),
            "cutouts": len(geo.region.cutouts),
            "accuracy": geo.region.accuracy.value,
        },
        "counts": geo.counts(),
        "courtyards": len(geo.courtyards),
        "spatial_index": {
            "type": geo.hole_index.kind,
            "cell_size_mm": internal_to_mm(geo.index_cell_size),
            "entries": geo.index_entry_count,
        },
        "notes": list(geo.notes),
        "unsupported": list(geo.unsupported),
        "unfilled_zones": len(geo.unfilled_zones),
        "objects": objects,
    }
