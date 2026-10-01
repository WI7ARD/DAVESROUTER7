"""Tiny KiCad 8 boards for rule-fidelity tests.

Each board holds a horizontal track of net ``net_a`` and one other copper item of
net ``net_b`` at an exact edge-to-edge ``gap_mm``, plus an optional
``.kicad_dru``. With the default class clearance (0.2 mm) below the gap, a
custom rule raising the clearance above it produces a violation exactly when
its condition matches the pair: the board turns a rule condition into a yes/no
question that both our engine and KiCad DRC answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

WIDTH = 0.25
CLASS_CLEARANCE = 0.2
Y = 10.0


@dataclass(frozen=True)
class Item:
    """The second copper item: ``track`` | ``via`` | ``smd`` | ``pth`` | ``npth``."""

    kind: str = "track"
    layer: str = "F.Cu"


def _uuid(n: int) -> str:
    return f"7e570000-0000-4000-8000-{n:012d}"


def _pro(classes: dict[str, list[str]]) -> str:
    patterns = [{"netclass": cls, "pattern": net} for cls, nets in classes.items() for net in nets]
    rows = [
        {"name": "Default", "clearance": CLASS_CLEARANCE, "track_width": WIDTH,
         "via_diameter": 0.6, "via_drill": 0.3},
    ] + [
        {"name": cls, "clearance": CLASS_CLEARANCE, "track_width": WIDTH,
         "via_diameter": 0.6, "via_drill": 0.3}
        for cls in classes
    ]  # fmt: skip
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
                        "min_hole_clearance": 0.1,
                        "min_hole_to_hole": 0.25,
                        "min_via_annular_width": 0.1,
                    }
                }
            },
            "net_settings": {
                "classes": rows,
                "netclass_patterns": patterns,
                "meta": {"version": 3},
            },
        },
        indent=2,
    )


def _pad(kind: str, n: int, x: float, y: float, net: tuple[int, str]) -> str:
    size = 1.0
    if kind == "smd":
        body = f'smd rect (at 0 0) (size {size} {size}) (layers "F.Cu" "F.Paste" "F.Mask")'
    elif kind == "pth":
        body = (
            f'thru_hole circle (at 0 0) (size {size} {size}) (drill 0.5) (layers "*.Cu" "*.Mask")'
        )
    else:  # npth: no copper, a bare hole
        body = (
            f"np_thru_hole circle (at 0 0) (size {size} {size}) (drill {size}) "
            '(layers "*.Cu" "*.Mask")'
        )
    net_part = "" if kind == "npth" else f' (net {net[0]} "{net[1]}")'
    return (
        f'\t(footprint "Test:P" (layer "F.Cu") (uuid "{_uuid(n)}") (at {x} {y})\n'
        f'\t\t(property "Reference" "P{n}") (property "Value" "P")\n'
        f'\t\t(pad "1" {body}{net_part} (uuid "{_uuid(n + 1)}"))\n\t)\n'
    )


def board(
    folder: Path,
    *,
    name: str = "fid",
    net_a: str = "/SIG",
    net_b: str = "GND",
    gap_mm: float = 0.3,
    item: Item | None = None,
    dru: str | None = None,
    classes: dict[str, list[str]] | None = None,
    extra_nets: tuple[str, ...] = (),
) -> Path:
    """Write ``name.kicad_pcb`` / ``.kicad_pro`` (/ ``.kicad_dru``) and return the board."""
    item = item or Item()
    folder.mkdir(parents=True, exist_ok=True)
    nets = [net_a, net_b, *extra_nets]
    code = {n: i + 1 for i, n in enumerate(nets)}
    lines = [
        "(kicad_pcb",
        "\t(version 20240108)",
        '\t(generator "pcbnew")',
        '\t(generator_version "8.0")',
        "\t(general (thickness 1.6))",
        '\t(paper "A4")',
        "\t(layers",
        '\t\t(0 "F.Cu" signal)',
        '\t\t(31 "B.Cu" signal)',
        '\t\t(38 "B.Mask" user)',
        '\t\t(39 "F.Mask" user)',
        '\t\t(44 "Edge.Cuts" user)',
        "\t)",
        "\t(setup (pad_to_mask_clearance 0))",
        '\t(net 0 "")',
        *[f'\t(net {code[n]} "{n}")' for n in nets],
        f"\t(gr_rect (start 0 0) (end 30 20) (stroke (width 0.05) (type default)) "
        f'(fill none) (layer "Edge.Cuts") (uuid "{_uuid(1)}"))',
    ]
    a = code[net_a]
    lines.append(
        f'\t(segment (start 5 {Y}) (end 25 {Y}) (width {WIDTH}) (layer "F.Cu") '
        f'(net {a}) (uuid "{_uuid(2)}"))'
    )
    b = code[net_b]
    edge = Y + WIDTH / 2 + gap_mm  # nearest point of item B
    if item.kind == "track":
        yb = edge + WIDTH / 2
        lines.append(
            f"\t(segment (start 5 {yb:.4f}) (end 25 {yb:.4f}) (width {WIDTH}) "
            f'(layer "{item.layer}") (net {b}) (uuid "{_uuid(3)}"))'
        )
    elif item.kind == "via":
        yb = edge + 0.3
        lines.append(
            f'\t(via (at 15 {yb:.4f}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
            f'(net {b}) (uuid "{_uuid(3)}"))'
        )
    else:
        lines.append(_pad(item.kind, 10, 15.0, round(edge + 0.5, 4), (b, net_b)))
    lines.append(")")
    path = folder / f"{name}.kicad_pcb"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.with_suffix(".kicad_pro").write_text(_pro(classes or {}), encoding="utf-8")
    if dru is not None:
        path.with_suffix(".kicad_dru").write_text(dru, encoding="utf-8")
    return path


def clearance_rule(condition: str, min_mm: float = 0.5, kind: str = "clearance") -> str:
    """A ``.kicad_dru`` with one rule raising ``kind`` to *min_mm* under *condition*."""
    cond = condition.replace('"', '\\"')
    return (
        "(version 1)\n"
        f'(rule "fidelity" (constraint {kind} (min {min_mm}mm)) (condition "{cond}"))\n'
    )


def ours_flags(path: Path) -> bool:
    """Does our engine report a clearance-type error between the two items?"""
    from pcbrouter.kicad.loader import load_board
    from pcbrouter.kicad.rule_adapter import load_project_rules
    from pcbrouter.routing.working_board import WorkingBoard

    wb = WorkingBoard(load_board(path).board, load_project_rules(path))
    clearance = ("clearance", "hole")
    return any(any(word in v.kind.value for word in clearance) for v in wb.engine.run_drc().errors)


def kicad_flags(tool: object, path: Path) -> bool:
    """Does KiCad DRC report a clearance-type error on this board?"""
    from pcbrouter.kicad import oracle

    assert isinstance(tool, oracle.OracleTool)
    run = oracle.run_drc(tool, path, "as_exported")
    assert run.ok, run.message
    return any(
        v.severity == "error" and v.type in ("clearance", "hole_clearance") for v in run.violations
    )
