from __future__ import annotations

import json
from pathlib import Path

from pcbrouter.benchmark.real100 import generate_report, load_manifest, strip_routing_copper
from pcbrouter.kicad.loader import load_board

ROOT = Path(__file__).resolve().parents[2]


def test_real100_manifest_is_exactly_100_pinned_boards() -> None:
    manifest = load_manifest(ROOT / "benchmarks" / "real100" / "manifest.json")
    assert len(manifest.boards) == 100
    assert len({b.id for b in manifest.boards}) == 100
    assert sum(b.family == "qa" for b in manifest.boards) == 80
    assert sum(b.family == "demo" for b in manifest.boards) == 20
    assert all(len(b.git_blob_sha1) == 40 for b in manifest.boards)
    assert len(manifest.source_ref) == 40


def test_strip_routing_copper_only_removes_top_level_route_objects() -> None:
    text = """(kicad_pcb (version 20240108)
  (general (thickness 1.6))
  (footprint "X" (layer "F.Cu")
    (fp_line (start 0 0) (end 1 1) (stroke (width 0.1) (type default)) (layer "F.SilkS"))
    (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.5) (layers "*.Cu" "*.Mask") (net 1 "N")))
  (segment (start 0 0) (end 2 0) (width 0.25) (layer "F.Cu") (net 1))
  (via (at 2 0) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1))
  (arc (start 2 0) (mid 3 1) (end 4 0) (width 0.25) (layer "B.Cu") (net 1))
  (gr_arc (start 0 0) (mid 1 1) (end 2 0) (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
)"""
    stripped, counts = strip_routing_copper(text)
    assert counts == {"segment": 1, "via": 1, "arc": 1}
    assert "(segment " not in stripped
    assert "(via " not in stripped
    assert "\n  (arc " not in stripped
    assert "(gr_arc " in stripped
    assert "(footprint " in stripped
    assert "(pad " in stripped


def test_strip_respects_strings_comments_and_is_idempotent() -> None:
    text = (
        "(kicad_pcb (version 20240108)\n"
        '  (title_block (comment 1 "not a (via here)"))\n'
        "  ; (segment (start 0 0) (end 1 1)) in a comment stays\n"
        '  (segment (start 0 0) (end 2 0) (width 0.25) (layer "F.Cu") (net 1))\n'
        '  (via (at 2 0) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1))\n'
        '  (gr_line (start 0 0) (end 1 0) (layer "Edge.Cuts"))\n'
        ")\n"
    )
    stripped, counts = strip_routing_copper(text)
    assert counts == {"segment": 1, "via": 1, "arc": 0}
    assert '"not a (via here)"' in stripped and "; (segment" in stripped
    assert "\n\n" not in stripped  # removed objects leave no blank lines
    again, leftovers = strip_routing_copper(stripped)
    assert again == stripped and leftovers == {"segment": 0, "via": 0, "arc": 0}


def test_both_manifest_schemas_load(tmp_path: Path) -> None:
    data = json.loads((ROOT / "benchmarks" / "real100" / "manifest.json").read_text("utf-8"))
    for schema in ("davesrouter-real100/1", "davesrouter-real100/1.1"):
        path = tmp_path / f"{schema.rsplit('/', 1)[1]}.json"
        path.write_text(json.dumps({**data, "schema": schema}), encoding="utf-8")
        assert len(load_manifest(path).boards) == 100


def test_strip_real_fixture_retains_loadable_board_and_nets(tmp_path: Path) -> None:
    src = ROOT / "tests" / "fixtures" / "boards" / "router_dense.kicad_pcb"
    before = load_board(src).board
    stripped, _ = strip_routing_copper(src.read_text(encoding="utf-8"))
    out = tmp_path / "router_dense.kicad_pcb"
    out.write_text(stripped, encoding="utf-8")
    after = load_board(out).board
    assert after.statistics.net_count == before.statistics.net_count
    assert after.statistics.pad_count == before.statistics.pad_count
    assert after.statistics.footprint_count == before.statistics.footprint_count
    assert after.statistics.track_count == 0
    assert after.statistics.via_count == 0
    assert after.outline.segments == before.outline.segments


def test_report_generation(tmp_path: Path) -> None:
    result = {
        "id": "K001",
        "name": "example.kicad_pcb",
        "family": "qa",
        "difficulty": "small",
        "profile": "smoke",
        "mode": "speed",
        "status": "ok",
        "route_status": "FULLY_ROUTED",
        "wall_s": 1.25,
        "runtime_s": 1.1,
        "metrics": {
            "nets_attempted": 2,
            "nets_completed": 2,
            "nets_failed": 0,
            "completion_pct": 100.0,
            "new_vias": 0,
            "total_routed_length_mm": 12.0,
            "expanded_nodes": 100,
            "ripups": 0,
            "reroutes": 0,
            "passes": 1,
            "clean_rate_pct": 100.0,
        },
    }
    jsonl = tmp_path / "results.jsonl"
    jsonl.write_text(json.dumps(result) + "\n", encoding="utf-8")
    csv_path, md_path = generate_report(jsonl)
    assert csv_path.is_file()
    assert md_path.is_file()
    report = md_path.read_text(encoding="utf-8")
    assert "Nets completed: **2/2**" in report
    assert "K001" in report


def test_run_accepts_a_policy_and_passes_it_to_workers(tmp_path: Path) -> None:
    from pcbrouter.benchmark.real100 import _worker_command, build_parser
    from pcbrouter.routing.presets import RouteMode

    args = build_parser().parse_args(["run", "--policy", "random:1"])
    assert args.policy == "random:1"
    assert build_parser().parse_args(["run"]).policy is None
    cmd = _worker_command(tmp_path / "b.kicad_pcb", RouteMode.SPEED, 10.0, True, None, "random:1")
    assert cmd[-2:] == ["--policy", "random:1"]


def test_worker_can_check_validity_of_new_copper(tmp_path: Path) -> None:
    from pcbrouter.benchmark.real100 import _worker_command, build_parser
    from pcbrouter.routing.presets import RouteMode

    assert build_parser().parse_args(["run", "--check-validity"]).check_validity
    cmd = _worker_command(tmp_path / "b.kicad_pcb", RouteMode.SPEED, 10.0, True, None, None, True)
    assert cmd[-1] == "--check-validity"


def test_side_by_side_runs_never_share_a_results_file(tmp_path: Path) -> None:
    import pytest

    from pcbrouter.benchmark.real100 import build_parser, run_corpus

    assert build_parser().parse_args(["run", "--out", "x.jsonl"]).out == Path("x.jsonl")
    out = tmp_path / "r.jsonl"
    out.write_text("")
    manifest = load_manifest()
    with pytest.raises(FileExistsError):
        run_corpus(manifest, tmp_path, profile="smoke", ids={"K001"}, out_path=out)
