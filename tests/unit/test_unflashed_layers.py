"""THT pads with ``remove_unused_layers``: on an unflashed layer KiCad's
connectivity sees only the drill hole (``CN_VISITOR``, NEVER_FLASHED), so a track
touching the pad's ring there is *not* connected (Real100 K035 '/Link Plug/RX-A':
KiCad reported the router's In1.Cu track as unconnected while we counted it)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pcbrouter.geometry.extract import build_board_geometry
from pcbrouter.kicad import oracle
from pcbrouter.kicad.loader import load_board
from pcbrouter.routing.connectivity import net_connectivity
from tests.support import kicadgen as gen

# pad 1 (THT, drill 0.6, size 1.2) at (10, 10); pad 2 (SMD) at (20, 10) on F.Cu.
# The track runs on ``layer`` from pad 2 towards pad 1 and stops ``end_x``.
HEAD = """(kicad_pcb (version 20240108) (generator "pcbnew") (generator_version "8.0")
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal)
    (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "") (net 1 "X")
  (gr_rect (start 0 0) (end 30 20) (stroke (width 0.05) (type default)) (fill none)
    (layer "Edge.Cuts") (uuid "00000000-0000-4000-8000-000000000001"))
"""


def _board(tmp_path: Path, layer: str, end_x: float, flags: str) -> Path:
    body = f"""
  (footprint "T:THT" (layer "F.Cu") (uuid "00000000-0000-4000-8000-000000000101") (at 10 10)
    (property "Reference" "J1") (property "Value" "J")
    (pad "1" thru_hole circle (at 0 0) (size 1.2 1.2) (drill 0.6) (layers "*.Cu" "*.Mask")
      {flags} (net 1 "X") (uuid "00000000-0000-4000-8000-000000000201")))
  (footprint "T:SMD" (layer "F.Cu") (uuid "00000000-0000-4000-8000-000000000102") (at 20 10)
    (property "Reference" "J2") (property "Value" "J")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask")
      (net 1 "X") (uuid "00000000-0000-4000-8000-000000000202")))
  (via (at 20 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1)
    (uuid "00000000-0000-4000-8000-000000000301"))
  (segment (start 20 10) (end {end_x} 10) (width 0.2) (layer "{layer}") (net 1)
    (uuid "00000000-0000-4000-8000-000000000302"))
"""
    path = tmp_path / "u.kicad_pcb"
    path.write_text(HEAD + body + ")\n", encoding="utf-8")
    path.with_suffix(".kicad_pro").write_text(gen._pro({}), encoding="utf-8")
    return path


REMOVE = "(remove_unused_layers yes) (keep_end_layers yes)"
REMOVE_V8 = "(remove_unused_layers) (keep_end_layers)"
KEEP = "(remove_unused_layers no) (keep_end_layers no)"

# (id, layer, track end x, pad flags, connected in KiCad 8.0.8)
# ring edge at x = 10.6, hole edge at x = 10.3; the track's 0.1 mm end cap counts
CASES = [
    ("inner_ring_only", "In1.Cu", 10.6, REMOVE, False),
    ("inner_ring_only_bare_flags", "In1.Cu", 10.6, REMOVE_V8, False),
    ("inner_reaches_hole", "In1.Cu", 10.3, REMOVE, True),
    ("inner_to_centre", "In1.Cu", 10.0, REMOVE, True),
    ("front_ring_kept_end_layer", "F.Cu", 10.6, REMOVE, True),
    ("inner_ring_layers_kept", "In1.Cu", 10.6, KEEP, True),
]


def _ours(path: Path) -> bool:
    geo = build_board_geometry(load_board(path).board)
    return net_connectivity(geo, "X").is_fully_connected


@pytest.mark.parametrize(
    ("layer", "end_x", "flags", "kicad"), [c[1:] for c in CASES], ids=[c[0] for c in CASES]
)
def test_connectivity_matches_kicad(
    tmp_path: Path, layer: str, end_x: float, flags: str, kicad: bool
) -> None:
    assert _ours(_board(tmp_path, layer, end_x, flags)) is kicad


def test_no_flag_value_is_parsed_as_no() -> None:
    from pcbrouter.kicad.parser import parse_sexpr

    node = parse_sexpr("(pad (remove_unused_layers no) (keep_end_layers no))")
    assert not node.has_flag("remove_unused_layers")


_TOOL = oracle.find_oracle()


@pytest.mark.skipif(_TOOL is None, reason="KiCad 8+ kicad-cli not installed")
@pytest.mark.parametrize(
    ("layer", "end_x", "flags", "kicad"), [c[1:] for c in CASES], ids=[c[0] for c in CASES]
)
def test_golden_still_matches_live_kicad(
    tmp_path: Path, layer: str, end_x: float, flags: str, kicad: bool
) -> None:
    assert _TOOL is not None
    run = oracle.run_drc(_TOOL, _board(tmp_path, layer, end_x, flags), "as_exported")
    assert run.ok, run.message
    assert (not run.unconnected) is kicad


def test_router_connects_through_the_hole_on_unflashed_layers(tmp_path: Path) -> None:
    """With no layer kept, every layer is unflashed: the route must end in the
    drill holes, and the result must be connected in our (KiCad's) sense."""
    from dataclasses import replace

    from pcbrouter.kicad.rule_adapter import load_project_rules
    from pcbrouter.routing.board_router import BoardRouter, BoardRouterSettings
    from pcbrouter.routing.presets import RouteMode, adjust_board_settings
    from pcbrouter.routing.request import RouteRequest
    from pcbrouter.routing.working_board import Provenance, WorkingBoard

    pads = "".join(f"""
  (footprint "T:THT" (layer "F.Cu") (uuid "00000000-0000-4000-8000-00000000010{n}") (at {x} 10)
    (property "Reference" "J{n}") (property "Value" "J")
    (pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 0.4) (layers "*.Cu" "*.Mask")
      (remove_unused_layers yes) (keep_end_layers no) (net 1 "X")
      (uuid "00000000-0000-4000-8000-00000000020{n}")))""" for n, x in ((1, 8), (2, 22)))
    path = tmp_path / "r.kicad_pcb"
    path.write_text(HEAD + pads + "\n)\n", encoding="utf-8")
    path.with_suffix(".kicad_pro").write_text(gen._pro({}), encoding="utf-8")
    source = load_board(path).board
    wb = WorkingBoard(source, load_project_rules(path))
    base = RouteRequest("", candidates=1)
    st = adjust_board_settings(
        BoardRouterSettings(base_request=base, budget_s=30), base, RouteMode.SPEED
    )
    res = BoardRouter(wb, replace(st, endgame=False)).run()
    assert res.outcomes["X"].status.value == "SUCCESS"
    tracks, vias, removed = res.objects_for(None)
    wb.commit_objects(tracks, vias, removed, "route", Provenance.ROUTER_GENERATED)
    assert net_connectivity(wb.geometry, "X").is_fully_connected
    if _TOOL is not None:
        import hashlib

        from pcbrouter.kicad.writer import export_board

        out = tmp_path / "routed.kicad_pcb"
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        assert export_board(path, sha, source, wb.board, out).status.value == "EXPORTED"

        out.with_suffix(".kicad_pro").write_text(gen._pro({}), encoding="utf-8")
        run = oracle.run_drc(_TOOL, out, "as_exported")
        assert run.ok and not run.unconnected, [v.brief() for v in run.unconnected]
