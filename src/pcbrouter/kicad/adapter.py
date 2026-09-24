"""Translate a parsed KiCad S-expression tree into the internal :class:`Board`.

This is the *only* module that understands KiCad board semantics. It must:

* never leak :class:`SNode` or other KiCad-shaped objects to callers;
* convert every length through :func:`pcbrouter.domain.units.parse_mm`;
* be defensive: one malformed footprint/track must not abort the whole load. Items
  that cannot be understood are skipped and reported as :class:`LoadWarning` —
  never silently dropped and never guessed at.

Supported format range: KiCad 5 (format 20171130) up to KiCad 9 (20241229). Newer
formats are loaded best-effort with a warning.
"""

from __future__ import annotations

import itertools
import logging
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Concatenate

from pcbrouter.domain.board import (
    Board,
    BoardMetadata,
    BoardOutline,
    OutlineSegment,
    OutlineShape,
)
from pcbrouter.domain.component import Component
from pcbrouter.domain.footprint import BoardSide, Footprint
from pcbrouter.domain.geometry import (
    BoundingBox,
    Point,
    arc_mid_from_center,
    arc_points,
    rotate_point,
)
from pcbrouter.domain.layer import (
    BACK_COPPER,
    EDGE_CUTS,
    FRONT_COPPER,
    CopperLayerType,
    Layer,
    classify_layer_name,
    copper_stack_position,
    expand_layer_pattern,
)
from pcbrouter.domain.net import Net
from pcbrouter.domain.pad import Pad, PadShape, PadType
from pcbrouter.domain.rules import DesignRules
from pcbrouter.domain.track import Track
from pcbrouter.domain.units import Nm, UnitConversionError, parse_mm
from pcbrouter.domain.via import Via, ViaType
from pcbrouter.kicad.errors import MalformedBoardError, UnsupportedKiCadVersion
from pcbrouter.kicad.parser import SNode, line_col

log = logging.getLogger(__name__)

#: KiCad 5.0 file format. Older (KiCad 4 "version 4") files use different syntax.
MIN_SUPPORTED_VERSION = 20171130
#: Newest format this adapter has been written against (KiCad 9.0).
NEWEST_KNOWN_VERSION = 20241229

_KICAD_MAJOR_BY_VERSION: tuple[tuple[int, str], ...] = (
    (20241229, "9"),
    (20240108, "8"),
    (20221018, "7"),
    (20211014, "6"),
    (20171130, "5"),
)

#: Top-level tokens that carry no geometry we need, or are handled elsewhere.
_IGNORED_TOP_LEVEL = frozenset(
    {
        "version", "generator", "generator_version", "general", "paper", "page",
        "title_block", "layers", "setup", "net", "net_class", "property",
        "embedded_fonts", "host",
    }
)  # fmt: skip
#: Recognised constructs that Stage 1 intentionally does not display.
_NOT_DISPLAYED_TOP_LEVEL = frozenset(
    {
        "zone", "dimension", "gr_text", "gr_text_box", "target", "group", "image",
        "generated", "table", "embedded_files", "gr_vector", "gr_bbox", "barcode",
        "point",
    }
)  # fmt: skip
_OUTLINE_GRAPHICS = frozenset({"gr_line", "gr_arc", "gr_circle", "gr_rect", "gr_poly", "gr_curve"})
_FP_GRAPHICS = frozenset({"fp_line", "fp_arc", "fp_circle", "fp_rect", "fp_poly", "fp_curve"})


class WarningSeverity(Enum):
    INFO = "info"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class LoadWarning:
    severity: WarningSeverity
    message: str
    line: int | None = None

    def __str__(self) -> str:
        where = f" (line {self.line})" if self.line is not None else ""
        return f"{self.severity.value}: {self.message}{where}"


class _ItemError(Exception):
    """Raised inside item parsers; converted into a LoadWarning by the caller."""


