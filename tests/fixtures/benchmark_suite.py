"""Deterministic benchmark boards (MIT, generated — no third-party designs).

    tiny              router_basic fixture (5 nets, 2 layers)
    small_2layer      router_dense fixture (11 nets, 2 layers)
    medium_2layer     headers + DIPs, ~40 crossing nets, 100 x 70 mm, 2 layers
    dense_2layer      same parts squeezed into 70 x 50 mm, ~60 nets, 2 layers
    small_4layer      router_4layer fixture (4 layers)
    medium_4layer     QFP-64 (0.5 mm pitch) + headers + DIP, ~60 nets, 4 layers
    impossible        one net whose pad is walled in by keepouts on every layer

``write_suite(folder)`` writes each generated board with its ``.kicad_pro``.
Rules are explicit net classes (nothing is guessed by the router).
"""

from __future__ import annotations

import json
import random
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

FIXTURES = Path(__file__).parent / "boards"
_NS = uuid.UUID("7d1f3a2e-5b64-4c0a-9e1b-2f6a8c9d0e11")


def _uid(*parts: object) -> str:
    return str(uuid.uuid5(_NS, "/".join(str(p) for p in parts)))


@dataclass
class _Pad:
    ref: str
    number: str
    x: float  # relative to footprint
    y: float
    smd: bool
    w: float
    h: float
    net: str | None = None


@dataclass
class _Part:
    ref: str
    x: float
    y: float
    pads: list[_Pad] = field(default_factory=list)


def header(ref: str, x: float, y: float, cols: int, rows: int, pitch: float = 2.54) -> _Part:
    p = _Part(ref, x, y)
    n = 1
    for r in range(rows):
        for c in range(cols):
            p.pads.append(_Pad(ref, str(n), c * pitch, r * pitch, False, 1.7, 1.7))
            n += 1
    return p


def dip(ref: str, x: float, y: float, pins: int, row: float = 7.62) -> _Part:
    p = _Part(ref, x, y)
    half = pins // 2
    for i in range(half):
        p.pads.append(_Pad(ref, str(i + 1), 0.0, i * 2.54, False, 1.6, 1.6))
    for i in range(half):
        p.pads.append(_Pad(ref, str(half + i + 1), row, (half - 1 - i) * 2.54, False, 1.6, 1.6))
    return p


def qfp(ref: str, x: float, y: float, per_side: int, pitch: float = 0.5) -> _Part:
    """Square QFP: pads 0.3 x 1.5 mm, rows 1.6 mm outside the body edge."""
    p = _Part(ref, x, y)
    span = (per_side - 1) * pitch
    off = span / 2 + 1.6
    n = 1
    for i in range(per_side):  # bottom (left -> right)
        p.pads.append(_Pad(ref, str(n), -span / 2 + i * pitch, off, True, 0.3, 1.5))
        n += 1
    for i in range(per_side):  # right (bottom -> top)
        p.pads.append(_Pad(ref, str(n), off, span / 2 - i * pitch, True, 1.5, 0.3))
        n += 1
    for i in range(per_side):  # top (right -> left)
        p.pads.append(_Pad(ref, str(n), span / 2 - i * pitch, -off, True, 0.3, 1.5))
        n += 1
    for i in range(per_side):  # left (top -> bottom)
        p.pads.append(_Pad(ref, str(n), -off, -span / 2 + i * pitch, True, 1.5, 0.3))
        n += 1
    return p


def _assign_nets(parts: list[_Part], n_nets: int, seed: int, power: int = 2) -> list[str]:
    """Deterministic nets of 2-4 pads spread across different parts (crossings)."""
    rng = random.Random(seed)
    free = [pad for part in parts for pad in part.pads]
    rng.shuffle(free)
    names: list[str] = []
    for k in range(power):  # power nets: several pads each
        name = ("GND", "+3V3", "+5V")[k]
        for pad in free[:6]:
            pad.net = name
        free = free[6:]
        names.append(name)
    for i in range(n_nets):
        size = rng.choice((2, 2, 2, 3, 3, 4))
        members: list[_Pad] = []
        refs: set[str] = set()
        for pad in list(free):
            if pad.ref not in refs:
                members.append(pad)
                refs.add(pad.ref)
                free.remove(pad)
            if len(members) == size:
                break
        if len(members) < 2:
            break
        name = f"SIG{i + 1:02d}"
        for pad in members:
            pad.net = name
        names.append(name)
    return names


