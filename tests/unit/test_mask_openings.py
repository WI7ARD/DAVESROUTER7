"""Board-level solder-mask openings: new copper of another net may not enter an
opening that already exposes copper (KiCad ``solder_mask_bridge``: Real100
K024 GND repair copper ran through a B.Mask opening over another net)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pcbrouter.domain.geometry import Point
from pcbrouter.kicad import oracle
from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.routing.working_board import WorkingBoard
from tests.support import kicadgen as gen

MM = 1_000_000
HEAD = """(kicad_pcb (version 20240108) (generator "pcbnew") (generator_version "8.0")
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (38 "B.Mask" user) (39 "F.Mask" user)
    (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "") (net 1 "GND") (net 2 "SIG")
  (gr_rect (start 0 0) (end 30 20) (stroke (width 0.05) (type default)) (fill none)
    (layer "Edge.Cuts") (uuid "00000000-0000-4000-8000-000000000001"))
  (footprint "T:S" (layer "F.Cu") (uuid "00000000-0000-4000-8000-000000000101") (at 15 10)
    (property "Reference" "S1") (property "Value" "S")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask")
      (net 2 "SIG") (uuid "00000000-0000-4000-8000-000000000201")))
  (gr_rect (start 12 7) (end 18 13) (stroke (width 0) (type default)) (fill solid)
    (layer "F.Mask") (uuid "00000000-0000-4000-8000-000000000301"))
"""


def _board(tmp_path: Path, body: str = "") -> Path:
    path = tmp_path / "m.kicad_pcb"
    path.write_text(HEAD + body + ")\n", encoding="utf-8")
    path.with_suffix(".kicad_pro").write_text(gen._pro({}), encoding="utf-8")
    return path


def _wb(path: Path) -> WorkingBoard:
    return WorkingBoard(load_board(path).board, load_project_rules(path))


def test_new_copper_of_another_net_may_not_enter_the_opening(tmp_path: Path) -> None:
    v = _wb(_board(tmp_path)).engine.validator
    a, b = Point(13 * MM, 8 * MM), Point(13 * MM, 12 * MM)  # inside the opening
    assert not v.validate_segment("GND", "F.Cu", a, b, 250_000).legal
    assert v.validate_segment("SIG", "F.Cu", a, b, 250_000).legal  # only SIG exposed
    # B.Cu is under the B side mask: not affected by an F.Mask opening
    assert v.validate_segment("GND", "B.Cu", a, b, 250_000).legal


def test_existing_copper_in_an_opening_is_not_reported(tmp_path: Path) -> None:
    track = (
        '  (segment (start 13 8) (end 13 12) (width 0.25) (layer "F.Cu") (net 1) '
        '(uuid "00000000-0000-4000-8000-000000000401"))\n'
    )
    drc = _wb(_board(tmp_path, track)).engine.run_drc()
    assert not [e for e in drc.errors if "keepout" in e.kind.value]


_TOOL = oracle.find_oracle()


@pytest.mark.skipif(_TOOL is None, reason="KiCad 8+ kicad-cli not installed")
def test_kicad_flags_the_bridge_we_avoid(tmp_path: Path) -> None:
    track = (
        '  (segment (start 13 8) (end 13 12) (width 0.25) (layer "F.Cu") (net 1) '
        '(uuid "00000000-0000-4000-8000-000000000401"))\n'
    )
    assert _TOOL is not None
    run = oracle.run_drc(_TOOL, _board(tmp_path, track), "as_exported")
    assert run.ok, run.message
    assert any(v.type == "solder_mask_bridge" for v in run.violations)
