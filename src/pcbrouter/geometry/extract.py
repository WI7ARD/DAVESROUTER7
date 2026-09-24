"""Domain model -> :class:`~pcbrouter.geometry.board.BoardGeometry`.

Works purely on the normalised domain model (never on KiCad objects), so it lives
in the geometry package rather than in ``pcbrouter.kicad``.

Pad geometry (board space) = pad position + (shape offset rotated by the absolute pad
angle) + the shape rotated by that angle. Footprint placement is already applied
by the adapter (pad positions/angles in the domain model are absolute), including
back-side mirroring, which KiCad stores pre-applied in the board file.

Representation table (accuracy in brackets):

* circle / rect / oval / roundrect pads [exact]
* chamfered rect [exact]; chamfered *rounded* rect [conservative: sharp corners kept]
* trapezoid [conservative: bounding rectangle of the trapezoid]
* custom pads: anchor + primitives [exact for polygons/lines/circles; conservative
  for arcs (chords + sagitta), Bezier curves (convex hull) and hollow outlines (filled)]
* unknown pad shapes [unknown: bounding rectangle, flagged]
* THT pads with ``remove_unused_layers`` [conservative: copper kept on all layers]
* tracks [exact]; arc tracks [conservative: chords + sagitta margin]
* vias [exact]; zone fills [exact as stored in the file — may be stale]
"""

from __future__ import annotations

import logging
import time
from itertools import pairwise

from pcbrouter.domain.board import Board, OutlineSegment, OutlineShape
from pcbrouter.domain.footprint import Footprint
from pcbrouter.domain.geometry import BoundingBox, Point
from pcbrouter.domain.pad import Pad, PadShape, PadType, PrimitiveKind
from pcbrouter.domain.units import Nm
from pcbrouter.domain.zone import ZoneFillState
from pcbrouter.geometry.board import (
    BoardGeometry,
    BoardRegion,
    CopperItem,
    CourtyardItem,
    EdgeItem,
    HoleItem,
    ItemKind,
    KeepoutItem,
    RegionStatus,
)
from pcbrouter.geometry.errors import InvalidGeometryError
from pcbrouter.geometry.path import ARC_MAX_SAGITTA_NM, arc_capsules, arc_chords
from pcbrouter.geometry.polygon import Polygon, convex_hull
from pcbrouter.geometry.shapes import (
    Shape,
    ShapeAccuracy,
    capsule,
    circle,
    oval,
    polygon,
    rectangle,
)
from pcbrouter.geometry.transforms import pad_local_to_board
from pcbrouter.spatial.grid_index import GridIndex, choose_cell_size
from pcbrouter.spatial.query import LayeredIndex

log = logging.getLogger(__name__)

DEFAULT_ROUNDRECT_RATIO = 0.25  # KiCad's default when the file omits the ratio


# ---------------------------------------------------------------- pads
def _chamfered_rect(pad: Pad, center: Point) -> list[Point]:
    """Rectangle with the requested corners cut at 45 degrees (pad frame, then rotated)."""
    w, h = pad.size
    hw, hh = w // 2, h // 2
    c = round((pad.chamfer_ratio or 0.0) * min(w, h))
    corners = set(pad.chamfer_corners)
    pts: list[Point] = []
    # Clockwise on screen starting top-left: (x, y) offsets in the pad frame.
    spec = (
        ("top_left", (-hw, -hh), ((-hw, -hh + c), (-hw + c, -hh))),
        ("top_right", (hw, -hh), ((hw - c, -hh), (hw, -hh + c))),
        ("bottom_right", (hw, hh), ((hw, hh - c), (hw - c, hh))),
        ("bottom_left", (-hw, hh), ((-hw + c, hh), (-hw, hh - c))),
    )
    for name, corner, cut in spec:
        if c > 0 and name in corners:
            pts.extend(Point(center.x + dx, center.y + dy) for dx, dy in cut)
        else:
            pts.append(Point(center.x + corner[0], center.y + corner[1]))
    return pts


