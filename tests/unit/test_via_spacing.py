"""A route's own vias keep the hole-to-hole minimum between each other.

The per-via check only sees holes already on the board, so two vias of one
route (or of two connections of one net) could sit closer than
``min_hole_to_hole``; the commit-time check then refused the whole net (Real100
K022: 'via ...: drill 0.2472 mm from via hole, required 0.25 mm').
"""

from __future__ import annotations

from pathlib import Path

from pcbrouter.domain.geometry import Point
from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.routing.proposal import RouteProposal, RouteSegment, RouteVia
from pcbrouter.routing.router import _drills_too_close
from pcbrouter.routing.working_board import WorkingBoard
from tests.support import kicadgen as gen

MM = 1_000_000


def _via(x_mm: float) -> RouteVia:
    return RouteVia(Point(int(x_mm * MM), 10 * MM), "F.Cu", "B.Cu", 600_000, 300_000)


def test_drill_spacing_helper() -> None:
    # min hole-to-hole 0.25 mm; drills 0.3 mm: centres must be >= 0.55 mm apart
    assert _drills_too_close(Point(10 * MM, 10 * MM), 300_000, [_via(10.54)], 250_000)
    assert not _drills_too_close(Point(10 * MM, 10 * MM), 300_000, [_via(10.56)], 250_000)
    assert _drills_too_close(Point(10 * MM, 10 * MM), 300_000, [_via(10.0)], 0)  # same spot


def test_validator_refuses_a_proposal_whose_own_vias_are_too_close(tmp_path: Path) -> None:
    path = gen.board(tmp_path)  # min_hole_to_hole 0.25 mm (kicadgen project)
    wb = WorkingBoard(load_board(path).board, load_project_rules(path))
    seg = (
        RouteSegment(Point(10 * MM, 15 * MM), Point(10 * MM, 10 * MM), "F.Cu", 250_000),
        RouteSegment(Point(10 * MM, 10 * MM), Point(int(10.5 * MM), 10 * MM), "B.Cu", 250_000),
        RouteSegment(Point(int(10.5 * MM), 10 * MM), Point(11 * MM, 15 * MM), "F.Cu", 250_000),
    )
    close = RouteProposal("GND", seg, (_via(10.0), _via(10.5)))
    res = wb.engine.validator.validate_route(close)
    assert not res.legal
    assert any("hole to hole" in m for m in res.messages)
