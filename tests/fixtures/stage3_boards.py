"""Generator for the Stage 3 fixture boards (run to regenerate; output is committed).

    python -m tests.fixtures.stage3_boards

Boards (all 2-layer, KiCad 8 format, coordinates in mm):

* ``stage3_rules``: net classes (.kicad_pro), custom rules (.kicad_dru), rotated /
  back-side / THT / SMD / NPTH / slotted pads, keepouts with different permissions,
  a board cutout, a GND zone fill, partially routed nets, and these deliberate
  violations (asserted by tests):

  - SIG2 track 0.25 mm from a VBAT (Power, 0.30 mm) track       -> clearance
  - SIG track 0.10 mm wide (board minimum 0.15 mm)              -> min width
  - GND track crossing the right board edge                      -> edge crossing
  - SIG track through keepout A (tracks forbidden)              -> track in keepout
  - GND via inside keepout B (vias forbidden)                    -> via in keepout
  - SIG via ~0.54 mm from the HV pad (custom rule: HV 1.0 mm)    -> via clearance
  - a GND via inside keepout A (vias allowed there)             -> *no* violation

* ``stage3_clean``: a small legal board (internal DRC passes).
* ``stage3_unsupported``: ``stage3_clean`` plus custom rules the engine cannot
  evaluate (an ``insideCourtyard()`` clearance and a disallow with an unsupported
  condition) -> RULE_UNKNOWN behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent / "boards"
_uid = 0


def uuid() -> str:
    global _uid
    _uid += 1
    return f"5a3e0000-0000-4000-8000-{_uid:012d}"


def n(v: float) -> str:
    text = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def header(nets: list[str], title: str) -> str:
    out = [
        "(kicad_pcb",
        "\t(version 20240108)",
        '\t(generator "pcbnew")',
        '\t(generator_version "8.0")',
        "\t(general\n\t\t(thickness 1.6)\n\t)",
        f'\t(title_block\n\t\t(title "{title}")\n\t)',
        "\t(layers",
        '\t\t(0 "F.Cu" signal)',
        '\t\t(31 "B.Cu" signal)',
        '\t\t(37 "F.SilkS" user)',
        '\t\t(44 "Edge.Cuts" user)',
        '\t\t(46 "B.CrtYd" user)',
        '\t\t(47 "F.CrtYd" user)',
        "\t)",
        '\t(net 0 "")',
    ]
    out += [f'\t(net {i} "{name}")' for i, name in enumerate(nets, start=1)]
    return "\n".join(out) + "\n"


class Board:
    def __init__(self, nets: list[str], title: str) -> None:
        self.nets = nets
        self.parts = [header(nets, title)]

    def net(self, name: str | None) -> str:
        if not name:
            return "(net 0)"
        return f'(net {self.nets.index(name) + 1} "{name}")'

    def rect_edge(self, x0: float, y0: float, x1: float, y1: float) -> None:
        self.parts.append(
            f"\t(gr_rect (start {n(x0)} {n(y0)}) (end {n(x1)} {n(y1)}) "
            f'(stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts") (uuid "{uuid()}"))\n'
        )

    def footprint(
        self,
        ref: str,
        lib: str,
        x: float,
        y: float,
        rot: float,
        pads: list[str],
        side: str = "F",
        court: tuple[float, float, float, float] | None = None,
    ) -> None:
        cy = ""
        if court is not None:
            cy = (
                f"\t\t(fp_rect (start {n(court[0])} {n(court[1])}) (end {n(court[2])} {n(court[3])}) "
                f'(stroke (width 0.05) (type default)) (fill none) (layer "{side}.CrtYd"))\n'
            )
        at = f"(at {n(x)} {n(y)}{' ' + n(rot) if rot else ''})"
        self.parts.append(
            f'\t(footprint "{lib}"\n\t\t(layer "{side}.Cu")\n\t\t(uuid "{uuid()}")\n\t\t{at}\n'
            f'\t\t(property "Reference" "{ref}")\n\t\t(property "Value" "{lib.split(":")[-1]}")\n'
            + cy
            + "".join(f"\t\t{p}\n" for p in pads)
            + "\t)\n"
        )

    def pad(
        self,
        num: str,
        kind: str,
        shape: str,
        x: float,
        y: float,
        rot: float,
        w: float,
        h: float,
        layers: str,
        net: str | None,
        extra: str = "",
    ) -> str:
        angle = f" {n(rot)}" if rot else ""
        return (
            f'(pad "{num}" {kind} {shape} (at {n(x)} {n(y)}{angle}) (size {n(w)} {n(h)}) '
            f'{extra}(layers {layers}) {self.net(net)} (uuid "{uuid()}"))'
        )

    def track(
        self, x0: float, y0: float, x1: float, y1: float, w: float, layer: str, net: str
    ) -> None:
        self.parts.append(
            f"\t(segment (start {n(x0)} {n(y0)}) (end {n(x1)} {n(y1)}) (width {n(w)}) "
            f'(layer "{layer}") {self.net(net)} (uuid "{uuid()}"))\n'
        )

    def via(self, x: float, y: float, dia: float, drill: float, net: str) -> None:
        self.parts.append(
            f'\t(via (at {n(x)} {n(y)}) (size {n(dia)}) (drill {n(drill)}) (layers "F.Cu" "B.Cu") '
            f'{self.net(net)} (uuid "{uuid()}"))\n'
        )

    def keepout(
        self, name: str, x0: float, y0: float, x1: float, y1: float, tracks: bool, vias: bool
    ) -> None:
        def flag(b: bool) -> str:
            return "not_allowed" if b else "allowed"

        self.parts.append(
            f'\t(zone (net 0) (net_name "") (layers "F.Cu" "B.Cu") (uuid "{uuid()}") (name "{name}")\n'
            f"\t\t(hatch edge 0.5)\n\t\t(connect_pads (clearance 0))\n\t\t(min_thickness 0.25)\n"
            f"\t\t(keepout (tracks {flag(tracks)}) (vias {flag(vias)}) (pads allowed) "
            f"(copperpour not_allowed) (footprints allowed))\n"
            f"\t\t(fill (thermal_gap 0.5) (thermal_bridge_width 0.5))\n"
            f"\t\t(polygon (pts (xy {n(x0)} {n(y0)}) (xy {n(x1)} {n(y0)}) (xy {n(x1)} {n(y1)}) (xy {n(x0)} {n(y1)})))\n"
            "\t)\n"
        )

    def zone_fill(self, net: str, layer: str, x0: float, y0: float, x1: float, y1: float) -> None:
        pts = f"(xy {n(x0)} {n(y0)}) (xy {n(x1)} {n(y0)}) (xy {n(x1)} {n(y1)}) (xy {n(x0)} {n(y1)})"
        self.parts.append(
            f'\t(zone {self.net(net)} (net_name "{net}") (layer "{layer}") (uuid "{uuid()}") (name "{net}_pour")\n'
            f"\t\t(hatch edge 0.5)\n\t\t(connect_pads (clearance 0.3))\n\t\t(min_thickness 0.25)\n"
            f"\t\t(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))\n"
            f"\t\t(polygon (pts {pts}))\n"
            f'\t\t(filled_polygon (layer "{layer}") (pts {pts}))\n'
            "\t)\n"
        )

    def text(self) -> str:
        return "".join(self.parts) + ")\n"


THT = '"*.Cu" "*.Mask"'
SMD_F = '"F.Cu" "F.Paste" "F.Mask"'
SMD_B = '"B.Cu" "B.Paste" "B.Mask"'


def rules_board() -> str:
    b = Board(["GND", "VBAT", "/CAN_H", "/CAN_L", "SIG", "SIG2", "HV"], "Stage 3 rules fixture")
    b.rect_edge(0, 0, 40, 30)
    b.rect_edge(30, 20, 36, 26)  # internal cutout
    b.footprint(
        "J1",
        "Connector:Conn_01x03",
        5,
        15,
        0,
        [
            b.pad("1", "thru_hole", "circle", 0, -2.54, 0, 1.7, 1.7, THT, "/CAN_H", "(drill 1) "),
            b.pad("2", "thru_hole", "circle", 0, 0, 0, 1.7, 1.7, THT, "/CAN_L", "(drill 1) "),
            b.pad("3", "thru_hole", "circle", 0, 2.54, 0, 1.7, 1.7, THT, "GND", "(drill 1) "),
        ],
        court=(-1.5, -4, 1.5, 4),
    )
    b.footprint(
        "U1",
        "Package_SO:SO-4",
        20,
        15,
        90,
        [
            b.pad("1", "smd", "rect", -1.27, -2.5, 90, 0.6, 1.2, SMD_F, "/CAN_H"),
            b.pad("2", "smd", "rect", 1.27, -2.5, 90, 0.6, 1.2, SMD_F, "/CAN_L"),
            b.pad("3", "smd", "rect", 1.27, 2.5, 90, 0.6, 1.2, SMD_F, "VBAT"),
            b.pad("4", "smd", "rect", -1.27, 2.5, 90, 0.6, 1.2, SMD_F, "GND"),
        ],
        court=(-2, -3.5, 2, 3.5),
    )
    b.footprint(
        "R1",
        "Resistor_SMD:R_0603",
        28,
        8,
        45,
        [
            b.pad(
                "1",
                "smd",
                "roundrect",
                -0.8,
                0,
                45,
                0.8,
                0.95,
                SMD_F,
                "VBAT",
                "(roundrect_rratio 0.25) ",
            ),
            b.pad(
                "2",
                "smd",
                "roundrect",
                0.8,
                0,
                45,
                0.8,
                0.95,
                SMD_F,
                "SIG",
                "(roundrect_rratio 0.25) ",
            ),
        ],
        court=(-1.5, -0.8, 1.5, 0.8),
    )
    b.footprint(
        "U2",
        "Custom:Back_Oval",
        10,
        25,
        180,
        [
            b.pad("1", "smd", "oval", -1.5, 0, 180, 1.5, 0.8, SMD_B, "SIG2"),
            b.pad("2", "smd", "oval", 1.5, 0, 180, 1.5, 0.8, SMD_B, "HV"),
        ],
        side="B",
        court=(-2.5, -1, 2.5, 1),
    )
    b.footprint(
        "U3",
        "Connector:HV_Slot",
        33,
        15,
        0,
        [
            b.pad("1", "thru_hole", "oval", 0, 0, 0, 2, 1.5, THT, "HV", "(drill oval 1.2 0.8) "),
            b.pad("2", "thru_hole", "circle", 0, 3.5, 0, 1.7, 1.7, THT, "GND", "(drill 1) "),
        ],
        court=(-1.5, -1.2, 1.5, 4.7),
    )
    b.footprint(
        "H1",
        "MountingHole:MountingHole_3.2mm",
        36,
        4,
        0,
        [
            b.pad("", "np_thru_hole", "circle", 0, 0, 0, 3.2, 3.2, THT, None, "(drill 3.2) "),
        ],
    )
    # CAN_H: J1.1 -> dangling stub (U1.1 not reached): partially connected.
    b.track(5, 12.46, 10, 12.46, 0.25, "F.Cu", "/CAN_H")
    # CAN_L: J1.2 -> via -> B.Cu -> via -> U1.2: fully connected.
    b.track(5, 15, 12, 15, 0.25, "F.Cu", "/CAN_L")
    b.via(12, 15, 0.6, 0.3, "/CAN_L")
    b.track(12, 15, 15, 15, 0.25, "B.Cu", "/CAN_L")
    b.via(15, 15, 0.6, 0.3, "/CAN_L")
    b.track(15, 15, 17.5, 13.73, 0.25, "F.Cu", "/CAN_L")
    # VBAT (Power, 0.5 mm): U1.3 -> R1.1.
    b.track(22.5, 13.73, 22.5, 11, 0.5, "F.Cu", "VBAT")
    b.track(22.5, 11, 27.4343, 8.5657, 0.5, "F.Cu", "VBAT")
    # SIG: R1.2 -> right; a 0.10 mm segment (min width violation).
    b.track(28.5657, 7.4343, 34, 7.4343, 0.2, "F.Cu", "SIG")
    b.track(34, 7.4343, 34, 10, 0.1, "F.Cu", "SIG")
    # SIG2: 0.25 mm from the VBAT track (Power clearance 0.30 mm): violation.
    b.track(23.1, 11.6, 23.1, 12.8, 0.2, "F.Cu", "SIG2")
    # GND: pour on B.Cu under J1.3, via, track to U1.4 (U3.2 stays unconnected).
    b.zone_fill("GND", "B.Cu", 2, 16.4, 9, 22)
    b.via(7, 20, 0.8, 0.4, "GND")
    b.track(7, 20, 22.5, 16.27, 0.5, "F.Cu", "GND")
    # GND track crossing the right board edge (x = 40).
    b.track(38, 28, 41, 28, 0.25, "F.Cu", "GND")
    # Keepout A: tracks forbidden, vias allowed. Keepout B: tracks and vias forbidden.
    b.keepout("A tracks-only", 25, 24, 29, 28, tracks=True, vias=False)
    b.keepout("B tracks+vias", 2, 24, 6, 28, tracks=True, vias=True)
    b.track(24, 26, 29.5, 26, 0.2, "F.Cu", "SIG")  # through keepout A
    b.via(26, 27, 0.8, 0.4, "GND")  # inside A (allowed)
    b.via(4, 26, 0.8, 0.4, "GND")  # inside B (forbidden)
    # SIG via ~0.54 mm from the HV pad (custom rule requires 1.0 mm).
    b.via(34, 13.6, 0.6, 0.3, "SIG")
    return b.text()


def rules_project() -> dict[str, object]:
    return {
        "board": {
            "design_settings": {
                "rules": {
                    "min_clearance": 0.0,
                    "min_track_width": 0.15,
                    "min_via_diameter": 0.4,
                    "min_through_hole_diameter": 0.2,
                    "min_via_annular_width": 0.1,
                    "min_copper_edge_clearance": 0.3,
                    "min_hole_clearance": 0.25,
                    "min_hole_to_hole": 0.25,
                }
            }
        },
        "net_settings": {
            "classes": [
                {
                    "name": "Default",
                    "clearance": 0.2,
                    "track_width": 0.2,
                    "via_diameter": 0.6,
                    "via_drill": 0.3,
                },
                {
                    "name": "CAN",
                    "clearance": 0.2,
                    "track_width": 0.25,
                    "via_diameter": 0.6,
                    "via_drill": 0.3,
                },
                {
                    "name": "Power",
                    "clearance": 0.3,
                    "track_width": 0.5,
                    "via_diameter": 0.8,
                    "via_drill": 0.4,
                },
                {"name": "Sparse", "clearance": 0.4},
            ],
            "meta": {"version": 3},
            "netclass_patterns": [{"netclass": "CAN", "pattern": "/CAN_*"}],
            "netclass_assignments": {"VBAT": "Power", "GND": "Power"},
        },
    }


RULES_DRU = """(version 1)
(rule "HV clearance"
\t(constraint clearance (min 1.0mm))
\t(condition "A.NetName == 'HV' || B.NetName == 'HV'"))
(rule "CAN width"
\t(constraint track_width (min 0.2mm) (opt 0.25mm))
\t(condition "A.NetClass == 'CAN'"))
(rule "No vias on SIG2"
\t(constraint disallow via)
\t(condition "A.NetName == 'SIG2'"))
(rule "Silk"
\t(constraint silk_clearance (min 0.1mm)))
(rule "Disabled"
\t(severity ignore)
\t(constraint clearance (min 5mm)))
"""

UNSUPPORTED_DRU = """(version 1)
(rule "Courtyard keep"
\t(constraint clearance (min 0.5mm))
\t(condition "A.insideCourtyard('R1')"))
(rule "No vias near RF"
\t(constraint disallow via)
\t(condition "A.intersectsArea('RF')"))
"""


def clean_board(title: str = "Stage 3 clean fixture") -> str:
    b = Board(["A", "B", "GND"], title)
    b.rect_edge(0, 0, 20, 15)
    for ref, x, left, right in (("R1", 5, "B", "A"), ("R2", 15, "A", "GND")):
        b.footprint(
            ref,
            "Resistor_SMD:R_0603",
            x,
            7.5,
            0,
            [
                b.pad(
                    "1",
                    "smd",
                    "roundrect",
                    -0.8,
                    0,
                    0,
                    0.8,
                    0.95,
                    SMD_F,
                    left,
                    "(roundrect_rratio 0.25) ",
                ),
                b.pad(
                    "2",
                    "smd",
                    "roundrect",
                    0.8,
                    0,
                    0,
                    0.8,
                    0.95,
                    SMD_F,
                    right,
                    "(roundrect_rratio 0.25) ",
                ),
            ],
            court=(-1.5, -0.8, 1.5, 0.8),
        )
    b.track(5.8, 7.5, 10, 7.5, 0.25, "F.Cu", "A")
    b.via(10, 7.5, 0.6, 0.3, "A")
    b.track(10, 7.5, 14.2, 7.5, 0.25, "B.Cu", "A")
    b.via(14.2, 7.5, 0.6, 0.3, "A")
    return b.text()


def clean_project() -> dict[str, object]:
    return {
        "board": {
            "design_settings": {
                "rules": {
                    "min_clearance": 0.0,
                    "min_track_width": 0.15,
                    "min_via_diameter": 0.4,
                    "min_through_hole_diameter": 0.2,
                    "min_copper_edge_clearance": 0.3,
                    "min_hole_clearance": 0.25,
                    "min_hole_to_hole": 0.25,
                    "min_via_annular_width": 0.1,
                }
            }
        },
        "net_settings": {
            "classes": [
                {
                    "name": "Default",
                    "clearance": 0.2,
                    "track_width": 0.25,
                    "via_diameter": 0.6,
                    "via_drill": 0.3,
                },
            ],
            "meta": {"version": 3},
        },
    }


def main() -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / "stage3_rules.kicad_pcb").write_text(rules_board())
    (OUT / "stage3_rules.kicad_pro").write_text(json.dumps(rules_project(), indent=2) + "\n")
    (OUT / "stage3_rules.kicad_dru").write_text(RULES_DRU)
    (OUT / "stage3_clean.kicad_pcb").write_text(clean_board())
    (OUT / "stage3_clean.kicad_pro").write_text(json.dumps(clean_project(), indent=2) + "\n")
    (OUT / "stage3_unsupported.kicad_pcb").write_text(
        clean_board("Stage 3 unsupported-rule fixture")
    )
    (OUT / "stage3_unsupported.kicad_pro").write_text(json.dumps(clean_project(), indent=2) + "\n")
    (OUT / "stage3_unsupported.kicad_dru").write_text(UNSUPPORTED_DRU)


if __name__ == "__main__":
    main()
