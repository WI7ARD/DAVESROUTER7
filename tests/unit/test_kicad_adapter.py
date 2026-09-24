"""Adapter tests on small in-memory boards, one KiCad construct at a time."""

from __future__ import annotations

import pytest

from pcbrouter.domain import Board, BoardSide, OutlineShape, PadShape, PadType, Point, ViaType
from pcbrouter.kicad.adapter import (
    NEWEST_KNOWN_VERSION,
    KiCadBoardAdapter,
    LoadWarning,
    WarningSeverity,
)
from pcbrouter.kicad.errors import MalformedBoardError, UnsupportedKiCadVersion
from pcbrouter.kicad.parser import parse_sexpr

MM = 1_000_000
LAYERS = '(layers (0 "F.Cu" signal) (1 "In1.Cu" power) (31 "B.Cu" signal) (44 "Edge.Cuts" user))'


def build(body: str, version: int | None = 20240108) -> tuple[Board, list[LoadWarning]]:
    ver = f"(version {version})" if version is not None else ""
    text = f"(kicad_pcb {ver} {LAYERS} {body})"
    adapter = KiCadBoardAdapter(parse_sexpr(text), text=text)
    return adapter.build(), adapter.warnings


def warnings_of(warnings: list[LoadWarning], severity: WarningSeverity) -> list[str]:
    return [w.message for w in warnings if w.severity is severity]


def test_root_must_be_board() -> None:
    with pytest.raises(MalformedBoardError, match="schematic"):
        KiCadBoardAdapter(parse_sexpr("(kicad_sch (version 1))")).build()


def test_version_checks() -> None:
    with pytest.raises(UnsupportedKiCadVersion):
        build("", version=4)
    _, w = build("", version=NEWEST_KNOWN_VERSION + 1)
    assert any("newer than the newest tested" in m for m in warnings_of(w, WarningSeverity.WARNING))
    _, w = build("", version=None)
    assert any("version missing" in m for m in warnings_of(w, WarningSeverity.WARNING))


def test_layer_table() -> None:
    board, _ = build("")
    assert board.copper_layer_names == ["F.Cu", "In1.Cu", "B.Cu"]
    in1 = board.layer("In1.Cu")
    assert in1 is not None and in1.copper_type.value == "power" and in1.ordinal == 1


def test_nets_by_code_and_by_name() -> None:
    board, _ = build(
        '(net 0 "") (net 1 "GND")'
        '(segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 1))'
        '(segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net "SIG_BY_NAME"))'
        '(segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 0))'
    )
    assert [t.net_name for t in board.tracks] == ["GND", "SIG_BY_NAME", None]
    names = {n.name: n.code for n in board.nets}
    assert names == {"GND": 1, "SIG_BY_NAME": None}  # "" (no net) is not a real net


def test_undeclared_net_code_skips_item_with_warning() -> None:
    board, w = build('(segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 7))')
    assert board.tracks == ()
    assert any("undeclared net code 7" in m for m in warnings_of(w, WarningSeverity.WARNING))


def test_footprint_rotation_places_pads_absolutely() -> None:
    board, _ = build(
        '(net 1 "A")'
        '(footprint "L:C" (layer "F.Cu") (at 120 100 90)'
        ' (property "Reference" "C1") (property "Value" "100n") (property "MPN" "X123")'
        ' (pad "1" smd rect (at -0.775 0 90) (size 0.9 0.95) (layers "F.Cu") (net 1 "A")))'
    )
    comp = board.components[0]
    assert (comp.reference, comp.value) == ("C1", "100n")
    assert comp.properties == {"MPN": "X123"}
    pad = comp.footprint.pads[0]
    assert pad.position == Point(120 * MM, 100_775_000)
    assert pad.rotation_deg == 90
    assert pad.net_name == "A"