def pad_shapes(pad: Pad) -> tuple[list[Shape], ShapeAccuracy, str | None]:
    """Board-space copper shapes of a pad, the worst accuracy, and a note."""
    w, h = pad.size
    rot = pad.rotation_deg
    center = pad_local_to_board(pad.position, rot, pad.offset)
    shape = pad.shape
    if shape is PadShape.CIRCLE:
        return [circle(center, w // 2)], ShapeAccuracy.EXACT, None
    if shape is PadShape.OVAL:
        return [oval(center, w, h, rot)], ShapeAccuracy.EXACT, None
    if shape is PadShape.RECT and not pad.chamfer_corners:
        return [rectangle(center, w, h, rot)], ShapeAccuracy.EXACT, None
    if shape in (PadShape.ROUNDRECT, PadShape.RECT):
        ratio = pad.roundrect_ratio if pad.roundrect_ratio is not None else DEFAULT_ROUNDRECT_RATIO
        if shape is PadShape.RECT:
            ratio = 0.0
        if pad.chamfer_corners and (pad.chamfer_ratio or 0) > 0:
            pts = _chamfered_rect(pad, Point(0, 0))
            local = [
                pad_local_to_board(pad.position, rot, Point(pad.offset.x + p.x, pad.offset.y + p.y))
                for p in pts
            ]
            if ratio > 0:
                return (
                    [polygon(local, 0, ShapeAccuracy.CONSERVATIVE)],
                    ShapeAccuracy.CONSERVATIVE,
                    "chamfered rounded rectangle: rounding ignored (covers the real pad)",
                )
            return [polygon(local)], ShapeAccuracy.EXACT, None
        radius = round(ratio * min(w, h))
        return [rectangle(center, w, h, rot, radius)], ShapeAccuracy.EXACT, None
    if shape is PadShape.TRAPEZOID:
        dx, dy = pad.trapezoid_delta or (0, 0)
        bw, bh = w + abs(dy), h + abs(dx)
        return (
            [rectangle(center, bw, bh, rot, accuracy=ShapeAccuracy.CONSERVATIVE)],
            ShapeAccuracy.CONSERVATIVE,
            "trapezoid pad approximated by its bounding rectangle",
        )
    if shape is PadShape.CUSTOM:
        return _custom_pad_shapes(pad, center)
    return (
        [rectangle(center, w, h, rot, accuracy=ShapeAccuracy.UNKNOWN)],
        ShapeAccuracy.UNKNOWN,
        f"pad shape '{pad.shape.value}' not understood: size rectangle used, accuracy unknown",
    )


def _custom_pad_shapes(pad: Pad, center: Point) -> tuple[list[Shape], ShapeAccuracy, str | None]:
    rot = pad.rotation_deg
    w, h = pad.size
    shapes: list[Shape] = []
    notes: list[str] = []
    worst = ShapeAccuracy.EXACT
    if pad.custom_anchor is PadShape.RECT:
        shapes.append(rectangle(center, w, h, rot))
    else:
        shapes.append(circle(center, w // 2))

    def to_board(p: Point) -> Point:
        return pad_local_to_board(pad.position, rot, Point(pad.offset.x + p.x, pad.offset.y + p.y))

    for prim in pad.primitives:
        r = prim.width // 2
        pts = [to_board(p) for p in prim.points]
        try:
            if prim.kind is PrimitiveKind.CIRCLE and len(pts) == 2:
                radius = round(pts[0].distance_to(pts[1])) + r
                acc = ShapeAccuracy.EXACT if prim.filled else ShapeAccuracy.CONSERVATIVE
                if not prim.filled:
                    notes.append("hollow circle primitive treated as filled")
                shapes.append(circle(pts[0], radius, acc))
                worst = ShapeAccuracy.worst(worst, acc)
            elif prim.kind is PrimitiveKind.LINE and len(pts) == 2:
                shapes.append(capsule(pts[0], pts[1], r))
            elif prim.kind in (PrimitiveKind.POLYGON, PrimitiveKind.RECT) and len(pts) >= 3:
                acc = ShapeAccuracy.EXACT if prim.filled else ShapeAccuracy.CONSERVATIVE
                if not prim.filled:
                    notes.append("hollow outline primitive treated as filled")
                shapes.append(polygon(pts, r, acc))
                worst = ShapeAccuracy.worst(worst, acc)
            elif prim.kind is PrimitiveKind.ARC and len(pts) == 3:
                arc = arc_capsules(pts[0], pts[1], pts[2], r)
                shapes.extend(arc)
                if arc[0].accuracy is not ShapeAccuracy.EXACT:
                    notes.append("arc primitive approximated by chords (conservative)")
                    worst = ShapeAccuracy.worst(worst, ShapeAccuracy.CONSERVATIVE)
            elif prim.kind is PrimitiveKind.CURVE and len(pts) >= 2:
                hull = convex_hull(pts)
                if len(hull) >= 3:
                    shapes.append(polygon(hull, r, ShapeAccuracy.CONSERVATIVE))
                else:
                    shapes.append(capsule(hull[0], hull[-1], r, ShapeAccuracy.CONSERVATIVE))
                notes.append("Bezier primitive bounded by its control-point hull")
                worst = ShapeAccuracy.worst(worst, ShapeAccuracy.CONSERVATIVE)
        except InvalidGeometryError:
            notes.append(f"degenerate {prim.kind.value} primitive skipped")
    return shapes, worst, "; ".join(dict.fromkeys(notes)) or None


def _hole_shape(pad: Pad) -> Shape | None:
    if pad.drill_size is None:
        return circle(pad.position, pad.drill // 2) if pad.drill else None
    dx, dy = pad.drill_size
    if dx == dy:
        return circle(pad.position, dx // 2)
    return oval(pad.position, dx, dy, pad.rotation_deg)


# ---------------------------------------------------------------- outline
def _fine_points(seg: OutlineSegment) -> list[Point]:
    if seg.shape is OutlineShape.ARC and seg.mid is not None:
        return arc_chords(seg.start, seg.mid, seg.end)[0]
    if seg.shape is OutlineShape.CIRCLE:
        r = round(seg.start.distance_to(seg.end))
        top = Point(seg.start.x, seg.start.y - r)
        bottom = Point(seg.start.x, seg.start.y + r)
        left = Point(seg.start.x - r, seg.start.y)
        right = Point(seg.start.x + r, seg.start.y)
        first = arc_chords(top, left, bottom)[0]
        second = arc_chords(bottom, right, top)[0]
        return first + second[1:]
    return [seg.start, seg.end]


def _edge_shapes(seg: OutlineSegment) -> list[Shape]:
    if seg.shape is OutlineShape.LINE:
        return [capsule(seg.start, seg.end, 0)]
    if seg.shape is OutlineShape.ARC and seg.mid is not None:
        return arc_capsules(seg.start, seg.mid, seg.end, 0)
    pts = _fine_points(seg)
    sag = ARC_MAX_SAGITTA_NM + 1
    return [capsule(a, b, sag, ShapeAccuracy.CONSERVATIVE) for a, b in pairwise(pts)]


def build_region(board: Board) -> BoardRegion:
    outline = board.outline
    if outline.is_empty:
        return BoardRegion(status=RegionStatus.NO_OUTLINE)
    loops, open_chains = outline.closed_loops(points=_fine_points)
    polys: list[Polygon] = []
    for loop in loops:
        try:
            polys.append(Polygon(loop))
        except InvalidGeometryError:
            open_chains += 1
    if not polys:
        return BoardRegion(status=RegionStatus.OPEN_OUTLINE, open_chains=open_chains)
    depths = []
    for i, poly in enumerate(polys):
        probe = poly.points[0]
        depths.append(sum(1 for j, other in enumerate(polys) if j != i and other.contains(probe)))
    has_curves = any(s.shape is not OutlineShape.LINE for s in outline.segments)
    return BoardRegion(
        loops=tuple(polys),
        depths=tuple(depths),
        status=RegionStatus.KNOWN if open_chains == 0 else RegionStatus.OPEN_OUTLINE,
        open_chains=open_chains,
        accuracy=ShapeAccuracy.CONSERVATIVE if has_curves else ShapeAccuracy.EXACT,
    )


# ---------------------------------------------------------------- build
def _bounds_of(shapes: list[Shape] | tuple[Shape, ...]) -> BoundingBox:
    box = shapes[0].bounds
    for s in shapes[1:]:
        box = box.union(s.bounds)
    return box


def _pad_label(pad: Pad) -> str:
    return f"{pad.footprint_ref} pad {pad.number}" if pad.number else f"{pad.footprint_ref} pad"


def build_board_geometry(board: Board) -> BoardGeometry:
    """Extract and index all geometry of ``board``. Deterministic for a given board."""
    t0 = time.perf_counter()
    copper_layers = tuple(board.copper_layer_names)
    copper: dict[str, CopperItem] = {}
    holes: dict[str, HoleItem] = {}
    keepouts: dict[str, KeepoutItem] = {}
    edges: dict[str, EdgeItem] = {}
    notes: list[str] = []
    unsupported: list[str] = []
    unfilled: list[str] = []
    approx_counts: dict[str, int] = {}

    fp_by_ref: dict[str, Footprint] = {c.reference: c.footprint for c in board.components}

    for pad in board.pads:
        fp = fp_by_ref.get(pad.footprint_ref)
        uid = f"pad:{pad.id}"
        hole = _hole_shape(pad) if pad.is_through_hole else None
        layers = frozenset(layer for layer in pad.copper_layers if layer in copper_layers)
        has_copper = pad.pad_type is not PadType.NPTH and bool(layers)
        if has_copper:
            shapes, acc, note = pad_shapes(pad)
            if pad.remove_unused_layers and len(layers) > 2:
                acc = ShapeAccuracy.worst(acc, ShapeAccuracy.CONSERVATIVE)
                note = "; ".join(
                    filter(None, [note, "unused inner-layer copper kept (conservative)"])
                )
            if note:
                approx_counts[note] = approx_counts.get(note, 0) + 1
            clearance = pad.local_clearance
            if clearance is None and fp is not None:
                clearance = fp.local_clearance
            copper[uid] = CopperItem(
                uid=uid,
                kind=ItemKind.PAD,
                source_id=pad.id,
                net=pad.net_name,
                layers=layers,
                shapes=tuple(shapes),
                bounds=_bounds_of(shapes),
                label=_pad_label(pad),
                locked=bool(fp and fp.locked),
                local_clearance=clearance,
                footprint_ref=pad.footprint_ref,
                drill=pad.drill,
                accuracy=acc,
                note=note,
            )
        if hole is not None:
            hid = f"hole:{pad.id}"
            holes[hid] = HoleItem(
                uid=hid,
                shape=hole,
                bounds=hole.bounds,
                plated=pad.pad_type is not PadType.NPTH,
                net=pad.net_name if pad.pad_type is not PadType.NPTH else None,
                owner_uid=uid if has_copper else None,
                label=f"{_pad_label(pad)} hole"
                + (" (NPTH)" if pad.pad_type is PadType.NPTH else ""),
            )

    for track in board.tracks:
        if track.layer not in copper_layers:
            unsupported.append(f"track {track.id} on non-copper layer {track.layer} ignored")
            continue
        uid = f"track:{track.id}"
        r = track.width // 2
        if track.mid is not None:
            shapes = arc_capsules(track.start, track.mid, track.end, r)
            acc = shapes[0].accuracy
            note = shapes[0].note
        else:
            shapes = [capsule(track.start, track.end, r)]
            acc, note = ShapeAccuracy.EXACT, None
        copper[uid] = CopperItem(
            uid=uid,
            kind=ItemKind.TRACK,
            source_id=track.id,
            net=track.net_name,
            layers=frozenset((track.layer,)),
            shapes=tuple(shapes),
            bounds=_bounds_of(shapes),
            label=f"track on {track.layer}",
            locked=track.locked,
            width=track.width,
            accuracy=acc,
            note=note,
        )

    for via in board.vias:
        uid = f"via:{via.id}"
        layers = frozenset(
            layer for layer in copper_layers if via.spans_layer(layer, list(copper_layers))
        )
        shape = circle(via.position, via.diameter // 2)
        copper[uid] = CopperItem(
            uid=uid,
            kind=ItemKind.VIA,
            source_id=via.id,
            net=via.net_name,
            layers=layers,
            shapes=(shape,),
            bounds=shape.bounds,
            label=f"{via.via_type.value} via",
            locked=via.locked,
            diameter=via.diameter,
            drill=via.drill,
        )
        if via.drill:
            hid = f"hole:{via.id}"
            hs = circle(via.position, via.drill // 2)
            holes[hid] = HoleItem(hid, hs, hs.bounds, True, via.net_name, uid, "via hole")

    for zone in board.zones:
        if zone.is_keepout:
            assert zone.keepout is not None
            zone_layers = frozenset(layer for layer in zone.layers if layer in copper_layers)
            if not zone_layers:
                continue
            for n, loop in enumerate((zone.outline, *zone.extra_outlines)):
                try:
                    shape = polygon(list(loop))
                except InvalidGeometryError:
                    notes.append(f"keepout {zone.id}: degenerate outline skipped")
                    continue
                kid = f"keepout:{zone.id}/{n}"
                keepouts[kid] = KeepoutItem(
                    kid, zone.id, zone.name, zone_layers, shape, shape.bounds, zone.keepout,
                    zone.footprint_ref,
                )  # fmt: skip
            continue
        if zone.fill_state is ZoneFillState.UNFILLED:
            unfilled.append(f"zone {zone.name or zone.id} ({zone.net_name or 'no net'})")
            continue
        for n, fill in enumerate(zone.filled):
            if fill.layer not in copper_layers:
                continue
            try:
                shape = polygon(list(fill.points))
            except InvalidGeometryError:
                notes.append(f"zone {zone.id}: degenerate fill polygon skipped")
                continue
            uid = f"zone:{zone.id}/{fill.layer}/{n}"
            copper[uid] = CopperItem(
                uid=uid,
                kind=ItemKind.ZONE_FILL,
                source_id=zone.id,
                net=zone.net_name,
                layers=frozenset((fill.layer,)),
                shapes=(shape,),
                bounds=shape.bounds,
                label=f"zone {zone.name or zone.net_name or zone.id} fill on {fill.layer}",
                locked=zone.locked,
                local_clearance=zone.local_clearance,
                note="zone fill as stored in the file (may be stale if not refilled)",
            )

    for n, seg in enumerate(board.outline.segments):
        for k, shape in enumerate(_edge_shapes(seg)):
            eid = f"edge:{n}/{k}"
            edges[eid] = EdgeItem(eid, shape, shape.bounds)

    courtyards: list[CourtyardItem] = []
    for comp in board.components:
        for cy in comp.footprint.courtyards:
            try:
                courtyards.append(CourtyardItem(comp.reference, cy.layer, Polygon(cy.points)))
            except InvalidGeometryError:
                continue

    region = build_region(board)
    if region.status is RegionStatus.NO_OUTLINE:
        notes.append("no board outline (Edge.Cuts): inside/outside-board checks unavailable")
    elif region.status is RegionStatus.OPEN_OUTLINE:
        notes.append(
            f"board outline is not closed ({region.open_chains} open chain(s)): "
            "inside/outside-board checks unavailable"
        )
    for note, count in sorted(approx_counts.items()):
        notes.append(f"{count} pad(s): {note}")

    t1 = time.perf_counter()
    cell = choose_cell_size([i.bounds for i in copper.values()])
    copper_index = LayeredIndex(copper_layers, lambda: GridIndex(cell))
    by_net: dict[str | None, list[str]] = {}
    for uid, item in copper.items():
        copper_index.insert(uid, item.bounds, item.layers)
        by_net.setdefault(item.net, []).append(uid)
    hole_index = GridIndex(cell)
    for uid, h in holes.items():
        hole_index.insert(uid, h.bounds)
    keepout_index = GridIndex(max(cell, 1_000_000))
    for uid, keepout in keepouts.items():
        keepout_index.insert(uid, keepout.bounds)
    edge_index = GridIndex(max(cell, 1_000_000))
    for uid, e in edges.items():
        edge_index.insert(uid, e.bounds)
    t2 = time.perf_counter()

    geometry = BoardGeometry(
        board=board,
        fingerprint=board.fingerprint,
        copper_layers=copper_layers,
        copper=copper,
        holes=holes,
        keepouts=keepouts,
        edges=edges,
        region=region,
        courtyards=tuple(courtyards),
        copper_index=copper_index,
        hole_index=hole_index,
        keepout_index=keepout_index,
        edge_index=edge_index,
        notes=notes,
        unsupported=unsupported,
        unfilled_zones=unfilled,
        build_seconds=t1 - t0,
        index_seconds=t2 - t1,
        index_cell_size=cell,
        by_net=by_net,
    )
    counts = geometry.counts()
    log.info(
        "geometry.build copper=%d holes=%d keepouts=%d edges=%d region=%s approx=%d "
        "extract_ms=%.1f index_ms=%.1f cell_um=%d index_entries=%d",
        len(copper), len(holes), len(keepouts), len(edges), region.status.value,
        counts["approximate_items"], (t1 - t0) * 1e3, (t2 - t1) * 1e3, cell // 1000,
        counts["index_entries"],
    )  # fmt: skip
    return geometry


def clearance_envelope(item: CopperItem, clearance: Nm) -> list[Shape]:
    """The item's copper grown by ``clearance`` (exact Minkowski inflation)."""
    return [s.inflated(clearance) for s in item.shapes]
