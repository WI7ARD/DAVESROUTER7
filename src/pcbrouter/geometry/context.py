"""Per-object rule context: where an existing board object sits and what it is.

Location rule functions (``insideCourtyard``, ``intersectsArea``,
``enclosedByArea``, ``memberOfFootprint``) and pad properties (``Pad_Type``,
``isPlated``) need facts about a concrete object. This module computes them for
every *existing* copper object from the board geometry, following KiCad
(``pcbexpr_functions.cpp`` at fec63a6):

* courtyards: the object's copper collides with the front or back courtyard
  polygon of a footprint (touching counts, any copper layer);
* areas: the object collides with a named zone's outline on a common layer
  (KiCad deflates the outline by its DRC epsilon, so touching does not count);
* enclosure: the object lies entirely inside a zone's outline.

Our polygons approximate KiCad's (arcs are chorded differently), so anything
within :data:`CONTEXT_MARGIN_NM` of a boundary is *maybe* (unknown): a rule
that depends on it then bounds conservatively instead of relaxing on a guess.
"""

from __future__ import annotations

from dataclasses import dataclass

from pcbrouter.domain.geometry import BoundingBox, Point
from pcbrouter.domain.pad import Pad, PadShape, PadType
from pcbrouter.geometry.board import BoardGeometry, CopperItem, ItemKind
from pcbrouter.geometry.clearance import touches
from pcbrouter.geometry.distance import PointCore, PolygonCore, SegmentCore
from pcbrouter.geometry.errors import InvalidGeometryError
from pcbrouter.geometry.polygon import Polygon
from pcbrouter.geometry.shapes import Shape, capsule
from pcbrouter.rules.conditions import ObjectContext

#: boundary band treated as unknown (covers KiCad's own arc polygonisation)
CONTEXT_MARGIN_NM = 10_000

_PAD_TYPE = {
    PadType.SMD: "SMD",
    PadType.THROUGH_HOLE: "Through-hole",
    PadType.NPTH: "NPTH, mechanical",
    PadType.CONNECT: "Edge connector",
}
_PAD_SHAPE = {
    PadShape.CIRCLE: "Circle",
    PadShape.RECT: "Rectangle",
    PadShape.OVAL: "Oval",
    PadShape.TRAPEZOID: "Trapezoid",
    PadShape.CUSTOM: "Custom",
}


@dataclass(frozen=True)
class _Region:
    keys: frozenset[str]  # zone name and uuid (or footprint ref for courtyards)
    polys: tuple[Polygon, ...]
    edges: tuple[Shape, ...]  # boundary as zero-width capsules
    bounds: BoundingBox
    layers: frozenset[str] | None  # None = every layer (courtyards)


def _region(
    keys: frozenset[str], loops: list[tuple[Point, ...]], layers: frozenset[str] | None
) -> _Region | None:
    polys: list[Polygon] = []
    for loop in loops:
        try:
            polys.append(Polygon(list(loop)))
        except InvalidGeometryError:
            continue
    if not polys:
        return None
    edges = tuple(capsule(a, b, 0) for p in polys for a, b in p.edges())
    box = polys[0].bounds
    for p in polys[1:]:
        box = box.union(p.bounds)
    return _Region(keys, tuple(polys), edges, box, layers)