Box = tuple[float, float, float, float]


def _board_text(
    title: str,
    layers: list[str],
    w: float,
    h: float,
    parts: list[_Part],
    nets: list[str],
    keepouts: list[Box] | tuple[()] = (),
) -> str:
    codes = {n: i + 1 for i, n in enumerate(nets)}
    layer_ids = {"F.Cu": 0, "In1.Cu": 1, "In2.Cu": 2, "B.Cu": 31}
    out = [
        "(kicad_pcb",
        "\t(version 20240108)",
        '\t(generator "pcbnew")',
        '\t(generator_version "8.0")',
        "\t(general (thickness 1.6))",
        f'\t(title_block (title "{title}") (company "AI PCB Router benchmark suite (MIT)"))',
        "\t(layers",
    ]
    out += [f'\t\t({layer_ids[layer]} "{layer}" signal)' for layer in layers]
    out += ['\t\t(37 "F.SilkS" user)', '\t\t(44 "Edge.Cuts" user)', "\t)", '\t(net 0 "")']
    out += [f'\t(net {codes[n]} "{n}")' for n in nets]
    out.append(
        f"\t(gr_rect (start 0 0) (end {w} {h}) (stroke (width 0.1) (type default)) (fill none) "
        f'(layer "Edge.Cuts") (uuid "{_uid(title, "edge")}"))'
    )
    cu = " ".join(f'"{layer}"' for layer in layers)
    for i, (x0, y0, x1, y1) in enumerate(keepouts):
        out.append(
            f'\t(zone (net 0) (net_name "") (layers {cu}) (uuid "{_uid(title, "ko", i)}") '
            f'(name "wall {i}")\n\t\t(hatch edge 0.5)\n\t\t(connect_pads (clearance 0))\n'
            "\t\t(min_thickness 0.25)\n\t\t(keepout (tracks not_allowed) (vias not_allowed) "
            "(pads allowed) (copperpour not_allowed) (footprints allowed))\n"
            "\t\t(fill (thermal_gap 0.5) (thermal_bridge_width 0.5))\n"
            f"\t\t(polygon (pts (xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1})))\n\t)"
        )
    for part in parts:
        out.append(
            f'\t(footprint "Bench:{part.ref}"\n\t\t(layer "F.Cu")\n'
            f'\t\t(uuid "{_uid(title, part.ref)}")\n'
            f"\t\t(at {part.x:.3f} {part.y:.3f})\n"
            f'\t\t(property "Reference" "{part.ref}")\n\t\t(property "Value" "BENCH")'
        )
        for pad in part.pads:
            net = f' (net {codes[pad.net]} "{pad.net}")' if pad.net else ""
            if pad.smd:
                out.append(
                    f'\t\t(pad "{pad.number}" smd roundrect (at {pad.x:.3f} {pad.y:.3f}) '
                    f'(size {pad.w} {pad.h}) (layers "F.Cu" "F.Paste" "F.Mask") '
                    f'(roundrect_rratio 0.25){net} (uuid "{_uid(title, part.ref, pad.number)}"))'
                )
            else:
                out.append(
                    f'\t\t(pad "{pad.number}" thru_hole circle (at {pad.x:.3f} {pad.y:.3f}) '
                    f'(size {pad.w} {pad.h}) (drill 1.0) (layers "*.Cu" "*.Mask"){net} '
                    f'(uuid "{_uid(title, part.ref, pad.number)}"))'
                )
        out.append("\t)")
    out.append(")")
    return "\n".join(out) + "\n"


