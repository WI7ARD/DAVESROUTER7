"""The :class:`Board` aggregate — the application's single source of truth for a PCB.

Boards are immutable (frozen dataclasses of tuples). Future routing stages will
produce *proposals* (new tracks/vias) that are diffed against, and only later merged
into, a new Board instance — never mutated in place. That makes undo, before/after
comparison and "reject proposal" cheap and safe.

Derived lookup tables live in :class:`BoardIndex`, built lazily once per board.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from functools import cached_property
from pathlib import Path

from pcbrouter.domain.component import Component
from pcbrouter.domain.footprint import Footprint
from pcbrouter.domain.geometry import BoundingBox, Point, arc_points, union_all
from pcbrouter.domain.layer import Layer, copper_stack_position
from pcbrouter.domain.net import Net, NetStatistics
from pcbrouter.domain.pad import Pad
from pcbrouter.domain.rules import DesignRules
from pcbrouter.domain.track import Track
from pcbrouter.domain.units import Nm
from pcbrouter.domain.via import Via


class OutlineShape(Enum):
    LINE = "line"
    ARC = "arc"
    CIRCLE = "circle"


@dataclass(frozen=True, slots=True)
class OutlineSegment:
    """One primitive of the board edge (Edge.Cuts).

    * LINE: ``start`` -> ``end``
    * ARC: ``start`` -> ``mid`` -> ``end``
    * CIRCLE: centre ``start``, a point on the circumference ``end``
    """

    shape: OutlineShape
    start: Point
    end: Point
    mid: Point | None = None
    width: Nm = 0

    def points(self) -> list[Point]:
        if self.shape is OutlineShape.ARC and self.mid is not None:
            return arc_points(self.start, self.mid, self.end)
        if self.shape is OutlineShape.CIRCLE:
            r = round(self.start.distance_to(self.end))
            return [
                Point(
                    self.start.x + round(r * math.cos(math.radians(a))),
                    self.start.y + round(r * math.sin(math.radians(a))),
                )
                for a in range(0, 361, 10)
            ]
        return [self.start, self.end]


@dataclass(frozen=True, slots=True)
class BoardOutline:
    segments: tuple[OutlineSegment, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.segments

    @property
    def bounds(self) -> BoundingBox | None:
        return BoundingBox.from_points(p for s in self.segments for p in s.points())

    def closed_loops(self, tolerance: Nm = 1_000) -> tuple[list[list[Point]], int]:
        """Chain segments into closed polylines.

        Returns ``(loops, open_chain_count)``. Board outlines are stored as
        unordered primitives, so segments are joined greedily wherever endpoints
        coincide within ``tolerance`` (default 1 µm). Circles form their own loop.
        """

        def close(a: Point, b: Point) -> bool:
            return abs(a.x - b.x) <= tolerance and abs(a.y - b.y) <= tolerance

        loops: list[list[Point]] = []
        pieces: list[list[Point]] = []
        for seg in self.segments:
            (loops if seg.shape is OutlineShape.CIRCLE else pieces).append(seg.points())
        open_chains = 0
        while pieces:
            chain = pieces.pop()
            extended = True
            while extended and not close(chain[0], chain[-1]):
                extended = False
                for i, piece in enumerate(pieces):
                    if close(chain[-1], piece[0]):
                        chain.extend(piece[1:])
                    elif close(chain[-1], piece[-1]):
                        chain.extend(reversed(piece[:-1]))
                    elif close(chain[0], piece[-1]):
                        chain[:0] = piece[:-1]
                    elif close(chain[0], piece[0]):
                        chain[:0] = list(reversed(piece[1:]))
                    else:
                        continue
                    del pieces[i]
                    extended = True
                    break
            if len(chain) > 2 and close(chain[0], chain[-1]):
                loops.append(chain)
            else:
                open_chains += 1
        return loops, open_chains


@dataclass(frozen=True, slots=True)
class BoardMetadata:
    """File-level facts. Unknown values are ``None`` (displayed as "unknown")."""

    source_path: Path | None = None
    format_version: int | None = None  # KiCad's YYYYMMDD file format version
    generator: str | None = None
    generator_version: str | None = None
    title: str | None = None
    revision: str | None = None
    date: str | None = None
    company: str | None = None
    kicad_major_version_guess: str | None = None


@dataclass(frozen=True, slots=True)
class BoardStatistics:
    footprint_count: int
    pad_count: int
    net_count: int  # excludes the unconnected "" net
    track_count: int
    arc_track_count: int
    via_count: int
    copper_layer_count: int
    total_track_length: Nm
    width: Nm | None
    height: Nm | None


@dataclass(frozen=True, eq=False)  # identity equality: boards are large aggregates
class Board:
    metadata: BoardMetadata
    layers: tuple[Layer, ...]
    nets: tuple[Net, ...]
    components: tuple[Component, ...]
    tracks: tuple[Track, ...]
    vias: tuple[Via, ...]
    outline: BoardOutline = field(default_factory=BoardOutline)
    rules: DesignRules = field(default_factory=DesignRules)

    # ----------------------------------------------------------------- views
    @property
    def footprints(self) -> tuple[Footprint, ...]:
        return tuple(c.footprint for c in self.components)

    @cached_property
    def pads(self) -> tuple[Pad, ...]:
        return tuple(p for c in self.components for p in c.footprint.pads)

    @cached_property
    def copper_layers(self) -> tuple[Layer, ...]:
        """Copper layers in physical stack order (front, inner..., back)."""
        return tuple(
            sorted(
                (lyr for lyr in self.layers if lyr.is_copper),
                key=lambda lyr: copper_stack_position(lyr.name),
            )
        )

    @cached_property
    def copper_layer_names(self) -> list[str]:
        return [layer.name for layer in self.copper_layers]

    def layer(self, name: str) -> Layer | None:
        return self.index.layers_by_name.get(name)

    @cached_property
    def index(self) -> BoardIndex:
        return BoardIndex(self)

    @cached_property
    def bounds(self) -> BoundingBox | None:
        """Board outline bounds if an outline exists, else bounds of all content."""
        outline_bounds = self.outline.bounds
        if outline_bounds is not None:
            return outline_bounds
        return self.content_bounds

    @cached_property
    def content_bounds(self) -> BoundingBox | None:
        return union_all(
            [fp.bounds for fp in self.footprints]
            + [t.bounds for t in self.tracks]
            + [v.bounds for v in self.vias]
        )

    @cached_property
    def statistics(self) -> BoardStatistics:
        outline = self.outline.bounds
        return BoardStatistics(
            footprint_count=len(self.components),
            pad_count=len(self.pads),
            net_count=sum(1 for n in self.nets if not n.is_unconnected),
            track_count=len(self.tracks),
            arc_track_count=sum(1 for t in self.tracks if t.is_arc),
            via_count=len(self.vias),
            copper_layer_count=len(self.copper_layers),
            total_track_length=sum(t.length for t in self.tracks),
            width=outline.width if outline else None,
            height=outline.height if outline else None,
        )


class BoardIndex:
    """Lookup tables derived from a :class:`Board`. Built once, read many times."""

    def __init__(self, board: Board) -> None:
        self.layers_by_name: dict[str, Layer] = {lyr.name: lyr for lyr in board.layers}
        self.nets_by_name: dict[str, Net] = {n.name: n for n in board.nets}
        self.components_by_ref: dict[str, Component] = {}
        self.components_by_id: dict[str, Component] = {}
        for comp in board.components:
            # Duplicate designators do happen (unannotated boards); keep the first.
            self.components_by_ref.setdefault(comp.reference, comp)
            self.components_by_id[comp.id] = comp
        self.pads_by_id: dict[str, Pad] = {p.id: p for p in board.pads}
        self.tracks_by_id: dict[str, Track] = {t.id: t for t in board.tracks}
        self.vias_by_id: dict[str, Via] = {v.id: v for v in board.vias}

        pads_by_net: dict[str, list[Pad]] = defaultdict(list)
        tracks_by_net: dict[str, list[Track]] = defaultdict(list)
        vias_by_net: dict[str, list[Via]] = defaultdict(list)
        for pad in board.pads:
            if pad.net_name is not None:
                pads_by_net[pad.net_name].append(pad)
        for track in board.tracks:
            if track.net_name is not None:
                tracks_by_net[track.net_name].append(track)
        for via in board.vias:
            if via.net_name is not None:
                vias_by_net[via.net_name].append(via)
        self.pads_by_net = dict(pads_by_net)
        self.tracks_by_net = dict(tracks_by_net)
        self.vias_by_net = dict(vias_by_net)

        self.net_statistics: dict[str, NetStatistics] = {}
        for net in board.nets:
            tracks = self.tracks_by_net.get(net.name, [])
            self.net_statistics[net.name] = NetStatistics(
                net=net,
                pad_count=len(self.pads_by_net.get(net.name, [])),
                track_count=len(tracks),
                via_count=len(self.vias_by_net.get(net.name, [])),
                routed_length=sum(t.length for t in tracks),
            )
