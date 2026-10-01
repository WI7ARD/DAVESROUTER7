"""Stale zone fills: connectivity counts only what a KiCad refill keeps.

A foreign track crossing a stored pour splits it once KiCad refills (Real100
K037: "Missing connection between Zone [GND] and Zone [GND]"); a stub into the
pour does not. Also: ``A.Name == '<zone>'`` rules apply to zone fills and,
being custom rules, beat the zone's own clearance (KiCad 8 refills K037's
'outer_pour' at the rule's 0.4 mm, not its 0.508 mm local clearance).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pcbrouter.kicad import oracle
from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.routing.connectivity import net_connectivity
from pcbrouter.routing.working_board import WorkingBoard
from tests.support import kicadgen as gen

HEAD = """(kicad_pcb (version 20240108) (generator "pcbnew") (generator_version "8.0")
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (38 "B.Mask" user) (39 "F.Mask" user)
    (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "") (net 1 "GND") (net 2 "SIG")
  (gr_rect (start 0 0) (end 30 20) (stroke (width 0.05) (type default)) (fill none)
    (layer "Edge.Cuts") (uuid "00000000-0000-4000-8000-000000000001"))
"""


def _pad(n: int, x: float) -> str:
    return f"""
  (footprint "T:P" (layer "F.Cu") (uuid "00000000-0000-4000-8000-00000000010{n}") (at {x} 10)
    (property "Reference" "P{n}") (property "Value" "P")
    (pad "1" smd rect (at 0 0) (size 1.5 1.5) (layers "F.Cu" "F.Paste" "F.Mask")
      (net 1 "GND") (uuid "00000000-0000-4000-8000-00000000020{n}")))"""


def _zone(name: str = "POUR", clearance: float = 0.5, fill: str = "2 2 28 18") -> str:
    x0, y0, x1, y1 = fill.split()
    return f"""
  (zone (net 1) (net_name "GND") (layer "F.Cu") (uuid "00000000-0000-4000-8000-000000000301")
    (name "{name}") (hatch edge 0.5) (connect_pads yes (clearance {clearance}))
    (min_thickness 0.25) (filled_areas_thickness no)
    (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
    (polygon (pts (xy 2 2) (xy 28 2) (xy 28 18) (xy 2 18)))
    (filled_polygon (layer "F.Cu")
      (pts (xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1}))))"""


def _board(tmp_path: Path, body: str, dru: str | None = None) -> Path:
    path = tmp_path / "z.kicad_pcb"
    path.write_text(HEAD + body + "\n)\n", encoding="utf-8")
    path.with_suffix(".kicad_pro").write_text(gen._pro({}), encoding="utf-8")
    if dru is not None:
        path.with_suffix(".kicad_dru").write_text(dru, encoding="utf-8")
    return path


def _sig_pad(n: int, y: float) -> str:
    return f"""
  (footprint "T:S" (layer "F.Cu") (uuid "00000000-0000-4000-8000-00000000050{n}") (at 15 {y})
    (property "Reference" "S{n}") (property "Value" "S")
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (net 2 "SIG") (uuid "00000000-0000-4000-8000-00000000060{n}")))"""


def _track(y0: float, y1: float) -> str:
    """A SIG track between two SIG pads (a net KiCad keeps)."""
    return (
        _sig_pad(1, y0)
        + _sig_pad(2, y1)
        + f'\n  (segment (start 15 {y0}) (end 15 {y1}) (width 0.25) (layer "F.Cu") (net 2) '
        '(uuid "00000000-0000-4000-8000-000000000401"))'
    )


# (id, foreign track, GND connected after a KiCad 8.0.8 refill)
SPLITS = [
    ("no_foreign_copper", "", True),
    ("track_cuts_the_pour", _track(1, 19), False),
    ("stub_into_the_pour", _track(1, 9), True),
]


def _ours(path: Path) -> bool:
    wb = WorkingBoard(load_board(path).board, load_project_rules(path))
    return net_connectivity(wb.engine.geometry, "GND").is_fully_connected


@pytest.mark.parametrize(("track", "kicad"), [c[1:] for c in SPLITS], ids=[c[0] for c in SPLITS])
def test_stale_fill_connectivity_matches_a_refill(tmp_path: Path, track: str, kicad: bool) -> None:
    path = _board(tmp_path, _pad(1, 5) + _pad(2, 25) + _zone() + track)
    assert _ours(path) is kicad


def test_stored_fill_without_rules_is_taken_as_is(tmp_path: Path) -> None:
    """Geometry used without an engine (no rules): stored fills, as before."""
    from pcbrouter.geometry.extract import build_board_geometry

    path = _board(tmp_path, _pad(1, 5) + _pad(2, 25) + _zone() + _track(1, 19))
    geo = build_board_geometry(load_board(path).board)
    assert net_connectivity(geo, "GND").is_fully_connected


NAME_RULE = (
    "(version 1)\n"
    '(rule "pour" (constraint clearance (min 0.4mm)) (condition "A.Name == \'POUR\'"))\n'
)
# A SIG track 0.45 mm from the stored fill; the zone's own clearance is 0.5 mm.
# (id, rule file, KiCad 8.0.8 flags a clearance error)
NAME_CASES = [
    ("zone_clearance_applies", None, True),
    ("name_rule_beats_zone_clearance", NAME_RULE, False),
    ("name_rule_other_zone", NAME_RULE.replace("'POUR'", "'OTHER'"), True),
]


def _name_board(tmp_path: Path, dru: str | None) -> Path:
    # fill right edge at x = 14; the track's near edge at 14.45
    track = (
        '\n  (segment (start 14.575 1) (end 14.575 19) (width 0.25) (layer "F.Cu") (net 2) '
        '(uuid "00000000-0000-4000-8000-000000000401"))'
    )
    return _board(tmp_path, _pad(1, 5) + _zone(fill="2 2 14 18") + track, dru)


@pytest.mark.parametrize(
    ("dru", "kicad"), [c[1:] for c in NAME_CASES], ids=[c[0] for c in NAME_CASES]
)
def test_zone_name_rule_matches_kicad(tmp_path: Path, dru: str | None, kicad: bool) -> None:
    """The clearance the engine requires between the fill and the track (our DRC
    reports fill clearance as a warning: fills are refillable)."""
    path = _name_board(tmp_path, dru)
    wb = WorkingBoard(load_board(path).board, load_project_rules(path))
    geo = wb.engine.geometry
    fill = next(i for i in geo.copper.values() if i.kind.value == "zone_fill")
    track = next(i for i in geo.copper.values() if i.kind.value == "track")
    required, _ = wb.engine._zone_clearance(fill, track, "F.Cu")
    assert (required > 450_000) is kicad


_TOOL = oracle.find_oracle()


@pytest.mark.skipif(_TOOL is None or _TOOL.python is None, reason="KiCad 8+ with pcbnew needed")
@pytest.mark.parametrize(("track", "kicad"), [c[1:] for c in SPLITS], ids=[c[0] for c in SPLITS])
def test_split_golden_matches_live_kicad(tmp_path: Path, track: str, kicad: bool) -> None:
    assert _TOOL is not None
    path = _board(tmp_path, _pad(1, 5) + _pad(2, 25) + _zone() + track)
    run = oracle.run_drc(_TOOL, path, "refilled")
    assert run.ok, run.message
    gnd_open = any("GND" in v.nets() for v in run.unconnected)
    assert gnd_open is not kicad


@pytest.mark.skipif(_TOOL is None, reason="KiCad 8+ kicad-cli not installed")
@pytest.mark.parametrize(
    ("dru", "kicad"), [c[1:] for c in NAME_CASES], ids=[c[0] for c in NAME_CASES]
)
def test_name_golden_matches_live_kicad(tmp_path: Path, dru: str | None, kicad: bool) -> None:
    assert gen.kicad_flags(_TOOL, _name_board(tmp_path, dru)) is kicad


def test_router_reconnects_a_pour_that_later_copper_splits(tmp_path: Path) -> None:
    """GND's two pads share a pour; a third GND pad lies outside it, so GND is
    routed. SIG must cross the pour, which splits it on refill: the board router
    must leave GND connected in KiCad's (refilled) sense, not the stored one."""
    import hashlib
    from dataclasses import replace

    from pcbrouter.kicad.writer import export_board
    from pcbrouter.routing.board_router import BoardRouter, BoardRouterSettings
    from pcbrouter.routing.presets import RouteMode, adjust_board_settings
    from pcbrouter.routing.request import RouteRequest
    from pcbrouter.routing.working_board import Provenance

    third = _pad(3, 27).replace("(at 27 10)", "(at 27 19)")
    sig = _sig_pad(1, 3) + _sig_pad(2, 17)  # strips left beside them < min_thickness
    path = _board(tmp_path, _pad(1, 5) + _pad(2, 25) + third + _zone() + sig)
    source = load_board(path).board
    wb = WorkingBoard(source, load_project_rules(path))
    base = RouteRequest("", candidates=1, allowed_layers=("F.Cu",))
    # GND first (priority): SIG's later F.Cu track then splits the pour GND relied on
    st = adjust_board_settings(
        BoardRouterSettings(base_request=base, budget_s=60, priorities={"GND": 10}),
        base,
        RouteMode.SPEED,
    )
    res = BoardRouter(wb, replace(st, endgame=False)).run()
    assert any("re-verify" in line for line in res.log), res.log
    tracks, vias, removed = res.objects_for(None)
    wb.commit_objects(tracks, vias, removed, "route", Provenance.ROUTER_GENERATED)
    geo = wb.engine.geometry
    assert net_connectivity(geo, "SIG").is_fully_connected
    assert net_connectivity(geo, "GND").is_fully_connected
    assert res.outcomes["GND"].status.value == "SUCCESS"
    if _TOOL is not None and _TOOL.python is not None:
        out = tmp_path / "routed.kicad_pcb"
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        assert export_board(path, sha, source, wb.board, out).status.value == "EXPORTED"
        out.with_suffix(".kicad_pro").write_text(gen._pro({}), encoding="utf-8")
        run = oracle.run_drc(_TOOL, out, "refilled")
        assert run.ok, run.message
        assert not any("GND" in v.nets() for v in run.unconnected), [
            v.brief() for v in run.unconnected
        ]


