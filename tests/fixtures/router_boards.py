"""Generator for the router fixture boards (Stages 4-5; output is committed).

    python -m tests.fixtures.router_boards

* ``router_basic`` (2 layers, 30 x 20 mm):
  - a keepout strip on **F.Cu only** (tracks forbidden, vias allowed) splits the
    board at x = 14.5..15.5 → net ``A`` (SMD pads on F.Cu, left and right) can only
    connect through two vias and B.Cu;
  - net ``B``: a keepout (tracks + vias forbidden, both layers) sits between its
    pads → detour required;
  - nets ``C`` and ``D`` (THT pads) cross each other → one uses the other layer;
  - net ``E`` has three pads (two connections);
  - net ``G`` is already routed in the file (source copper, must be preserved).
* ``router_dense`` (2 layers, 40 x 30 mm): 8 signals from a left header to a right
  header in reversed order (every pair crosses), a differential pair
  ``USB_P``/``USB_N``, a wide ``VBUS`` power net (net class Power, 0.6 mm), and a
  locked source track on ``LOCKED``.
* ``router_4layer`` (4 layers): nets that need inner layers under a full-height
  F.Cu + B.Cu keepout band.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.fixtures.stage3_boards import SMD_F, THT, Board, n, uuid

OUT = Path(__file__).parent / "boards"


class RBoard(Board):
    def keepout_on(
        self,
        name: str,
        layers: str,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        tracks: bool,
        vias: bool,
    ) -> None:

        def flag(b: bool) -> str:
            return "not_allowed" if b else "allowed"

        pts = f"(xy {n(x0)} {n(y0)}) (xy {n(x1)} {n(y0)}) (xy {n(x1)} {n(y1)}) (xy {n(x0)} {n(y1)})"
        self.parts.append(
            f'\t(zone (net 0) (net_name "") (layers {layers}) (uuid "{uuid()}") (name "{name}")\n'
            "\t\t(hatch edge 0.5)\n\t\t(connect_pads (clearance 0))\n\t\t(min_thickness 0.25)\n"
            f"\t\t(keepout (tracks {flag(tracks)}) (vias {flag(vias)}) (pads allowed) "
            "(copperpour not_allowed) (footprints allowed))\n"
            "\t\t(fill (thermal_gap 0.5) (thermal_bridge_width 0.5))\n"
            f"\t\t(polygon (pts {pts}))\n\t)\n"
        )

    def test_point(
        self, ref: str, x: float, y: float, net: str, tht: bool = False, size: float = 1.2
    ) -> None:
        if tht:
            pad = self.pad("1", "thru_hole", "circle", 0, 0, 0, 1.6, 1.6, THT, net, "(drill 0.9) ")
        else:
            pad = self.pad("1", "smd", "rect", 0, 0, 0, size, size, SMD_F, net)
        self.footprint(ref, "TestPoint:TP", x, y, 0, [pad])

    def locked_track(
        self, x0: float, y0: float, x1: float, y1: float, w: float, layer: str, net: str
    ) -> None:
        self.parts.append(
            f"\t(segment (start {n(x0)} {n(y0)}) (end {n(x1)} {n(y1)}) (width {n(w)}) "
            f'(locked yes) (layer "{layer}") {self.net(net)} (uuid "{uuid()}"))\n'
        )


def project(
    classes: list[dict[str, object]] | None = None, assignments: dict[str, str] | None = None
) -> dict[str, object]:
    default = {
        "name": "Default",
        "clearance": 0.2,
        "track_width": 0.25,
        "via_diameter": 0.6,
        "via_drill": 0.3,
    }
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
            "classes": [default, *(classes or [])],
            "netclass_assignments": assignments or {},
            "meta": {"version": 3},
        },
    }


def router_basic() -> str:
    b = RBoard(["A", "B", "C", "D", "E", "G"], "Router basic fixture")
    b.rect_edge(0, 0, 30, 20)
    b.keepout_on("F.Cu wall", '"F.Cu"', 14.5, -1, 15.5, 21, tracks=True, vias=False)
    b.test_point("TP1", 5, 10, "A")
    b.test_point("TP2", 25, 10, "A")
    b.keepout_on("B block", '"F.Cu" "B.Cu"', 7.5, 2.5, 9.5, 5.5, tracks=True, vias=True)
    b.test_point("TP3", 5, 4, "B")
    b.test_point("TP4", 12, 4, "B")
    b.test_point("TP5", 4, 16, "C", tht=True)
    b.test_point("TP6", 12, 16, "C", tht=True)
    b.test_point("TP7", 8, 13, "D", tht=True)
    b.test_point("TP8", 8, 19, "D", tht=True)
    b.test_point("TP9", 20, 4, "E")
    b.test_point("TP10", 26, 4, "E")
    b.test_point("TP11", 23, 7, "E")
    b.test_point("TP12", 18, 14, "G")
    b.test_point("TP13", 27, 14, "G")
    b.track(18, 14, 27, 14, 0.25, "F.Cu", "G")
    return b.text()


def router_dense() -> str:
    signals = [f"S{i}" for i in range(8)]
    b = RBoard([*signals, "USB_P", "USB_N", "VBUS", "LOCKED"], "Router dense fixture")
    b.rect_edge(0, 0, 40, 30)
    for i, net in enumerate(signals):
        b.test_point(f"L{i}", 4, 5 + i * 2.54, net, tht=True)
        b.test_point(f"R{i}", 36, 5 + (7 - i) * 2.54, net, tht=True)
    b.test_point("UP1", 10, 26, "USB_P", size=0.8)
    b.test_point("UN1", 10, 27.2, "USB_N", size=0.8)
    b.test_point("UP2", 30, 26, "USB_P", size=0.8)
    b.test_point("UN2", 30, 27.2, "USB_N", size=0.8)
    b.test_point("V1", 3, 1.8, "VBUS", size=1.6)
    b.test_point("V2", 37, 1.8, "VBUS", size=1.6)
    b.test_point("K1", 18, 12, "LOCKED")
    b.test_point("K2", 22, 12, "LOCKED")
    b.locked_track(18, 12, 22, 12, 0.25, "F.Cu", "LOCKED")
    return b.text()


def router_4layer() -> str:
    b = RBoard(["N1", "N2"], "Router 4-layer fixture")
    parts = b.parts[0].replace(
        '\t\t(31 "B.Cu" signal)',
        '\t\t(1 "In1.Cu" signal)\n\t\t(2 "In2.Cu" signal)\n\t\t(31 "B.Cu" signal)',
    )
    b.parts[0] = parts
    b.rect_edge(0, 0, 30, 20)
    b.keepout_on("outer band", '"F.Cu" "B.Cu"', 13, -1, 17, 21, tracks=True, vias=True)
    b.test_point("A1", 5, 7, "N1", tht=True)
    b.test_point("A2", 25, 7, "N1", tht=True)
    b.test_point("B1", 5, 13, "N2", tht=True)
    b.test_point("B2", 25, 13, "N2", tht=True)
    return b.text()


def main() -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / "router_basic.kicad_pcb").write_text(router_basic())
    (OUT / "router_basic.kicad_pro").write_text(json.dumps(project(), indent=2) + "\n")
    (OUT / "router_dense.kicad_pcb").write_text(router_dense())
    power = [
        {
            "name": "Power",
            "clearance": 0.25,
            "track_width": 0.6,
            "via_diameter": 0.8,
            "via_drill": 0.4,
        }
    ]
    (OUT / "router_dense.kicad_pro").write_text(
        json.dumps(project(power, {"VBUS": "Power"}), indent=2) + "\n"
    )
    (OUT / "router_4layer.kicad_pcb").write_text(router_4layer())
    (OUT / "router_4layer.kicad_pro").write_text(json.dumps(project(), indent=2) + "\n")


if __name__ == "__main__":
    main()