def _project(track: float, clearance: float, via: float, drill: float) -> str:
    return json.dumps(
        {
            "board": {
                "design_settings": {
                    "rules": {
                        "min_clearance": 0.0,
                        "min_track_width": 0.1,
                        "min_via_diameter": 0.4,
                        "min_through_hole_diameter": 0.2,
                        "min_copper_edge_clearance": 0.3,
                        "min_hole_clearance": 0.2,
                        "min_hole_to_hole": 0.25,
                        "min_via_annular_width": 0.1,
                    }
                }
            },
            "net_settings": {
                "classes": [
                    {
                        "name": "Default",
                        "clearance": clearance,
                        "track_width": track,
                        "via_diameter": via,
                        "via_drill": drill,
                    }
                ],
                "netclass_assignments": {},
                "meta": {"version": 3},
            },
        },
        indent=2,
    )


def medium_2layer() -> tuple[str, str]:
    parts = [header("J1", 6, 12, 2, 10), dip("U1", 30, 14, 24), dip("U2", 60, 14, 24),
             header("J2", 90, 12, 2, 10), header("J3", 36, 60, 10, 1)]  # fmt: skip
    nets = _assign_nets(parts, 40, seed=11)
    return (_board_text("Bench medium 2-layer", ["F.Cu", "B.Cu"], 100, 70, parts, nets),
            _project(0.25, 0.2, 0.6, 0.3))  # fmt: skip


def dense_2layer() -> tuple[str, str]:
    parts = [header("J1", 4, 6, 2, 10), dip("U1", 20, 8, 24), dip("U2", 38, 8, 24),
             header("J2", 60, 6, 2, 10), header("J3", 22, 45, 12, 1)]  # fmt: skip
    nets = _assign_nets(parts, 60, seed=23)
    return (_board_text("Bench dense 2-layer", ["F.Cu", "B.Cu"], 70, 50, parts, nets),
            _project(0.2, 0.15, 0.6, 0.3))  # fmt: skip


def medium_4layer() -> tuple[str, str]:
    parts = [qfp("U1", 40, 32, 16), header("J1", 6, 10, 2, 12), header("J2", 70, 10, 2, 12),
             dip("U2", 20, 45, 16), header("J3", 50, 58, 10, 1)]  # fmt: skip
    nets = _assign_nets(parts, 60, seed=37)
    layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    return (_board_text("Bench medium 4-layer", layers, 80, 66, parts, nets),
            _project(0.15, 0.15, 0.5, 0.25))  # fmt: skip


def impossible() -> tuple[str, str]:
    a, b = header("J1", 10, 10, 1, 1), header("J2", 40, 10, 1, 1)
    a.pads[0].net = b.pads[0].net = "TRAPPED"
    walls = [(7, 7, 13, 8), (7, 12, 13, 13), (7, 7, 8, 13), (12, 7, 13, 13)]  # box around J1
    return (_board_text("Bench impossible", ["F.Cu", "B.Cu"], 50, 20, [a, b], ["TRAPPED"],
                        walls),
            _project(0.25, 0.2, 0.6, 0.3))  # fmt: skip


GENERATED = {
    "medium_2layer": medium_2layer,
    "dense_2layer": dense_2layer,
    "medium_4layer": medium_4layer,
    "impossible": impossible,
}
FIXTURE_BOARDS = {"tiny": "router_basic", "small_2layer": "router_dense",
                  "small_4layer": "router_4layer"}  # fmt: skip
ORDER = ["tiny", "small_2layer", "medium_2layer", "dense_2layer", "small_4layer",
         "medium_4layer", "impossible"]  # fmt: skip


def write_suite(folder: Path) -> dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for name in ORDER:
        target = folder / f"{name}.kicad_pcb"
        if name in FIXTURE_BOARDS:
            src = FIXTURES / FIXTURE_BOARDS[name]
            shutil.copyfile(src.with_suffix(".kicad_pcb"), target)
            shutil.copyfile(src.with_suffix(".kicad_pro"), target.with_suffix(".kicad_pro"))
        else:
            board, project = GENERATED[name]()
            target.write_text(board, encoding="utf-8")
            target.with_suffix(".kicad_pro").write_text(project, encoding="utf-8")
        out[name] = target
    return out