@dataclass
class _Context:
    text: str | None
    warnings: list[LoadWarning] = field(default_factory=list)
    net_names_by_code: dict[int, str] = field(default_factory=dict)
    declared_nets: dict[str, Net] = field(default_factory=dict)
    referenced_nets: set[str] = field(default_factory=set)
    copper_layers: list[str] = field(default_factory=list)
    used_ids: set[str] = field(default_factory=set)

    def line_of(self, node: SNode) -> int | None:
        return line_col(self.text, node.offset)[0] if self.text is not None else None

    def warn(self, message: str, node: SNode | None = None) -> None:
        line = self.line_of(node) if node is not None else None
        self.warnings.append(LoadWarning(WarningSeverity.WARNING, message, line))

    def info(self, message: str) -> None:
        self.warnings.append(LoadWarning(WarningSeverity.INFO, message))

    def unique_id(self, preferred: str | None, fallback: str) -> str:
        candidate = preferred or fallback
        if candidate in self.used_ids:
            n = 2
            while f"{candidate}#{n}" in self.used_ids:
                n += 1
            candidate = f"{candidate}#{n}"
        self.used_ids.add(candidate)
        return candidate


# --------------------------------------------------------------------- helpers
def _mm(atom: str | None, what: str) -> Nm:
    if atom is None:
        raise _ItemError(f"missing {what}")
    try:
        return parse_mm(atom)
    except UnitConversionError as exc:
        raise _ItemError(f"invalid {what} {atom!r}") from exc


def _float(atom: str | None, what: str, default: float | None = None) -> float:
    if atom is None:
        if default is not None:
            return default
        raise _ItemError(f"missing {what}")
    try:
        return float(atom)
    except ValueError as exc:
        raise _ItemError(f"invalid {what} {atom!r}") from exc


def _point(node: SNode | None, what: str) -> Point:
    if node is None:
        raise _ItemError(f"missing {what}")
    return Point(_mm(node.atom(0), f"{what} x"), _mm(node.atom(1), f"{what} y"))


def _at(node: SNode) -> tuple[Point, float]:
    at = node.first("at")
    if at is None:
        raise _ItemError("missing position (at ...)")
    pos = Point(_mm(at.atom(0), "x"), _mm(at.atom(1), "y"))
    rot = _float(at.atom(2), "rotation", 0.0)
    return pos, rot


def _item_id(node: SNode) -> str | None:
    return node.value("uuid") or node.value("tstamp")


def _is_int(text: str) -> bool:
    return text.lstrip("-").isdigit()


def _graphic_points(node: SNode) -> list[Point]:
    """Points describing a gr_*/fp_* graphic, used for outlines and bounds."""
    kind = node.name[3:]  # strip "gr_"/"fp_"
    if kind == "line":
        return [_point(node.first("start"), "start"), _point(node.first("end"), "end")]
    if kind == "rect":
        a = _point(node.first("start"), "start")
        b = _point(node.first("end"), "end")
        return [a, Point(b.x, a.y), b, Point(a.x, b.y), a]
    if kind == "circle":
        # KiCad 6+: (center)(end); KiCad 5: (center)(end) as well.
        c = _point(node.first("center") or node.first("start"), "center")
        e = _point(node.first("end"), "end")
        r = round(c.distance_to(e))
        return [Point(c.x - r, c.y - r), Point(c.x + r, c.y + r)]
    if kind == "arc":
        start, mid, end = _arc_triplet(node)
        return arc_points(start, mid, end)
    if kind in ("poly", "curve"):
        pts_node = node.first("pts")
        if pts_node is None:
            raise _ItemError("missing pts")
        pts = [_point(xy, "xy") for xy in pts_node.nodes("xy")]
        if len(pts) < 2:
            raise _ItemError("polygon with fewer than two points")
        if kind == "poly":
            pts.append(pts[0])
        return pts
    raise _ItemError(f"unsupported graphic {node.name}")


def _arc_triplet(node: SNode) -> tuple[Point, Point, Point]:
    """Return ``(start, mid, end)`` for both KiCad 6+ and KiCad 5 arc syntax."""
    if node.first("mid") is not None:
        return (
            _point(node.first("start"), "start"),
            _point(node.first("mid"), "mid"),
            _point(node.first("end"), "end"),
        )
    # KiCad 5: (start <center>) (end <arc start>) (angle <sweep>)
    center = _point(node.first("start"), "arc center")
    start = _point(node.first("end"), "arc start")
    angle = _float(node.value("angle"), "arc angle")
    mid, end = arc_mid_from_center(center, start, angle)
    return start, mid, end


def _graphic_layer(node: SNode) -> str | None:
    return node.value("layer")


