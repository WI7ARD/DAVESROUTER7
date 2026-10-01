"""KiCad DRC oracle (pcbrouter.kicad.oracle): report parsing, pre/post diffing,
version limits, "oracle unavailable", and - when KiCad 8+ is installed - a real
route -> export -> KiCad DRC round trip."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pcbrouter.kicad import oracle
from pcbrouter.kicad.oracle import DrcRun, OracleTool, compare, parse_report

BOARDS = Path(__file__).parent.parent / "fixtures" / "boards"


def _v(vtype: str, sev: str, *items: tuple[str, str | None]) -> dict:
    return {
        "type": vtype,
        "severity": sev,
        "description": vtype,
        "items": [{"description": d, "uuid": u, "pos": {"x": 1.0, "y": 2.0}} for d, u in items],
    }


def _report(violations: list[dict], unconnected: list[dict] | None = None) -> dict:
    return {"kicad_version": "8.0.8", "violations": violations, "unconnected_items": unconnected}


def test_compare_separates_pre_existing_new_generated_unrelated_and_resolved() -> None:
    old = _v("clearance", "error", ("Pad 1 [A]", "p1"), ("Pad 2 [B]", "p2"))
    gone = _v("silk_overlap", "warning", ("Text", "t1"))
    pre = parse_report(
        _report([old, gone], [_v("unconnected_items", "error", ("Pad [N]", "x"))]), "refilled"
    )
    gen = _v("clearance", "error", ("Track [N]", "gen-1"), ("Pad 3 [C]", "p3"))
    unrel = _v("starved_thermal", "error", ("Zone [GND]", "z1"))
    post = parse_report(_report([old, gen, unrel]), "refilled")
    res = compare(pre, post, {"gen-1"})
    assert res["status"] == "ROUTER_VIOLATIONS"
    assert (res["pre_existing"], res["new_generated_errors"], res["new_unrelated_errors"]) == (
        1,
        1,
        1,
    )
    assert res["resolved"] == 1 and res["new_generated_types"] == {"clearance": 1}
    assert (res["unconnected_before"], res["unconnected_after"]) == (1, 0)
    assert res["examples_generated"][0]["items"][0] == "Track [N]"


def test_compare_is_clean_when_only_old_problems_remain_and_reports_open_nets() -> None:
    old = _v("clearance", "error", ("Pad 1 [A]", "p1"))
    unc = _v("unconnected_items", "error", ("Track [GND]", "gen-2"), ("Pad 2 [GND]", "p9"))
    pre = parse_report(_report([old]), "refilled")
    post = parse_report(_report([old], [unc]), "refilled")
    res = compare(pre, post, {"gen-2"})
    assert res["status"] == "CLEAN"
    assert res["unconnected_nets_after"] == ["GND"] and res["newly_unconnected_nets"] == ["GND"]
    assert res["unconnected_touching_generated"] == 1


def test_items_without_uuid_still_match_across_runs() -> None:
    v = {
        "type": "copper_sliver",
        "severity": "warning",
        "description": "Copper sliver",
        "items": [],
    }
    pre, post = parse_report(_report([v]), "a"), parse_report(_report([v]), "a")
    assert compare(pre, post, set())["pre_existing"] == 1


def test_a_run_that_did_not_happen_is_never_reported_clean() -> None:
    ok = parse_report(_report([]), "refilled")
    res = compare(DrcRun("not_run", "refilled", message="too new"), ok, set())
    assert res["status"] == "NOT_RUN" and "too new" in res["reason"]


def test_board_format_limits_follow_the_kicad_version(tmp_path: Path) -> None:
    board = tmp_path / "b.kicad_pcb"
    board.write_text('(kicad_pcb (version 20241229) (generator "pcbnew"))')
    assert oracle.board_format(board) == 20241229
    k8 = OracleTool(Path("kicad-cli"), "8.0.8", "python", "python3")
    assert k8.max_format == 20240108 and not k8.can_read(20241229) and k8.can_read(20221018)
    run = oracle.run_drc(k8, board, "refilled")
    assert run.status == "not_run" and "newer than KiCad 8.0.8" in run.message
    nofill = OracleTool(Path("kicad-cli"), "9.0.3", "none")
    board.write_text("(kicad_pcb (version 20221018))")
    assert oracle.run_drc(nofill, board, "refilled").status == "not_run"
    assert OracleTool(Path("x"), "10.0.0", "cli").can_read(20991231)  # unknown: let KiCad decide


def test_oracle_unavailable_is_reported_not_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oracle, "find_kicad_cli", lambda: None)
    assert oracle.find_oracle() is None
    assert oracle.find_oracle("/no/such/kicad-cli") is None


def test_stage_board_copies_project_sidecars_and_never_writes_beside_the_source(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "b.kicad_pcb").write_text("(kicad_pcb)")
    (src / "b.kicad_pro").write_text("{}")
    (src / "b.kicad_dru").write_text("(version 1)")
    staged = oracle.stage_board(src / "b.kicad_pcb", tmp_path / "w", "post")
    assert staged.name == "post.kicad_pcb"
    assert (tmp_path / "w" / "post.kicad_pro").is_file()
    assert (tmp_path / "w" / "post.kicad_dru").is_file()
    assert sorted(p.name for p in src.iterdir()) == ["b.kicad_dru", "b.kicad_pcb", "b.kicad_pro"]


def test_routed_board_is_judged_with_the_source_project_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # an export has no .kicad_pro beside it; judged alone, KiCad would apply its
    # built-in defaults (0.5 mm via, 0.3 mm hole, 0.5 mm edge) and blame the router
    src = tmp_path / "src"
    src.mkdir()
    (src / "b.kicad_pcb").write_text("(kicad_pcb (version 20221018))")
    (src / "b.kicad_pro").write_text('{"rules": "source"}')
    routed = tmp_path / "out" / "b_routed.kicad_pcb"
    routed.parent.mkdir()
    routed.write_text("(kicad_pcb (version 20221018))")
    seen: list[str] = []

    def fake_drc(tool: OracleTool, board: Path, variant: str, timeout_s: float = 0) -> DrcRun:
        seen.append(board.with_suffix(".kicad_pro").read_text())
        return parse_report(_report([]), variant)

    monkeypatch.setattr(oracle, "run_drc", fake_drc)
    tool = OracleTool(Path("kicad-cli"), "8.0.8", "python", "python3")
    res = oracle.oracle_check(tool, src / "b.kicad_pcb", routed, set(), tmp_path / "w")
    assert res["status"] == "CLEAN" and seen and set(seen) == {'{"rules": "source"}'}


# ------------------------------------------------- real KiCad (skipped without it)
_TOOL = oracle.find_oracle()
needs_kicad = pytest.mark.skipif(_TOOL is None, reason="KiCad 8+ kicad-cli not installed")


@needs_kicad
def test_routed_board_is_clean_in_real_kicad_drc(tmp_path: Path) -> None:
    from pcbrouter.kicad.loader import load_board
    from pcbrouter.kicad.rule_adapter import load_project_rules
    from pcbrouter.kicad.writer import export_board
    from pcbrouter.routing.board_router import BoardRouter, BoardRouterSettings
    from pcbrouter.routing.presets import RouteMode, adjust_board_settings
    from pcbrouter.routing.request import RouteRequest
    from pcbrouter.routing.working_board import WorkingBoard

    assert _TOOL is not None
    src = oracle.stage_board(BOARDS / "router_ripup.kicad_pcb", tmp_path / "src", "board")
    loaded = load_board(src)
    wb = WorkingBoard(loaded.board, load_project_rules(src))
    base = RouteRequest("", candidates=1)
    st = adjust_board_settings(
        BoardRouterSettings(base_request=base, budget_s=60), base, RouteMode.ACCURACY
    )
    res = BoardRouter(wb, st).run()
    assert res.added_tracks
    out = tmp_path / "routed.kicad_pcb"
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    assert export_board(src, sha, loaded.board, res.final_board, out).ok
    # no sidecars beside the export: the oracle must use the source's project
    generated = {t.id for t in res.added_tracks} | {v.id for v in res.added_vias}
    done = {n for n, o in res.outcomes.items() if o.status.value == "SUCCESS"}
    result = oracle.oracle_check(
        _TOOL, src, out, generated, tmp_path / "oracle", claimed_complete_nets=done
    )
    json.dumps(result)  # serialisable for the benchmark log
    assert result["kicad_version"] == _TOOL.version and result["status"] != "NOT_RUN"
    verdict = result["variants"][result["verdict_variant"]]
    assert verdict["new_generated_errors"] == 0, verdict["examples_generated"]
    assert verdict["completion_disagreements"] == []
