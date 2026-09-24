"""Load every fixture board end-to-end and check the domain model it produces."""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path

import pytest

from pcbrouter.domain import BoardSide, PadShape, PadType, Point, ViaType
from pcbrouter.kicad import (
    KiCadLoadError,
    LoadResult,
    MalformedBoardError,
    UnsupportedKiCadVersion,
    WarningSeverity,
    load_board,
)
from tests.fixtures.synthetic import generate_board

MM = 1_000_000
Loader = Callable[[str], LoadResult]


@pytest.mark.parametrize(
    ("name", "footprints", "pads", "nets", "tracks", "vias", "copper"),
    [
        ("empty.kicad_pcb", 0, 0, 0, 0, 0, 2),
        ("two_components.kicad_pcb", 2, 4, 3, 0, 0, 2),
        ("traces.kicad_pcb", 2, 4, 3, 5, 0, 2),
        ("vias.kicad_pcb", 2, 4, 3, 3, 3, 2),
        ("four_layer.kicad_pcb", 4, 10, 5, 4, 3, 4),
        ("kicad5_legacy.kicad_pcb", 2, 4, 2, 2, 1, 2),
    ],
)
def test_expected_counts(
    load_fixture: Loader,
    name: str,
    footprints: int,
    pads: int,
    nets: int,
    tracks: int,
    vias: int,
    copper: int,
) -> None:
    s = load_fixture(name).board.statistics
    assert (
        s.footprint_count,
        s.pad_count,
        s.net_count,
        s.track_count,
        s.via_count,
        s.copper_layer_count,
    ) == (footprints, pads, nets, tracks, vias, copper)


def test_empty_board(load_fixture: Loader) -> None:
    result = load_fixture("empty.kicad_pcb")
    board = result.board
    assert board.bounds is None and board.outline.is_empty
    assert board.metadata.format_version == 20240108
    assert board.metadata.kicad_major_version_guess == "8"
    assert [w.severity for w in result.warnings] == [WarningSeverity.INFO]


def test_two_components_geometry(load_fixture: Loader) -> None:
    board = load_fixture("two_components.kicad_pcb").board
    idx = board.index
    r1, c1 = idx.components_by_ref["R1"], idx.components_by_ref["C1"]
    assert (r1.value, r1.footprint.lib_id) == ("10k", "Resistor_SMD:R_0603_1608Metric")
    assert r1.properties["Footprint"] == "Resistor_SMD:R_0603_1608Metric"
    assert [p.position for p in r1.footprint.pads] == [
        Point(109_175_000, 100 * MM), Point(110_825_000, 100 * MM),
    ]  # fmt: skip
    # C1 is rotated 90° CCW: local (-0.775, 0) lands 0.775 mm *below* the origin.
    assert [p.position for p in c1.footprint.pads] == [
        Point(120 * MM, 100_775_000), Point(120 * MM, 99_225_000),
    ]  # fmt: skip
    pad = r1.footprint.pads[0]
    assert pad.shape is PadShape.ROUNDRECT and pad.roundrect_ratio == 0.25
    assert pad.size == (800_000, 950_000)
    assert pad.net_name == "VCC"
    assert r1.footprint.local_bounds is not None
    assert r1.footprint.local_bounds.width == 2_960_000  # courtyard, not silk/pads
    assert board.statistics.width == 30 * MM and board.statistics.height == 20 * MM
    assert board.metadata.title == "Two component fixture"
    sig = idx.net_statistics["/SIG"]
    assert (sig.pad_count, sig.track_count) == (2, 0)


def test_traces_and_lengths(load_fixture: Loader) -> None:
    board = load_fixture("traces.kicad_pcb").board
    stats = board.index.net_statistics
    arc = next(t for t in board.tracks if t.is_arc)
    assert arc.length == pytest.approx(math.pi * 2.5 * MM, abs=2)
    assert stats["VCC"].routed_length == pytest.approx(4.175 * MM + math.pi * 2.5 * MM, abs=2)
    assert stats["/SIG"].routed_length == pytest.approx(
        8.4 * MM + math.hypot(0.775, 0.775) * MM, abs=2
    )
    assert stats["GND"].routed_length == 4_225_000
    assert board.statistics.arc_track_count == 1
    widths = {t.net_name: t.width for t in board.tracks}
    assert widths["GND"] == 500_000


