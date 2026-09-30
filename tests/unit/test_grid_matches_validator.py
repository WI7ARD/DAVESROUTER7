"""The search grid demands what conservative validation enforces (Real100 tuning).

* K037: unsupported critical DRU rules attach a *possibly stricter* clearance that
  conservative validation enforces (a smaller gap is RULE_UNKNOWN = illegal). The
  grid inflated obstacles only by the resolved value, so the search kept offering
  paths the validator refused until the net failed with VALIDATION.
* K059: with no copper-to-edge clearance stated, conservative validation refuses
  every track. The router searched for minutes before failing with VALIDATION;
  it now refuses up front with the real reason (RULE_UNKNOWN), like an unknown
  minimum width.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.routing.occupancy import CellState, build_occupancy, enforced_clearance
from pcbrouter.routing.request import RouteRequest, RuleUnknownError, normalise
from pcbrouter.routing.working_board import WorkingBoard
from pcbrouter.rules.model import UNBOUNDED, ItemType, ResolvedValue, unknown

BOARDS = Path(__file__).parent.parent / "fixtures" / "boards"


def working() -> WorkingBoard:
    path = BOARDS / "router_basic.kicad_pcb"
    return WorkingBoard(load_board(path).board, load_project_rules(path))


@pytest.mark.parametrize(
    ("conservative", "value", "bound", "expected"),
    [
        (True, 200_000, 500_000, 500_000),  # the stricter bound is enforced
        (True, 200_000, None, 200_000),
        (True, None, 300_000, 300_000),
        (True, 200_000, UNBOUNDED, 200_000),  # cannot be rasterised: validator decides
        (False, 200_000, 500_000, 200_000),  # expert mode: the validator only warns
    ],
)
def test_enforced_clearance(
    conservative: bool, value: int | None, bound: int | None, expected: int
) -> None:
    resolver = SimpleNamespace(conservative=conservative)
    req = ResolvedValue(value, possibly_stricter=bound, possibly_stricter_rules=("r",))
    assert enforced_clearance(resolver, req) == expected  # type: ignore[arg-type]


def test_grid_is_inflated_by_the_possibly_stricter_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    wb = working()
    geo, resolver = wb.engine.geometry, wb.engine.resolver
    layer = geo.copper_layers[0]

    def blocked() -> int:
        occ = build_occupancy(geo, resolver, layer, "A", 250_000, 100_000, None, ItemType.TRACK)
        return int(np.count_nonzero(occ.cells == CellState.FOREIGN_NET))

    plain = blocked()
    real = resolver.resolve_clearance

    def stricter(*a: object, **k: object) -> ResolvedValue:
        v = real(*a, **k)  # type: ignore[arg-type]
        return replace(v, possibly_stricter=(v.value or 0) + 400_000)

    monkeypatch.setattr(resolver, "resolve_clearance", stricter)
    assert resolver.conservative
    assert blocked() > plain  # the search now keeps the clearance validation enforces


def test_unknown_edge_clearance_fails_fast_in_conservative_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wb = working()
    engine = wb.engine
    monkeypatch.setattr(engine.resolver, "resolve_edge_clearance", lambda *a, **k: unknown())
    with pytest.raises(RuleUnknownError, match="copper-to-board-edge clearance"):
        normalise(engine, RouteRequest("A"))


def _with_min_drill(monkeypatch: pytest.MonkeyPatch, wb: WorkingBoard, minimum: int) -> None:
    resolver = wb.engine.resolver
    real = resolver.resolve_via_rules

    def stricter(*a: object, **k: object) -> object:
        v = real(*a, **k)  # type: ignore[arg-type]
        return replace(v, min_drill=ResolvedValue(minimum))

    monkeypatch.setattr(resolver, "resolve_via_rules", stricter)


def test_rule_via_below_the_board_minimum_routes_without_vias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KiCad demo K092: the net class via drill (0.4 mm) is below the board minimum
    (0.508 mm). Every net was refused before; now they route without vias."""
    wb = working()
    _with_min_drill(monkeypatch, wb, 10_000_000)
    norm = normalise(wb.engine, RouteRequest("A"))
    assert not norm.vias_allowed
    assert any("routing without vias" in n for n in norm.notes)


def test_an_explicit_undersized_via_is_still_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from pcbrouter.routing.request import RouteRequestError

    wb = working()
    _with_min_drill(monkeypatch, wb, 10_000_000)
    with pytest.raises(RouteRequestError, match="via drill"):
        normalise(wb.engine, RouteRequest("A", via_diameter=800_000, via_drill=400_000))


def test_known_edge_clearance_routes_normally() -> None:
    wb = working()
    assert normalise(wb.engine, RouteRequest("A")).width > 0