def _anchors(shape: Shape) -> list[Point]:
    """Points that are certainly copper of ``shape``."""
    core = shape.core
    if isinstance(core, PointCore):
        return [core.p]
    if isinstance(core, SegmentCore):
        return [core.a, core.b, Point((core.a.x + core.b.x) // 2, (core.a.y + core.b.y) // 2)]
    assert isinstance(core, PolygonCore)
    return list(core.polygon.points)


def _boundary_distance_ok(p: Point, region: _Region, margin: int) -> bool:
    probe = capsule(p, p, 0)
    return not any(touches(probe, e, margin) for e in region.edges)


def _inside(region: _Region, p: Point) -> bool:
    inside = False
    for poly in region.polys:  # even-odd over the loops
        if poly.contains(p):
            inside = not inside
    return inside


def classify(
    item: CopperItem, region: _Region, margin: int = CONTEXT_MARGIN_NM
) -> tuple[bool | None, bool | None]:
    """(intersects, enclosed) of ``item``'s copper with ``region``: True/False, or
    None within ``margin`` of the boundary."""
    if not item.bounds.expanded(margin).intersects(region.bounds):
        return False, False
    near_edge = any(
        touches(s, e, margin)
        for s in item.shapes
        for e in region.edges
        if e.bounds.expanded(margin).intersects(s.bounds)
    )
    if not near_edge:
        # copper never comes near the boundary: entirely inside or entirely outside
        inside = _inside(region, _anchors(item.shapes[0])[0])
        return inside, inside
    anchors = [p for s in item.shapes for p in _anchors(s)]
    clear = [p for p in anchors if _boundary_distance_ok(p, region, margin)]
    deep_in = any(_inside(region, p) for p in clear)
    deep_out = any(not _inside(region, p) for p in clear)
    return (True if deep_in else None), (False if deep_out else None)


def _pad_facts(pad: Pad | None) -> dict[str, object]:
    if pad is None:
        return {}
    shape = _PAD_SHAPE.get(pad.shape)
    if pad.shape is PadShape.ROUNDRECT:
        chamfered = bool(pad.chamfer_ratio and pad.chamfer_corners)
        shape = "Chamfered rectangle" if chamfered else "Rounded rectangle"
    return {
        "plated": pad.pad_type is PadType.THROUGH_HOLE,
        "pad_type": _PAD_TYPE.get(pad.pad_type),
        "pad_shape": shape,
        "layers": tuple(pad.copper_layers),
    }


class ContextIndex:
    """Lazily computed :class:`ObjectContext` per copper item. Built from the
    board's footprints and zones, which routing never changes, so geometry
    copies share one index (new copper gets its context on first use)."""

    def __init__(self, geo: BoardGeometry) -> None:
        board = geo.board
        self._libs = {c.reference: c.footprint.lib_id for c in board.components}
        self._pads = {p.id: p for p in board.pads}
        court: dict[tuple[str, str], list[tuple[Point, ...]]] = {}
        for cy in geo.courtyards:
            court.setdefault((cy.footprint_ref, cy.layer), []).append(tuple(cy.polygon.points))
        self._courtyards: list[tuple[tuple[str, str], str, _Region]] = []
        for (ref, layer), loops in sorted(court.items()):
            reg = _region(frozenset({ref}), loops, None)
            if reg is not None:
                side = "back" if layer.startswith("B.") else "front"
                self._courtyards.append(((ref, self._libs.get(ref, "")), side, reg))
        self._zones: list[_Region] = []
        for z in board.zones:
            if not z.name:
                continue
            reg = _region(
                frozenset({z.name, z.id}), [z.outline, *z.extra_outlines], frozenset(z.layers)
            )
            if reg is not None:
                self._zones.append(reg)
        self._cache: dict[tuple[str, BoundingBox], ObjectContext | None] = {}

    def get(self, item: CopperItem) -> ObjectContext | None:
        key = (item.uid, item.bounds)
        if key in self._cache:
            return self._cache[key]
        ctx = None if item.kind is ItemKind.ZONE_FILL else self._build(item)
        self._cache[key] = ctx
        return ctx

    def _build(self, item: CopperItem) -> ObjectContext:
        fp = None
        if item.footprint_ref is not None:
            fp = (item.footprint_ref, self._libs.get(item.footprint_ref, ""))
        pad = self._pads.get(item.source_id) if item.kind is ItemKind.PAD else None
        front: set[tuple[str, str]] = set()
        back: set[tuple[str, str]] = set()
        maybe: set[tuple[str, str]] = set()
        for key, side, reg in self._courtyards:
            hit, _ = classify(item, reg)
            if hit is True:
                (front if side == "front" else back).add(key)
            elif hit is None:
                maybe.add(key)
        areas: set[str] = set()
        areas_maybe: set[str] = set()
        enclosed: set[str] = set()
        enclosed_maybe: set[str] = set()
        for reg in self._zones:
            if reg.layers is not None and not (reg.layers & item.layers):
                continue
            hit, inside = classify(item, reg)
            if hit is True:
                areas |= reg.keys
            elif hit is None:
                areas_maybe |= reg.keys
            if inside is True:
                enclosed |= reg.keys
            elif inside is None:
                enclosed_maybe |= reg.keys
        return ObjectContext(
            footprint=fp,
            court_front=frozenset(front),
            court_back=frozenset(back),
            court_maybe=frozenset(maybe),
            areas=frozenset(areas),
            areas_maybe=frozenset(areas_maybe),
            enclosed=frozenset(enclosed),
            enclosed_maybe=frozenset(enclosed_maybe),
            **_pad_facts(pad),  # type: ignore[arg-type]
        )