def test_router_repairs_a_net_it_disconnected_outside_the_job(tmp_path: Path) -> None:
    """GND is connected before routing (two pads on one pour), so it is not in
    the job; SIG's route splits the pour. The job must take GND on and
    reconnect it, never leave a working net silently broken (Real100 K035:
    KiCad found 2-16 PWM-Sink nets newly unconnected after refill)."""
    from dataclasses import replace

    from pcbrouter.routing.board_router import BoardRouter, BoardRouterSettings
    from pcbrouter.routing.presets import RouteMode, adjust_board_settings
    from pcbrouter.routing.request import RouteRequest
    from pcbrouter.routing.working_board import Provenance

    sig = _sig_pad(1, 3) + _sig_pad(2, 17)
    path = _board(tmp_path, _pad(1, 5) + _pad(2, 25) + _zone() + sig)
    wb = WorkingBoard(load_board(path).board, load_project_rules(path))
    assert net_connectivity(wb.engine.geometry, "GND").is_fully_connected
    base = RouteRequest("", candidates=1, allowed_layers=("F.Cu",))
    st = adjust_board_settings(
        BoardRouterSettings(base_request=base, budget_s=60), base, RouteMode.SPEED
    )
    res = BoardRouter(wb, replace(st, endgame=False)).run()
    assert res.plan.nets[0] == "SIG" and "GND" in res.outcomes
    assert res.outcomes["GND"].status.value == "SUCCESS"
    assert res.metrics.nets_attempted == 2
    tracks, vias, removed = res.objects_for(None)
    wb.commit_objects(tracks, vias, removed, "route", Provenance.ROUTER_GENERATED)
    assert net_connectivity(wb.engine.geometry, "GND").is_fully_connected