def test_kicad5_module_and_fp_text() -> None:
    board, _ = build(
        "(module R_0805 (layer F.Cu) (tstamp 5E8A1B2C) (at 50 50)"
        " (fp_text reference R1 (at 0 -1.65)) (fp_text value 330 (at 0 1.65))"
        " (pad 1 smd rect (at -1 0) (size 1 1.4) (layers F.Cu F.Paste F.Mask)))",
        version=20171130,
    )
    comp = board.components[0]
    assert (comp.reference, comp.value, comp.id) == ("R1", "330", "5E8A1B2C")


def test_missing_reference_is_reported_not_invented() -> None:
    board, w = build('(footprint "L:X" (layer "F.Cu") (at 0 0))')
    assert board.components[0].reference == "?"
    assert any("no reference" in m for m in warnings_of(w, WarningSeverity.WARNING))


def test_back_side_and_locked_and_attributes() -> None:
    board, _ = build(
        '(footprint "L:R" (layer "B.Cu") (at 0 0 180) (locked yes) (attr smd board_only)'
        ' (property "Reference" "R9"))'
    )
    fp = board.components[0].footprint
    assert fp.side is BoardSide.BACK
    assert fp.locked
    assert fp.attributes == ("smd", "board_only")


def test_tht_pad_wildcards_drill_and_shapes() -> None:
    board, _ = build(
        '(footprint "L:J" (layer "F.Cu") (at 0 0) (property "Reference" "J1")'
        ' (pad "1" thru_hole oval (at 0 0) (size 1.7 2) (drill oval 1 1.4)'
        '  (layers "*.Cu" "*.Mask"))'
        ' (pad "" np_thru_hole circle (at 5 0) (size 3 3) (drill 3) (layers "*.Cu"))'
        ' (pad "3" smd custom (at 9 0) (size 1 1) (layers "F.Cu")))'
    )
    p1, p2, p3 = board.components[0].footprint.pads
    assert p1.layers == ("F.Cu", "In1.Cu", "B.Cu", "F.Mask", "B.Mask")
    assert p1.copper_layers == ("F.Cu", "In1.Cu", "B.Cu")
    assert p1.drill == MM
    assert p1.shape is PadShape.OVAL and p1.pad_type is PadType.THROUGH_HOLE
    assert p2.pad_type is PadType.NPTH and p2.net_name is None and p2.number == ""
    assert p3.shape is PadShape.CUSTOM


def test_footprint_body_from_courtyard_then_pads() -> None:
    board, _ = build(
        '(footprint "L:A" (layer "F.Cu") (at 10 10) (property "Reference" "A1")'
        ' (fp_rect (start -2 -1) (end 2 1) (layer "F.CrtYd"))'
        ' (fp_line (start -5 -5) (end 5 5) (layer "F.SilkS")))'
        '(footprint "L:B" (layer "F.Cu") (at 30 10) (property "Reference" "B1")'
        ' (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu"))'
        ' (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu")))'
    )
    a, b = (c.footprint for c in board.components)
    assert a.local_bounds is not None and a.local_bounds.width == 4 * MM  # silk ignored
    assert b.local_bounds is not None and b.local_bounds.width == 3 * MM  # from pads


def test_tracks_arcs_and_vias() -> None:
    board, _ = build(
        '(net 1 "N")'
        '(segment (start 0 0) (end 3 4) (width 0.25) (layer "F.Cu") (net 1) (locked yes))'
        '(arc (start 0 0) (mid -10 10) (end 0 20) (width 0.2) (layer "B.Cu") (net 1))'
        '(via (at 1 1) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))'
        '(via blind (at 2 2) (size 0.45) (drill 0.2) (layers "F.Cu" "In1.Cu") (net 1))'
        '(via micro (at 3 3) (size 0.3) (drill 0.1) (layers "F.Cu" "In1.Cu") (net 1))'
    )
    seg, arc = board.tracks
    assert seg.length == 5 * MM and seg.locked and not seg.is_arc
    assert arc.is_arc and arc.mid == Point(-10 * MM, 10 * MM)
    assert [v.via_type for v in board.vias] == [
        ViaType.THROUGH,
        ViaType.BLIND_BURIED,
        ViaType.MICRO,
    ]
    assert board.vias[1].end_layer == "In1.Cu"
    assert board.index.net_statistics["N"].via_count == 3


