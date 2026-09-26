"""Deterministic board facts — the "BOARD FACT" side of every AI interaction.

Everything here is computed from the domain model; nothing is inferred by an AI.
The context builder uses it today. :class:`BoardFactService` also exposes the same
queries as named, schema-described *tools* so a later stage can let an AI agent
ask for specific facts instead of receiving a large board dump. The tool surface is
read-only and has no filesystem, shell or network access.

Honesty notes:

* "no tracks" means no copper segments carry the net. Stage 2 does not analyse
  connectivity, so a net with tracks may still be incompletely routed.
* Net classes and design rules from ``.kicad_pro`` are not loaded yet (unknown).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, ClassVar

from pcbrouter.domain.board import Board
from pcbrouter.domain.units import internal_to_mm


class NetRoutingStatus(StrEnum):
    NO_PADS = "no_pads"
    SINGLE_PAD = "single_pad"
    NO_TRACKS = "no_tracks"  # >= 2 pads, no copper: needs routing
    HAS_TRACKS = "has_tracks"  # some copper exists; completeness not analysed


def _mm(nm: int) -> float:
    return round(internal_to_mm(nm), 4)


@dataclass(frozen=True, slots=True)
class NetFacts:
    name: str
    code: int | None
    pad_count: int
    track_count: int
    via_count: int
    routed_length_mm: float
    track_widths_mm: tuple[float, ...]
    layers_used: tuple[str, ...]
    components: tuple[str, ...]
    locked_track_count: int
    status: NetRoutingStatus


@dataclass(frozen=True, slots=True)
class ComponentFacts:
    reference: str
    value: str | None
    footprint: str
    side: str
    x_mm: float
    y_mm: float
    rotation_deg: float
    pad_count: int
    nets: tuple[str, ...]
    locked: bool


@dataclass(frozen=True, slots=True)
class LayerFacts:
    name: str
    kind: str
    copper_type: str
    is_copper: bool


@dataclass(frozen=True, slots=True)
class BoardFacts:
    name: str
    width_mm: float | None
    height_mm: float | None
    copper_layers: tuple[str, ...]
    components: int
    pads: int
    nets: int
    tracks: int
    vias: int
    total_track_length_mm: float
    nets_without_tracks: int
    min_track_width_mm: float | None
    min_clearance_mm: float | None


class BoardFactService:
    def __init__(self, board: Board) -> None:
        self.board = board

    def board_facts(self, name: str | None = None) -> BoardFacts:
        b = self.board
        s = b.statistics
        rules = b.rules
        return BoardFacts(
            name=name or (b.metadata.source_path.name if b.metadata.source_path else "board"),
            width_mm=_mm(s.width) if s.width is not None else None,
            height_mm=_mm(s.height) if s.height is not None else None,
            copper_layers=tuple(b.copper_layer_names),
            components=s.footprint_count,
            pads=s.pad_count,
            nets=s.net_count,
            tracks=s.track_count,
            vias=s.via_count,
            total_track_length_mm=_mm(s.total_track_length),
            nets_without_tracks=len(self.nets_without_tracks()),
            min_track_width_mm=_mm(rules.min_track_width) if rules.min_track_width else None,
            min_clearance_mm=_mm(rules.min_clearance) if rules.min_clearance else None,
        )

    def net_facts(self, name: str) -> NetFacts | None:
        idx = self.board.index
        net = idx.nets_by_name.get(name)
        if net is None:
            return None
        stats = idx.net_statistics[name]
        tracks = idx.tracks_by_net.get(name, [])
        pads = idx.pads_by_net.get(name, [])
        if stats.pad_count == 0:
            status = NetRoutingStatus.NO_PADS
        elif stats.pad_count == 1:
            status = NetRoutingStatus.SINGLE_PAD
        elif stats.track_count == 0 and stats.via_count == 0:
            status = NetRoutingStatus.NO_TRACKS
        else:
            status = NetRoutingStatus.HAS_TRACKS
        return NetFacts(
            name=name,
            code=net.code,
            pad_count=stats.pad_count,
            track_count=stats.track_count,
            via_count=stats.via_count,
            routed_length_mm=_mm(stats.routed_length),
            track_widths_mm=tuple(sorted({_mm(t.width) for t in tracks})),
            layers_used=tuple(sorted({t.layer for t in tracks})),
            components=tuple(sorted({p.footprint_ref for p in pads})),
            locked_track_count=sum(1 for t in tracks if t.locked),
            status=status,
        )

    def component_facts(self, reference: str) -> ComponentFacts | None:
        comp = self.board.index.components_by_ref.get(reference)
        if comp is None:
            return None
        fp = comp.footprint
        return ComponentFacts(
            reference=comp.reference,
            value=comp.value,
            footprint=fp.lib_id,
            side=fp.side.value,
            x_mm=_mm(fp.position.x),
            y_mm=_mm(fp.position.y),
            rotation_deg=fp.rotation_deg,
            pad_count=len(fp.pads),
            nets=tuple(sorted({p.net_name for p in fp.pads if p.net_name})),
            locked=fp.locked,
        )

    def layer_facts(self) -> list[LayerFacts]:
        return [
            LayerFacts(lyr.name, lyr.kind.value, lyr.copper_type.value, lyr.is_copper)
            for lyr in self.board.layers
        ]

    def nets_without_tracks(self) -> list[str]:
        idx = self.board.index
        return sorted(
            n.name
            for n in self.board.nets
            if idx.net_statistics[n.name].pad_count >= 2
            and idx.net_statistics[n.name].track_count == 0
            and idx.net_statistics[n.name].via_count == 0
        )

    # ------------------------------------------------------------ future tool surface
    TOOLS: ClassVar[dict[str, dict[str, Any]]] = {
        "get_board_statistics": {"description": "Board size, layers and counts.", "parameters": {}},
        "get_net_info": {"description": "Facts about one net.", "parameters": {"name": "string"}},
        "get_component_info": {
            "description": "Facts about one component.",
            "parameters": {"reference": "string"},
        },
        "get_layer_info": {"description": "All board layers.", "parameters": {}},
    }

    def call_tool(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run a read-only fact query by name. Unknown tools/arguments are refused."""
        if tool not in self.TOOLS:
            return {"error": f"unknown tool {tool!r}"}
        expected = set(self.TOOLS[tool]["parameters"])
        if set(args) != expected or not all(isinstance(v, str) for v in args.values()):
            return {"error": f"{tool} expects string arguments {sorted(expected)}"}
        result: object
        if tool == "get_board_statistics":
            result = self.board_facts()
        elif tool == "get_net_info":
            result = self.net_facts(args["name"])
        elif tool == "get_component_info":
            result = self.component_facts(args["reference"])
        else:
            result = self.layer_facts()
        if result is None:
            return {"error": "not found"}
        if isinstance(result, list):
            return {"result": [asdict(r) for r in result]}
        return {"result": asdict(result)}
