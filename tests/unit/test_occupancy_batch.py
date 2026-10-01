"""Batched obstacle marking must produce exactly the grid the per-shape path
produced (the router's soundness argument rests on the grid)."""

from __future__ import annotations

import glob
from pathlib import Path

import numpy as np
import pytest

from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.routing import occupancy
from pcbrouter.routing.working_board import WorkingBoard
from pcbrouter.rules.model import ItemType
from tests.fixtures.benchmark_suite import write_suite

BOARDS = Path(__file__).parent.parent / "fixtures" / "boards"
REAL100 = Path(__file__).parents[2] / "benchmarks" / "real100" / "work" / "prepared"


def _compare(path: Path, monkeypatch: pytest.MonkeyPatch, nets_per_layer: int = 3) -> int:
    wb = WorkingBoard(load_board(path).board, load_project_rules(path))
    geo, resolver = wb.engine.geometry, wb.engine.resolver
    nets = sorted({i.net for i in geo.copper.values() if i.net})[:nets_per_layer] + [None]
    checked = 0
    for layer in geo.copper_layers:
        for net in nets:
            for item, width in ((ItemType.TRACK, 250_000), (ItemType.VIA, 600_000)):
                grids = []
                for batch in (False, True):
                    monkeypatch.setattr(occupancy, "BATCH_MARKING", batch)
                    grids.append(
                        occupancy.build_occupancy(
                            geo, resolver, layer, net, width, 100_000, None, item
                        )
                    )
                assert np.array_equal(grids[0].cells, grids[1].cells), (path.name, layer, net, item)
                checked += 1
    return checked


@pytest.mark.parametrize("name", ["router_dense", "router_ripup", "stage3_rules"])
def test_batched_grid_equals_per_shape_grid_on_fixtures(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _compare(BOARDS / f"{name}.kicad_pcb", monkeypatch) > 0


def test_batched_grid_equals_per_shape_grid_on_suite_boards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for path in write_suite(tmp_path, ["dense_2layer", "medium_4layer"]).values():
        assert _compare(path, monkeypatch, nets_per_layer=2) > 0


@pytest.mark.skipif(not REAL100.is_dir(), reason="Real100 boards not prepared")
@pytest.mark.parametrize("bid", ["K067", "K022", "K037"])
def test_batched_grid_equals_per_shape_grid_on_real_boards(
    bid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = glob.glob(str(REAL100 / bid / "*.kicad_pcb"))
    if not found:
        pytest.skip(f"{bid} not prepared")
    assert _compare(Path(found[0]), monkeypatch, nets_per_layer=2) > 0