# --------------------------------------------------------------------- adapter
class KiCadBoardAdapter:
    """Converts one parsed ``kicad_pcb`` tree into a :class:`Board`."""

    def __init__(self, root: SNode, *, source_path: Path | None = None, text: str | None = None):
        self._root = root
        self._path = source_path
        self._ctx = _Context(text=text)

    @property
    def warnings(self) -> list[LoadWarning]:
        return list(self._ctx.warnings)

    def build(self) -> Board:
        root = self._root
        if root.name != "kicad_pcb":
            hint = " (this looks like a schematic)" if root.name == "kicad_sch" else ""
            raise MalformedBoardError(
                f"not a KiCad PCB: top-level element is '{root.name}'{hint}", path=self._path
            )
        metadata = self._metadata()
        layers = self._layers()
        self._ctx.copper_layers = [
            name
            for name in sorted(
                (lyr.name for lyr in layers if lyr.is_copper), key=copper_stack_position
            )
        ]
        self._declare_nets()

        components: list[Component] = []
        tracks: list[Track] = []
        vias: list[Via] = []
        outline: list[OutlineSegment] = []
        not_displayed: Counter[str] = Counter()
        unknown: Counter[str] = Counter()

        for index, node in enumerate(root.nodes()):
            name = node.name
            if name in ("footprint", "module"):
                comp = self._guard(node, f"footprint #{index}", self._footprint, index)
                if comp is not None:
                    components.append(comp)
            elif name in ("segment", "arc"):
                track = self._guard(node, f"{name} track", self._track, index)
                if track is not None:
                    tracks.append(track)
            elif name == "via":
                via = self._guard(node, "via", self._via, index)
                if via is not None:
                    vias.append(via)
            elif name in _OUTLINE_GRAPHICS:
                if _graphic_layer(node) == EDGE_CUTS:
                    segs = self._guard(node, f"board outline {name}", self._outline_segments)
                    outline.extend(segs or ())
                else:
                    not_displayed["graphic on non-edge layer"] += 1
            elif name in _NOT_DISPLAYED_TOP_LEVEL:
                not_displayed[name] += 1
            elif name in _IGNORED_TOP_LEVEL:
                continue
            else:
                unknown[name] += 1

        for name, count in sorted(not_displayed.items()):
            self._ctx.info(f"{count} × '{name}' present but not displayed in Stage 1")
        for name, count in sorted(unknown.items()):
            self._ctx.warn(f"{count} × unrecognised construct '{name}' ignored")
        if not outline:
            self._ctx.info("no Edge.Cuts board outline found; bounds derived from content")

        duplicates = sorted(
            ref for ref, n in Counter(c.reference for c in components).items() if n > 1
        )
        if duplicates:
            shown = ", ".join(duplicates[:10]) + (" …" if len(duplicates) > 10 else "")
            self._ctx.warn(f"duplicate reference designators (board not annotated?): {shown}")
        nets = self._final_nets()
        return Board(
            metadata=metadata,
            layers=tuple(layers),
            nets=nets,
            components=tuple(components),
            tracks=tuple(tracks),
            vias=tuple(vias),
            outline=BoardOutline(tuple(outline)),
            rules=self._rules(),
        )

    # ------------------------------------------------------------ plumbing
    def _guard[**P, T](
        self,
        node: SNode,
        what: str,
        parse: Callable[Concatenate[SNode, P], T],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T | None:
        """Run an item parser; on failure record a warning and return ``None``."""
        try:
            return parse(node, *args, **kwargs)
        except _ItemError as exc:
            self._ctx.warn(f"skipped {what}: {exc}", node)
            return None

    # ------------------------------------------------------------ metadata
    def _metadata(self) -> BoardMetadata:
        root = self._root
        version_text = root.value("version")
        version: int | None = None
        if version_text is not None and _is_int(version_text):
            version = int(version_text)
        if version is None:
            self._ctx.warn("file format version missing or unreadable; parsing best-effort")
        elif version < MIN_SUPPORTED_VERSION:
            raise UnsupportedKiCadVersion(version, MIN_SUPPORTED_VERSION, path=self._path)
        elif version > NEWEST_KNOWN_VERSION:
            self._ctx.warn(
                f"format version {version} is newer than the newest tested version "
                f"({NEWEST_KNOWN_VERSION}); parsed best-effort"
            )
        major = None
        if version is not None:
            major = next((m for v, m in _KICAD_MAJOR_BY_VERSION if version >= v), None)
            if version > NEWEST_KNOWN_VERSION:
                major = f"newer than {major}"
        tb = root.first("title_block")
        return BoardMetadata(
            source_path=self._path,
            format_version=version,
            generator=root.value("generator"),
            generator_version=root.value("generator_version"),
            title=tb.value("title") if tb else None,
            revision=tb.value("rev") if tb else None,
            date=tb.value("date") if tb else None,
            company=tb.value("company") if tb else None,
            kicad_major_version_guess=major,
        )

    def _layers(self) -> list[Layer]:
        table = self._root.first("layers")
        if table is None:
            self._ctx.warn("no layer table; assuming a two-layer board")
            return [
                Layer.from_name(FRONT_COPPER),
                Layer.from_name(BACK_COPPER),
                Layer.from_name(EDGE_CUTS),
            ]
        layers: list[Layer] = []
        for entry in table.nodes():
            atoms = entry.atoms()
            if not atoms:
                self._ctx.warn("layer entry without a name ignored", entry)
                continue
            name = atoms[0]
            kind = classify_layer_name(name)
            type_text = atoms[1] if len(atoms) > 1 else "unknown"
            try:
                ctype = CopperLayerType(type_text)
            except ValueError:
                ctype = CopperLayerType.UNKNOWN
            if not kind.is_copper:
                ctype = CopperLayerType.NOT_COPPER
            ordinal = int(entry.name) if _is_int(entry.name) else None
            user_name = atoms[2] if len(atoms) > 2 else None
            layers.append(Layer(name, kind, ordinal, ctype, user_name))
        return layers

    def _rules(self) -> DesignRules:
        setup = self._root.first("setup")
        general = self._root.first("general")
        thickness = None
        if general is not None and general.value("thickness") is not None:
            try:
                thickness = _mm(general.value("thickness"), "board thickness")
            except _ItemError as exc:
                self._ctx.warn(f"board thickness ignored: {exc}", general)
        values: dict[str, Nm | None] = {
            "clearance": None,
            "track": None,
            "via": None,
            "drill": None,
        }
        source = "unknown"
        if setup is not None:
            # KiCad 5 stored minimums in (setup ...); KiCad 6+ moved them to .kicad_pro.
            for key, token in (
                ("track", "trace_min"),
                ("via", "via_min_size"),
                ("drill", "via_min_drill"),
            ):
                if setup.value(token) is not None:
                    try:
                        values[key] = _mm(setup.value(token), token)
                        source = "board file setup (KiCad 5 style)"
                    except _ItemError as exc:
                        self._ctx.warn(f"design rule ignored: {exc}", setup)
        default_class = next(
            (nc for nc in self._root.nodes("net_class") if nc.atom(0) == "Default"), None
        )
        if default_class is not None and default_class.value("clearance") is not None:
            try:
                values["clearance"] = _mm(default_class.value("clearance"), "clearance")
                source = "board file setup (KiCad 5 style)"
            except _ItemError as exc:
                self._ctx.warn(f"design rule ignored: {exc}", default_class)
        if thickness is not None and source == "unknown":
            source = "board file"
        return DesignRules(
            source=source,
            min_clearance=values["clearance"],
            min_track_width=values["track"],
            min_via_diameter=values["via"],
            min_via_drill=values["drill"],
            board_thickness=thickness,
        )

    # ------------------------------------------------------------ nets
    def _declare_nets(self) -> None:
        for node in self._root.nodes("net"):
            atoms = node.atoms()
            if len(atoms) >= 2 and _is_int(atoms[0]):
                code, name = int(atoms[0]), atoms[1]
            elif len(atoms) == 1 and not _is_int(atoms[0]):
                code, name = None, atoms[0]
            else:
                self._ctx.warn("unreadable net declaration ignored", node)
                continue
            if code is not None:
                self._ctx.net_names_by_code[code] = name
            if name and name not in self._ctx.declared_nets:
                self._ctx.declared_nets[name] = Net(name=name, code=code)

    def _net_of(self, node: SNode) -> str | None:
        """Resolve an item's ``(net ...)`` reference to a net name (``None`` = no net)."""
        net = node.first("net")
        if net is None:
            return None
        atoms = net.atoms()
        if not atoms:
            return None
        if len(atoms) >= 2:
            name = atoms[1]
        elif _is_int(atoms[0]):
            code = int(atoms[0])
            if code == 0:
                return None
            resolved = self._ctx.net_names_by_code.get(code)
            if resolved is None:
                raise _ItemError(f"references undeclared net code {code}")
            name = resolved
        else:
            name = atoms[0]
        if not name:
            return None
        self._ctx.referenced_nets.add(name)
        return name

    def _final_nets(self) -> tuple[Net, ...]:
        nets = dict(self._ctx.declared_nets)
        for name in sorted(self._ctx.referenced_nets - nets.keys()):
            nets[name] = Net(name=name, code=None)
        return tuple(nets.values())

    # ------------------------------------------------------------ footprints
    def _footprint(self, node: SNode, index: int) -> Component:
        lib_id = node.atom(0) or ""
        position, rotation = _at(node)
        layer = node.value("layer") or FRONT_COPPER
        side = BoardSide.BACK if layer == BACK_COPPER else BoardSide.FRONT
        fp_id = self._ctx.unique_id(_item_id(node), f"footprint:{index}")

        reference: str | None = None
        value: str | None = None
        properties: dict[str, str] = {}
        for prop in node.nodes("property"):
            key, val = prop.atom(0), prop.atom(1)
            if key is None or val is None:
                continue
            if key == "Reference":
                reference = val
            elif key == "Value":
                value = val
            else:
                properties[key] = val
        for text in node.nodes("fp_text"):
            kind, val = text.atom(0), text.atom(1)
            if kind == "reference" and reference is None:
                reference = val
            elif kind == "value" and value is None:
                value = val
        if reference is None:
            self._ctx.warn(f"footprint {lib_id or fp_id} has no reference designator", node)
            reference = "?"

        pads: list[Pad] = []
        for pad_index, pad_node in enumerate(node.nodes("pad")):
            try:
                pads.append(self._pad(pad_node, pad_index, fp_id, reference, position, rotation))
            except _ItemError as exc:
                self._ctx.warn(f"skipped pad #{pad_index} of {reference}: {exc}", pad_node)

        attr = node.first("attr")
        attributes = tuple(attr.atoms()) if attr is not None else ()
        footprint = Footprint(
            id=fp_id,
            lib_id=lib_id,
            position=position,
            rotation_deg=rotation,
            side=side,
            pads=tuple(pads),
            local_bounds=self._footprint_local_bounds(node, pads, position, rotation),
            locked=node.has_flag("locked"),
            attributes=attributes,
        )
        return Component(
            reference=reference,
            value=value,
            footprint=footprint,
            properties=MappingProxyType(properties),
        )

    def _footprint_local_bounds(
        self, node: SNode, pads: list[Pad], position: Point, rotation: float
    ) -> BoundingBox | None:
        """Body extent in local coords: courtyard, else fab, else pads."""
        by_layer: dict[str, list[Point]] = {"CrtYd": [], "Fab": []}
        for g in node.children:
            if not isinstance(g, SNode) or g.name not in _FP_GRAPHICS:
                continue
            layer = _graphic_layer(g) or ""
            bucket = "CrtYd" if layer.endswith("CrtYd") else "Fab" if layer.endswith("Fab") else ""
            if not bucket:
                continue
            try:
                by_layer[bucket].extend(_graphic_points(g))
            except _ItemError:
                continue  # decorative graphics only affect the drawn body outline
        for bucket in ("CrtYd", "Fab"):
            box = BoundingBox.from_points(by_layer[bucket])
            if box is not None:
                return box
        # Fallback: pad extents transformed back into the local frame.
        local_pts: list[Point] = []
        for pad in pads:
            for corner in pad.bounds.corners:
                local = rotate_point(corner, -rotation, position) - position
                local_pts.append(local)
        return BoundingBox.from_points(local_pts)

    def _pad(
        self,
        node: SNode,
        index: int,
        fp_id: str,
        reference: str,
        fp_pos: Point,
        fp_rot: float,
    ) -> Pad:
        atoms = node.atoms()
        if len(atoms) < 3:
            raise _ItemError("expected number, type and shape")
        number, type_text, shape_text = atoms[0], atoms[1], atoms[2]
        local, pad_rot = _at(node)
        position = rotate_point(fp_pos + local, fp_rot, fp_pos)

        size_node = node.first("size")
        if size_node is None:
            raise _ItemError("missing size")
        width = _mm(size_node.atom(0), "pad width")
        height = _mm(size_node.atom(1), "pad height") if size_node.atom(1) else width

        drill: Nm | None = None
        drill_node = node.first("drill")
        if drill_node is not None:
            numeric = [a for a in drill_node.atoms() if a != "oval"]
            if numeric:
                drill = _mm(numeric[0], "drill")

        layers: list[str] = []
        layers_node = node.first("layers")
        for pattern in layers_node.atoms() if layers_node is not None else []:
            for name in expand_layer_pattern(pattern, self._ctx.copper_layers):
                if name not in layers:
                    layers.append(name)

        rratio_text = node.value("roundrect_rratio")
        return Pad(
            id=self._ctx.unique_id(_item_id(node), f"{fp_id}:pad{index}"),
            number=number,
            footprint_ref=reference,
            position=position,
            size=(width, height),
            shape=PadShape.parse(shape_text),
            pad_type=PadType.parse(type_text),
            layers=tuple(layers),
            net_name=self._net_of(node),
            rotation_deg=pad_rot,
            drill=drill,
            roundrect_ratio=_float(rratio_text, "roundrect ratio") if rratio_text else None,
        )

    # ------------------------------------------------------------ copper
    def _track(self, node: SNode, index: int) -> Track:
        arc = node.name == "arc"
        start = _point(node.first("start"), "start")
        end = _point(node.first("end"), "end")
        mid = _point(node.first("mid"), "mid") if arc else None
        layer = node.value("layer")
        if layer is None:
            raise _ItemError("missing layer")
        return Track(
            id=self._ctx.unique_id(_item_id(node), f"track:{index}"),
            start=start,
            end=end,
            width=_mm(node.value("width"), "width"),
            layer=layer,
            net_name=self._net_of(node),
            mid=mid,
            locked=node.has_flag("locked"),
        )

    def _via(self, node: SNode, index: int) -> Via:
        position, _ = _at(node)
        diameter = _mm(node.value("size"), "via size")
        drill_text = node.value("drill")
        layers = node.first("layers")
        layer_atoms = layers.atoms() if layers is not None else []
        flags = set(node.atoms())
        if "micro" in flags:
            via_type = ViaType.MICRO
        elif "blind" in flags:
            via_type = ViaType.BLIND_BURIED
        else:
            via_type = ViaType.THROUGH
        return Via(
            id=self._ctx.unique_id(_item_id(node), f"via:{index}"),
            position=position,
            diameter=diameter,
            drill=_mm(drill_text, "via drill") if drill_text is not None else None,
            net_name=self._net_of(node),
            start_layer=layer_atoms[0] if len(layer_atoms) >= 1 else None,
            end_layer=layer_atoms[1] if len(layer_atoms) >= 2 else None,
            via_type=via_type,
            locked=node.has_flag("locked"),
        )

    # ------------------------------------------------------------ outline
    def _outline_segments(self, node: SNode) -> Iterable[OutlineSegment]:
        width = _mm(node.value("width"), "width") if node.value("width") else 0
        stroke = node.first("stroke")
        if stroke is not None and stroke.value("width"):
            width = _mm(stroke.value("width"), "stroke width")
        kind = node.name[3:]
        if kind == "arc":
            start, mid, end = _arc_triplet(node)
            return [OutlineSegment(OutlineShape.ARC, start, end, mid, width)]
        if kind == "circle":
            center = _point(node.first("center") or node.first("start"), "center")
            return [
                OutlineSegment(
                    OutlineShape.CIRCLE, center, _point(node.first("end"), "end"), None, width
                )
            ]
        pts = _graphic_points(node)
        if kind == "curve":
            self._ctx.info("Bezier board-outline curve approximated by its control polygon")
        return [
            OutlineSegment(OutlineShape.LINE, a, b, None, width)
            for a, b in itertools.pairwise(pts)
            if a != b
        ]