def test_outline_primitives() -> None:
    board, _ = build(
        '(gr_rect (start 0 0) (end 10 5) (layer "Edge.Cuts"))'
        '(gr_circle (center 20 20) (end 22 20) (layer "Edge.Cuts"))'
        '(gr_poly (pts (xy 30 0) (xy 40 0) (xy 35 5)) (layer "Edge.Cuts"))'
        '(gr_line (start 0 0) (end 99 99) (layer "F.SilkS"))'
    )
    shapes = [s.shape for s in board.outline.segments]
    assert shapes.count(OutlineShape.LINE) == 4 + 3
    assert shapes.count(OutlineShape.CIRCLE) == 1
    loops, open_chains = board.outline.closed_loops()
    assert len(loops) == 3 and open_chains == 0
    assert board.bounds is not None and board.bounds.max_x == 40 * MM  # silk line ignored


def test_unknown_and_undisplayed_constructs_are_reported() -> None:
    _, w = build('(zone (net 0)) (zone (net 0)) (gr_text "x" (at 0 0)) (brand_new_thing 1)')
    info = warnings_of(w, WarningSeverity.INFO)
    assert "2 × 'zone' present but not displayed in Stage 1" in info
    assert any("'gr_text'" in m for m in info)
    assert any("brand_new_thing" in m for m in warnings_of(w, WarningSeverity.WARNING))


def test_bad_items_are_skipped_with_line_numbers() -> None:
    board, w = build(
        '(net 1 "N")\n'
        '(segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 1))\n'
        '(segment (start 0 0) (end x 0) (width 0.2) (layer "F.Cu") (net 1))\n'
        "(segment (start 0 0) (end 1 0) (width 0.2) (net 1))\n"
        '(footprint "L:X" (layer "F.Cu") (property "Reference" "U1"))\n'
        '(footprint "L:Y" (layer "F.Cu") (at 0 0) (property "Reference" "U2")'
        ' (pad "1" smd rect (at 0 0) (layers "F.Cu")))\n'
    )
    assert len(board.tracks) == 1
    assert [c.reference for c in board.components] == ["U2"]
    assert board.components[0].footprint.pads == ()
    msgs = [x for x in w if x.severity is WarningSeverity.WARNING]
    assert len(msgs) == 4
    assert all(x.line is not None and x.line > 1 for x in msgs)
    assert any("missing size" in x.message for x in msgs)


def test_duplicate_uuids_get_unique_ids() -> None:
    board, _ = build(
        '(segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (uuid "dup"))'
        '(segment (start 0 1) (end 1 1) (width 0.2) (layer "F.Cu") (uuid "dup"))'
    )
    ids = [t.id for t in board.tracks]
    assert len(set(ids)) == 2 and ids[0] == "dup"


def test_kicad5_rules() -> None:
    board, _ = build(
        "(general (thickness 1.6)) (setup (trace_min 0.2) (via_min_size 0.4) (via_min_drill 0.3))"
        ' (net_class Default "d" (clearance 0.2))',
        version=20171130,
    )
    r = board.rules
    assert (r.min_track_width, r.min_via_diameter, r.min_via_drill, r.min_clearance) == (
        200_000, 400_000, 300_000, 200_000,
    )  # fmt: skip
    assert r.board_thickness == 1_600_000


def test_modern_board_rules_stay_unknown() -> None:
    board, _ = build("(general (thickness 1.6)) (setup (pad_to_mask_clearance 0))")
    assert board.rules.min_clearance is None
    assert board.rules.board_thickness == 1_600_000


def test_duplicate_references_are_reported() -> None:
    board, w = build(
        '(footprint "L:R" (layer "F.Cu") (at 0 0) (property "Reference" "R?"))'
        '(footprint "L:R" (layer "F.Cu") (at 5 0) (property "Reference" "R?"))'
    )
    assert len(board.components) == 2  # both kept
    assert any("duplicate reference" in m for m in warnings_of(w, WarningSeverity.WARNING))