def _override_board(tmp_path: Path, pad_clearance: str) -> Path:
    """A SIG pad 0.3 mm from the stored fill; zone clearance 0.5 mm. A pad (or
    footprint) local clearance is a KiCad *override*: it beats the zone's."""
    sig = f"""
  (footprint "T:S" (layer "F.Cu") (uuid "00000000-0000-4000-8000-000000000501") (at 15.5 10)
    (property "Reference" "S1") (property "Value" "S")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") {pad_clearance}
      (net 2 "SIG") (uuid "00000000-0000-4000-8000-000000000601")))"""
    return _board(tmp_path, _pad(1, 5) + _zone(fill="2 2 14.7 18") + sig)


# (id, pad clearance, KiCad 8.0.8 flags pad-vs-fill clearance)
OVERRIDES = [
    ("zone_clearance", "", True),
    ("pad_override_beats_zone", "(clearance 0.2)", False),
]


@pytest.mark.parametrize(
    ("pad_clearance", "kicad"), [c[1:] for c in OVERRIDES], ids=[c[0] for c in OVERRIDES]
)
def test_pad_override_judges_stale_fills_like_kicad(
    tmp_path: Path, pad_clearance: str, kicad: bool
) -> None:
    """A fill correct under KiCad's precedence is not stale (Real100 K026: QFN
    footprint clearance 0.1999 mm, zone 0.254 mm, KiCad refills at 0.2 mm)."""
    path = _override_board(tmp_path, pad_clearance)
    wb = WorkingBoard(load_board(path).board, load_project_rules(path))
    geo = wb.engine.geometry
    fill = next(i for i in geo.copper.values() if i.kind.value == "zone_fill")
    assert (geo.refill.result(geo, fill) is not None) is kicad


@pytest.mark.skipif(_TOOL is None, reason="KiCad 8+ kicad-cli not installed")
@pytest.mark.parametrize(
    ("pad_clearance", "kicad"), [c[1:] for c in OVERRIDES], ids=[c[0] for c in OVERRIDES]
)
def test_override_golden_matches_live_kicad(
    tmp_path: Path, pad_clearance: str, kicad: bool
) -> None:
    assert gen.kicad_flags(_TOOL, _override_board(tmp_path, pad_clearance)) is kicad
