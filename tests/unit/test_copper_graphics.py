"""Graphics and text on copper layers are copper: parsed, extracted as obstacles
with no net, and respected by the router (KiCad DRC flags tracks crossing
them: Real100 K067 'MOD0')."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pcbrouter.geometry.board import ItemKind
from pcbrouter.geometry.extract import build_board_geometry
from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.routing.board_router import BoardRouter, BoardRouterSettings
from pcbrouter.routing.presets import RouteMode, adjust_board_settings
from pcbrouter.routing.request import RouteRequest
from pcbrouter.routing.working_board import Provenance, WorkingBoard
from tests.support import kicadgen as gen

HEAD = """(kicad_pcb (version 20240108) (generator "pcbnew") (generator_version "8.0")
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (37 "F.SilkS" user) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "") (net 1 "X")
  (gr_rect (start 0 0) (end 30 20) (stroke (width 0.05) (type default)) (fill none)
    (layer "Edge.Cuts") (uuid "00000000-0000-4000-8000-000000000001"))
"""


def _tp(n: int, x: float, y: float) -> str:
    return (
        f'  (footprint "T:TP" (layer "F.Cu") (uuid "00000000-0000-4000-8000-00000000010{n}") '
        f'(at {x} {y})\n    (property "Reference" "TP{n}") (property "Value" "TP")\n'
        f'    (pad "1" smd rect (at 0 0) (size 1.2 1.2) (layers "F.Cu" "F.Paste" "F.Mask") '
        f'(net 1 "X") (uuid "00000000-0000-4000-8000-00000000020{n}")))\n'
    )


def _board(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "g.kicad_pcb"
    path.write_text(HEAD + body + ")\n", encoding="utf-8")
    path.with_suffix(".kicad_pro").write_text(gen._pro({}), encoding="utf-8")
    return path


def test_graphics_and_visible_text_on_copper_are_parsed_and_others_ignored(
    tmp_path: Path,
) -> None:
    body = """
  (gr_line (start 1 1) (end 5 1) (stroke (width 0.3) (type solid)) (layer "F.Cu") (uuid "a1"))
  (gr_poly (pts (xy 10 10) (xy 12 10) (xy 12 12)) (stroke (width 0) (type solid)) (fill solid)
    (layer "B.Cu") (uuid "a2"))
  (gr_circle (center 20 5) (end 21 5) (stroke (width 0.2) (type solid)) (fill none)
    (layer "F.Cu") (uuid "a3"))
  (gr_text "LOGO" (at 15 15 90) (layer "F.Cu") (uuid "a4")
    (effects (font (size 1 1) (thickness 0.15))))
  (gr_text "SILK" (at 15 15) (layer "F.SilkS") (uuid "a5")
    (effects (font (size 1 1) (thickness 0.15))))
  (gr_line (start 1 2) (end 5 2) (stroke (width 0.1) (type solid)) (layer "F.SilkS") (uuid "a6"))
  (footprint "T:Logo" (layer "F.Cu") (uuid "f1") (at 25 15 90)
    (property "Reference" "G1" (at 0 0) (layer "F.Cu") (hide yes) (uuid "f2")
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_poly (pts (xy 0 0) (xy 1 0) (xy 1 1)) (stroke (width 0) (type solid)) (fill solid)
      (layer "F.Cu") (uuid "f3")))
"""
    board = load_board(_board(tmp_path, body)).board
    kinds = sorted((g.kind, g.layer) for g in board.copper_graphics)
    assert kinds == [
        ("circle", "F.Cu"), ("line", "F.Cu"), ("poly", "B.Cu"), ("poly", "F.Cu"), ("text", "F.Cu"),
    ]  # fmt: skip
    fp_poly = next(g for g in board.copper_graphics if g.footprint_ref == "G1")
    # rotated 90 degrees about (25, 15): local (1, 0) lands at (25, 14)
    assert (25_000_000, 14_000_000) in [(p.x, p.y) for p in fp_poly.points]
    text = next(g for g in board.copper_graphics if g.kind == "text")
    xs = {p.x for p in text.points}
    ys = {p.y for p in text.points}
    assert max(ys) - min(ys) > max(xs) - min(xs)  # rotated: tall, not wide
    geo = build_board_geometry(board)
    items = [i for i in geo.copper.values() if i.kind is ItemKind.GRAPHIC]
    assert len(items) == 5 and all(i.net is None for i in items)


def test_text_box_covers_the_glyphs_and_respects_justification(tmp_path: Path) -> None:
    body = """
  (gr_text "AB" (at 10 10) (layer "F.Cu") (uuid "t1")
    (effects (font (size 1 2) (thickness 0.2)) (justify left)))
"""
    text = load_board(_board(tmp_path, body)).board.copper_graphics[0]
    xs = sorted({p.x for p in text.points})
    assert xs[0] == 10_000_000  # left-justified: starts at the anchor
    assert xs[-1] - xs[0] >= 2 * 2_000_000  # 2 chars of a 2 mm wide font (at least)


def test_router_keeps_clearance_from_copper_text(tmp_path: Path) -> None:
    body = (
        _tp(1, 5, 10) + _tp(2, 25, 10) + """  (gr_text "BLOCK" (at 15 10) (layer "F.Cu") (uuid "t9")
    (effects (font (size 2 2) (thickness 0.3))))
"""
    )
    path = _board(tmp_path, body)
    wb = WorkingBoard(load_board(path).board, load_project_rules(path))
    base = RouteRequest("", candidates=1)
    st = adjust_board_settings(
        BoardRouterSettings(base_request=base, budget_s=30), base, RouteMode.SPEED
    )
    res = BoardRouter(wb, replace(st, endgame=False)).run()
    assert res.outcomes["X"].status.value == "SUCCESS"
    tracks, vias, removed = res.objects_for(None)
    wb.commit_objects(tracks, vias, removed, "route", Provenance.ROUTER_GENERATED)
    graphic_errors = [
        v
        for v in wb.engine.run_drc().errors
        if "graphic" in (v.object_a or "") or "graphic" in (v.object_b or "")
    ]
    assert graphic_errors == []


def test_text_variables_are_expanded_before_sizing_the_box(tmp_path: Path) -> None:
    """A conservative 16-character pad for '${COMMENT4}' grew Real100 K022's
    'P${COMMENT4}-${REVISION}' label over the board edge and seven pads."""
    body = """
  (title_block (title "T") (rev "00") (comment 4 "A05"))
  (gr_text "P${COMMENT4}-${REVISION}" (at 10 10) (layer "F.Cu") (uuid "t1")
    (effects (font (size 1 1) (thickness 0.2)) (justify left)))
  (gr_text "${NOT_DEFINED}" (at 10 15) (layer "F.Cu") (uuid "t2")
    (effects (font (size 1 1) (thickness 0.2)) (justify left)))
"""
    graphics = load_board(_board(tmp_path, body)).board.copper_graphics
    known = next(g for g in graphics if "A05" in g.label)
    width = max(p.x for p in known.points) - min(p.x for p in known.points)
    assert width < 10 * 1_000_000  # "PA05-00": 7 characters, not 24 + padding
    unknown = next(g for g in graphics if g is not known)
    uwidth = max(p.x for p in unknown.points) - min(p.x for p in unknown.points)
    assert uwidth >= 16 * 1_000_000  # an unknown variable is sized generously
