"""Deterministic generator for larger synthetic KiCad boards (performance tests).

The output uses the KiCad 8 file format. It is *synthetic*: electrically
meaningless, but structurally representative (footprints with pads, straight and
arc tracks on two layers, vias).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SyntheticCounts:
    footprints: int
    pads: int
    nets: int
    tracks: int
    vias: int


def generate_board(columns: int = 20, rows: int = 20) -> tuple[str, SyntheticCounts]:
    """A grid of 2-pad resistors, each joined to its right neighbour by a routed net.

    Every horizontal link is: F.Cu segment -> via -> B.Cu segment -> via -> F.Cu segment.
    """
    pitch = 5.0
    parts: list[str] = [
        '(kicad_pcb\n\t(version 20240108)\n\t(generator "synthetic")\n'
        '\t(generator_version "8.0")\n\t(general\n\t\t(thickness 1.6)\n\t)\n'
        '\t(layers\n\t\t(0 "F.Cu" signal)\n\t\t(31 "B.Cu" signal)\n'
        '\t\t(44 "Edge.Cuts" user)\n\t\t(47 "F.CrtYd" user "F.Courtyard")\n\t)\n'
        '\t(net 0 "")\n'
    ]
    net_count = columns * rows
    for n in range(1, net_count + 1):
        parts.append(f'\t(net {n} "N{n}")\n')
    uid = 0

    def next_uuid() -> str:
        nonlocal uid
        uid += 1
        return f"00000000-0000-4000-8000-{uid:012d}"

    tracks = vias = 0
    for r in range(rows):
        for c in range(columns):
            idx = r * columns + c
            x, y = 10 + c * pitch, 10 + r * pitch
            left_net = idx + 1
            right_net = idx + 2 if c < columns - 1 else idx + 1
            parts.append(
                f'\t(footprint "Synthetic:R_0603"\n\t\t(layer "F.Cu")\n\t\t(uuid "{next_uuid()}")\n'
                f"\t\t(at {x} {y})\n"
                f'\t\t(property "Reference" "R{idx + 1}")\n\t\t(property "Value" "1k")\n'
                f'\t\t(fp_rect (start -1.5 -0.75) (end 1.5 0.75) (layer "F.CrtYd"))\n'
                f'\t\t(pad "1" smd roundrect (at -0.8 0) (size 0.8 0.9) '
                f'(layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) '
                f'(net {left_net} "N{left_net}") (uuid "{next_uuid()}"))\n'
                f'\t\t(pad "2" smd roundrect (at 0.8 0) (size 0.8 0.9) '
                f'(layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) '
                f'(net {right_net} "N{right_net}") (uuid "{next_uuid()}"))\n\t)\n'
            )
            if c < columns - 1:
                net = idx + 2
                x0, x1 = x + 0.8, x + pitch - 0.8
                v0, v1 = x0 + 0.8, x1 - 0.8
                for sx, ex, layer in ((x0, v0, "F.Cu"), (v0, v1, "B.Cu"), (v1, x1, "F.Cu")):
                    parts.append(
                        f"\t(segment (start {sx:.4f} {y}) (end {ex:.4f} {y}) (width 0.2) "
                        f'(layer "{layer}") (net {net}) (uuid "{next_uuid()}"))\n'
                    )
                    tracks += 1
                for vx in (v0, v1):
                    parts.append(
                        f"\t(via (at {vx:.4f} {y}) (size 0.6) (drill 0.3) "
                        f'(layers "F.Cu" "B.Cu") (net {net}) (uuid "{next_uuid()}"))\n'
                    )
                    vias += 1
    w, h = 20 + columns * pitch, 20 + rows * pitch
    parts.append(
        f"\t(gr_rect (start 0 0) (end {w} {h}) (stroke (width 0.1) (type default)) "
        f'(fill none) (layer "Edge.Cuts") (uuid "{next_uuid()}"))\n)\n'
    )
    counts = SyntheticCounts(
        footprints=columns * rows,
        pads=columns * rows * 2,
        nets=net_count,
        tracks=tracks,
        vias=vias,
    )
    return "".join(parts), counts