def test_vias(load_fixture: Loader) -> None:
    board = load_fixture("vias.kicad_pcb").board
    sig = board.index.net_statistics["/SIG"]
    assert (sig.track_count, sig.via_count) == (3, 2)
    v = board.index.vias_by_net["/SIG"][0]
    assert (v.position, v.diameter, v.drill) == (Point(113 * MM, 100 * MM), 600_000, 300_000)
    assert (v.start_layer, v.end_layer, v.via_type) == ("F.Cu", "B.Cu", ViaType.THROUGH)
    assert {t.layer for t in board.tracks} == {"F.Cu", "B.Cu"}


def test_four_layer_board(load_fixture: Loader) -> None:
    result = load_fixture("four_layer.kicad_pcb")
    board = result.board
    idx = board.index
    assert board.copper_layer_names == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert board.layer("In1.Cu") is not None
    j1 = idx.components_by_ref["J1"].footprint
    assert j1.pads[0].copper_layers == ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
    assert j1.pads[0].pad_type is PadType.THROUGH_HOLE and j1.pads[0].drill == MM
    u1 = idx.components_by_ref["U1"].footprint
    assert u1.rotation_deg == 45
    assert u1.pads[3].shape is PadShape.TRAPEZOID
    r2 = idx.components_by_ref["R2"].footprint
    assert r2.side is BoardSide.BACK and r2.pads[0].copper_layers == ("B.Cu",)
    h1 = idx.components_by_ref["H1"].footprint
    assert h1.locked and h1.pads[0].pad_type is PadType.NPTH and h1.pads[0].net_name is None
    assert {t.layer for t in board.tracks} == {"F.Cu", "In1.Cu", "In2.Cu", "B.Cu"}
    assert sorted(v.via_type.value for v in board.vias) == ["blind_buried", "micro", "through"]
    assert any(t.locked for t in board.tracks)
    assert idx.net_statistics["/UNUSED"].pad_count == 0
    loops, open_chains = board.outline.closed_loops()
    assert len(loops) == 1 and open_chains == 0
    msgs = " | ".join(str(w) for w in result.warnings)
    assert "zone" in msgs and "future_feature_xyz" in msgs


def test_kicad5_legacy(load_fixture: Loader) -> None:
    board = load_fixture("kicad5_legacy.kicad_pcb").board
    md = board.metadata
    assert md.format_version == 20171130 and md.kicad_major_version_guess == "5"
    r1 = board.index.components_by_ref["R1"]
    assert r1.value == "330" and r1.id == "5E8A1B2C"
    assert board.index.components_by_ref["D1"].footprint.pads[0].copper_layers == ("F.Cu", "B.Cu")
    # The KiCad 5 center/angle arc bulges out to x = 30 mm.
    assert board.bounds is not None and board.bounds.min_x == 30 * MM
    assert board.rules.min_track_width == 200_000
    assert board.rules.min_clearance == 200_000


@pytest.mark.parametrize(
    ("name", "error", "fragment"),
    [
        ("malformed_unbalanced.kicad_pcb", MalformedBoardError, "truncated"),
        ("malformed_not_a_board.kicad_pcb", MalformedBoardError, "schematic"),
        ("unsupported_version.kicad_pcb", UnsupportedKiCadVersion, "too old"),
    ],
)
def test_bad_boards_raise_readable_errors(
    fixture_path: Callable[[str], Path], name: str, error: type[KiCadLoadError], fragment: str
) -> None:
    with pytest.raises(error) as info:
        load_board(fixture_path(name))
    assert fragment in info.value.user_message
    assert info.value.path == fixture_path(name).resolve()


def test_partially_broken_board_loads_what_it_can(load_fixture: Loader) -> None:
    result = load_fixture("partially_broken.kicad_pcb")
    assert len(result.board.tracks) == 1
    warnings = [w for w in result.warnings if w.severity is WarningSeverity.WARNING]
    assert len(warnings) == 4
    assert {w.line for w in warnings} == {19, 27, 34, 41}


def test_synthetic_large_board(tmp_path: Path) -> None:
    text, counts = generate_board(40, 40)
    path = tmp_path / "large.kicad_pcb"
    path.write_text(text, encoding="utf-8")
    result = load_board(path)
    s = result.board.statistics
    assert (s.footprint_count, s.pad_count, s.net_count, s.track_count, s.via_count) == (
        counts.footprints, counts.pads, counts.nets, counts.tracks, counts.vias,
    )  # fmt: skip
    assert result.warnings == ()
    # Generous bound: guards against accidental quadratic behaviour, not a benchmark.
    assert result.stats.total_seconds < 20
