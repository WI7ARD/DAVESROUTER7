"""The board as geometry: every copper object, hole, keepout and board edge as
:class:`~pcbrouter.geometry.shapes.Shape` objects, spatially indexed per layer.

This is what the collision engine, internal DRC, connectivity and occupancy code
query. It is built once per board (see :mod:`pcbrouter.geometry.extract`), is
immutable afterwards, and is tied to ``board.fingerprint``. Hypothetical route
proposals are never inserted here (they live in :mod:`pcbrouter.routing.proposal`).

Stable ids: ``pad:<id>``, ``track:<id>``, ``via:<id>``, ``zone:<id>/<layer>/<n>``,
``hole:<owner id>``, ``keepout:<zone id>/<n>``, ``edge:<n>`` — built from the
source file's UUIDs, so they stay stable across reloads of the same file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from pcbrouter.domain.board import Board
from pcbrouter.domain.geometry import BoundingBox, Point
from pcbrouter.domain.units import Nm
from pcbrouter.domain.zone import KeepoutRules
from pcbrouter.geometry.polygon import Polygon
from pcbrouter.geometry.shapes import Shape, ShapeAccuracy
from pcbrouter.spatial.index import SpatialIndex
from pcbrouter.spatial.query import LayeredIndex


class ItemKind(Enum):
    PAD = "pad"
    TRACK = "track"
    VIA = "via"
    ZONE_FILL = "zone_fill"

    @property
    def label(self) -> str:
        return {"pad": "pad", "track": "track", "via": "via", "zone_fill": "zone fill"}[self.value]


@dataclass(frozen=True, slots=True)
class CopperItem:
    uid: str
    kind: ItemKind
    source_id: str  # id of the domain object (Pad.id, Track.id, ...)
    net: str | None  # None = no net (every no-net item is its own island)
    layers: frozenset[str]
    shapes: tuple[Shape, ...]
    bounds: BoundingBox
    label: str  # human-readable, e.g. "U4 pad 7"
    locked: bool = False
    local_clearance: Nm | None = None
    footprint_ref: str | None = None
    width: Nm | None = None  # tracks
    diameter: Nm | None = None  # vias
    drill: Nm | None = None  # vias / plated pads
    accuracy: ShapeAccuracy = ShapeAccuracy.EXACT
    note: str | None = None


@dataclass(frozen=True, slots=True)
class HoleItem:
    """A drilled hole. Plated holes belong to a pad/via (``owner_uid``); NPTH holes
    are mechanical and belong to no net."""

    uid: str
    shape: Shape
    bounds: BoundingBox
    plated: bool
    net: str | None
    owner_uid: str | None
    label: str


@dataclass(frozen=True, slots=True)
class KeepoutItem:
    uid: str
    zone_id: str
    name: str | None
    layers: frozenset[str]
    shape: Shape
    bounds: BoundingBox
    rules: KeepoutRules
    footprint_ref: str | None = None

    @property
    def label(self) -> str:
        base = f"keepout '{self.name}'" if self.name else "keepout"
        return f"{base} ({self.rules.describe()})"


@dataclass(frozen=True, slots=True)
class EdgeItem:
    """One board-edge chord (radius = sagitta margin for arcs, else 0)."""

    uid: str
    shape: Shape
    bounds: BoundingBox


@dataclass(frozen=True, slots=True)
class CourtyardItem:
    """Placement metadata only — courtyards are NOT routing keepouts."""

    footprint_ref: str
    layer: str
    polygon: Polygon


class RegionStatus(Enum):
    KNOWN = "known"
    NO_OUTLINE = "no outline"  # no Edge.Cuts at all
    OPEN_OUTLINE = "open outline"  # Edge.Cuts does not form closed loops


@dataclass(frozen=True, slots=True)
class BoardRegion:
    """The board material: closed Edge.Cuts loops with even-odd nesting (outer
    boundary, cutouts inside it, islands inside cutouts)."""

    loops: tuple[Polygon, ...] = ()
    depths: tuple[int, ...] = ()
    status: RegionStatus = RegionStatus.NO_OUTLINE
    open_chains: int = 0
    accuracy: ShapeAccuracy = ShapeAccuracy.EXACT

    @property
    def outer(self) -> list[Polygon]:
        return [p for p, d in zip(self.loops, self.depths, strict=True) if d % 2 == 0]

    @property
    def cutouts(self) -> list[Polygon]:
        return [p for p, d in zip(self.loops, self.depths, strict=True) if d % 2 == 1]

    def contains(self, p: Point) -> bool | None:
        """True inside board material, False outside or in a cutout, ``None`` when
        the outline is unknown (callers must treat that conservatively)."""
        if self.status is not RegionStatus.KNOWN:
            return None
        inside = False
        for loop in self.loops:
            if loop.contains(p):
                inside = not inside
        return inside

    @property
    def area_nm2(self) -> float:
        return sum(
            p.area if d % 2 == 0 else -p.area for p, d in zip(self.loops, self.depths, strict=True)
        )


@dataclass
class BoardGeometry:
    """Immutable-after-build geometry view of a :class:`Board`."""

    board: Board
    fingerprint: str
    copper_layers: tuple[str, ...]
    copper: dict[str, CopperItem]
    holes: dict[str, HoleItem]
    keepouts: dict[str, KeepoutItem]
    edges: dict[str, EdgeItem]
    region: BoardRegion
    courtyards: tuple[CourtyardItem, ...]
    copper_index: LayeredIndex
    hole_index: SpatialIndex
    keepout_index: SpatialIndex
    edge_index: SpatialIndex
    #: Things represented approximately or not at all (for diagnostics/UI).
    notes: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    unfilled_zones: list[str] = field(default_factory=list)
    build_seconds: float = 0.0
    index_seconds: float = 0.0
    index_cell_size: int = 0
    #: uids of copper items by net, and pads by net (for connectivity).
    by_net: dict[str | None, list[str]] = field(default_factory=dict)

    # ------------------------------------------------------------ layers
    def is_copper_layer(self, layer: str) -> bool:
        return layer in self.copper_layers

    def adjacent_copper_layers(self, layer: str) -> list[str]:
        if layer not in self.copper_layers:
            return []
        i = self.copper_layers.index(layer)
        return [self.copper_layers[j] for j in (i - 1, i + 1) if 0 <= j < len(self.copper_layers)]

    def layer_span(self, a: str, b: str) -> list[str]:
        """Copper layers from ``a`` to ``b`` inclusive, in stack order."""
        i, j = self.copper_layers.index(a), self.copper_layers.index(b)
        lo, hi = min(i, j), max(i, j)
        return list(self.copper_layers[lo : hi + 1])

    # ------------------------------------------------------------ queries
    def copper_near(self, layer: str, box: BoundingBox) -> list[CopperItem]:
        return [self.copper[uid] for uid in self.copper_index.query(layer, box)]

    def holes_near(self, box: BoundingBox) -> list[HoleItem]:
        return [self.holes[uid] for uid in self.hole_index.query(box)]

    def keepouts_near(self, layer: str, box: BoundingBox) -> list[KeepoutItem]:
        return [
            k for k in (self.keepouts[uid] for uid in self.keepout_index.query(box))
            if layer in k.layers
        ]  # fmt: skip

    def edges_near(self, box: BoundingBox) -> list[EdgeItem]:
        return [self.edges[uid] for uid in self.edge_index.query(box)]

    def contains_routable_point(self, p: Point) -> bool | None:
        """Inside the board material (not outside, not in a cutout) and not inside a
        drilled hole. ``None`` when the board outline is unknown."""
        inside = self.region.contains(p)
        if not inside:
            return inside
        probe = BoundingBox(p.x, p.y, p.x, p.y)
        return not any(h.shape.contains(p) for h in self.holes_near(probe))

    # ------------------------------------------------------------ stats
    @property
    def index_entry_count(self) -> int:
        return (
            self.copper_index.entry_count()
            + len(self.hole_index)
            + len(self.keepout_index)
            + len(self.edge_index)
        )

    def counts(self) -> dict[str, int]:
        by_kind: dict[str, int] = {}
        for item in self.copper.values():
            by_kind[item.kind.value] = by_kind.get(item.kind.value, 0) + 1
        return {
            **{f"copper_{k}": v for k, v in sorted(by_kind.items())},
            "holes": len(self.holes),
            "keepouts": len(self.keepouts),
            "edge_chords": len(self.edges),
            "courtyards": len(self.courtyards),
            "approximate_items": sum(
                1 for i in self.copper.values() if i.accuracy is not ShapeAccuracy.EXACT
            ),
            "index_entries": self.index_entry_count,
        }
