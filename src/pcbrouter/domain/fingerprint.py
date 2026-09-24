"""Deterministic fingerprint of a board's *domain state*.

Used to tie AI context, proposals and history to the exact board they were made
for. Any change to geometry, nets, layers or components changes the fingerprint,
so proposals made against an older state can be detected as stale.

It hashes the parsed domain model, not the file bytes: re-saving a file with
cosmetic changes (e.g. a new timestamp) keeps the fingerprint stable.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pcbrouter.domain.board import Board

FINGERPRINT_VERSION = 1


def board_fingerprint(board: Board) -> str:
    h = hashlib.sha256(f"pcbrouter-board-v{FINGERPRINT_VERSION}\n".encode())

    def put(*values: object) -> None:
        h.update(repr(values).encode("utf-8"))
        h.update(b"\n")

    for layer in board.layers:
        put("L", layer.name, layer.kind.value)
    for net in board.nets:
        put("N", net.name, net.code)
    for comp in sorted(board.components, key=lambda c: c.id):
        fp = comp.footprint
        put(
            "C",
            fp.id,
            comp.reference,
            comp.value,
            fp.lib_id,
            fp.position,
            fp.rotation_deg,
            fp.side.value,
            fp.locked,
        )
        for pad in fp.pads:
            put(
                "P",
                pad.id,
                pad.number,
                pad.position,
                pad.size,
                pad.rotation_deg,
                pad.net_name,
                pad.layers,
                pad.drill,
            )
    for t in sorted(board.tracks, key=lambda t: t.id):
        put("T", t.id, t.start, t.end, t.mid, t.width, t.layer, t.net_name, t.locked)
    for v in sorted(board.vias, key=lambda v: v.id):
        put(
            "V",
            v.id,
            v.position,
            v.diameter,
            v.drill,
            v.net_name,
            v.start_layer,
            v.end_layer,
            v.via_type.value,
            v.locked,
        )
    for s in board.outline.segments:
        put("O", s.shape.value, s.start, s.end, s.mid)
    return h.hexdigest()
