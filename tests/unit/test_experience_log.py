"""Routing experience log (learning level 1): local, anonymised, never breaks routing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.learning.experience import (
    SCHEMA,
    ExperienceLog,
    board_id,
    record_board_job,
    records_from_result,
)
from pcbrouter.routing.board_router import BoardRouter, BoardRouterSettings
from pcbrouter.routing.request import RouteRequest
from pcbrouter.routing.working_board import WorkingBoard

BOARDS = Path(__file__).parent.parent / "fixtures" / "boards"


@pytest.fixture(scope="module")
def routed() -> tuple[object, BoardRouterSettings]:
    path = BOARDS / "router_basic.kicad_pcb"
    wb = WorkingBoard(load_board(path).board, load_project_rules(path))
    settings = BoardRouterSettings(base_request=RouteRequest("", candidates=1), budget_s=60)
    return BoardRouter(wb, settings).run(), settings


def test_one_record_per_net_with_features_settings_and_outcome(routed: tuple) -> None:
    result, settings = routed
    recs = records_from_result(result, settings, salt="s")
    assert len(recs) == len(result.plan.tasks) > 0
    r = recs[0]
    assert r["schema"] == SCHEMA and r["board_f"]["copper_layers"] >= 1
    assert {"kind", "pads", "airwire_mm", "escape_options", "congestion"} <= set(r["net_f"])
    assert r["settings"]["mode"] in ("speed", "accuracy") and r["settings"]["grid_mm"] > 0
    ok = [x for x in recs if x["outcome"]["status"] == "SUCCESS"]
    assert ok and all(x["outcome"]["attempts"] >= 1 for x in ok)
    assert all(x["outcome"]["expanded_nodes"] > 0 for x in ok)


def test_no_net_names_or_board_identity_are_stored(routed: tuple) -> None:
    result, settings = routed
    text = json.dumps(records_from_result(result, settings, salt="s"))
    for task in result.plan.tasks:
        assert f'"{task.net}"' not in text
    assert result.base_board.fingerprint not in text


def test_board_id_is_stable_per_installation_and_salted() -> None:
    assert board_id("fp", "a") == board_id("fp", "a") != board_id("fp", "b")


def test_append_read_rotate_and_torn_lines(tmp_path: Path) -> None:
    log = ExperienceLog(tmp_path, max_bytes=400)
    assert len(log.salt) == 32 and log.salt == ExperienceLog(tmp_path).salt  # created once
    for i in range(30):
        log.append([{"i": i, "pad": "x" * 60}])
    assert (tmp_path / "experience.jsonl.1").exists()  # rotated at the cap
    with log.path.open("a", encoding="utf-8") as fh:
        fh.write('{"torn": ')  # a crash mid-write must not break reading
    got = [r["i"] for r in log.read()]
    assert got == sorted(got) and got[-1] == 29 and len(got) < 30  # oldest dropped
    assert log.path.stat().st_size <= 400 + 100  # bounded by the cap


def test_recording_never_raises(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    assert record_board_job(object(), object(), folder=blocked / "x") == 0


def test_cli_route_records_experience(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pcbrouter.app.route_cli import route_cli

    monkeypatch.setenv("PCBROUTER_DATA_DIR", str(tmp_path / "data"))
    src = BOARDS / "router_basic.kicad_pcb"
    for suffix in (".kicad_pcb", ".kicad_pro"):
        s = src.with_suffix(suffix)
        if s.exists():
            (tmp_path / s.name).write_bytes(s.read_bytes())
    board = tmp_path / src.name
    code = route_cli(board, tmp_path / "out.kicad_pcb", "speed", 0, 60.0, None, False,
                     record_experience=True)  # fmt: skip
    assert code in (0, 3)
    lines = (tmp_path / "data" / "experience" / "experience.jsonl").read_text().splitlines()
    assert lines and json.loads(lines[0])["source"] == "cli"
    route_cli(board, tmp_path / "out2.kicad_pcb", "speed", 0, 60.0, None, False,
              record_experience=False)  # fmt: skip
    assert len(
        (tmp_path / "data" / "experience" / "experience.jsonl").read_text().splitlines()
    ) == len(lines)


def test_schema_2_records_carry_profile_attempt_trace_and_job(routed: tuple) -> None:
    result, settings = routed
    recs = records_from_result(result, settings, salt="s")
    r = recs[0]
    assert r["schema"] == "pcbrouter-experience/2"
    assert r["board_p"]["signal_layers"] >= 1 and r["board_p"]["nets_to_route"] == len(recs)
    assert r["job"]["attempted"] == len(recs) and r["job"]["policy_decision"] == "NONE"
    ok = [x for x in recs if x["outcome"]["attempts"]]
    assert ok and all(len(x["outcome"]["trace"]) == x["outcome"]["attempts"] for x in ok)
    t = ok[0]["outcome"]["trace"][0]
    assert {"pass", "grid_mm", "heuristic_weight", "coarse_factor", "status", "route_s"} <= set(t)


def _net(i: int, bucket: str, mode: str = "speed") -> dict:
    return {"i": i, "settings": {"mode": mode}, "net_f": {"bucket": bucket}, "pad": "x" * 40}


def test_rotation_keeps_rare_strata_and_the_keep_file_stays_bounded(tmp_path: Path) -> None:
    common, rare = "signal|p0|e2|l0|d1", "diff_pair|p0|e2|l0|d1"
    log = ExperienceLog(tmp_path, max_bytes=3000)
    assert ExperienceLog.stratum(_net(0, rare)) == f"speed|{rare}"
    assert ExperienceLog.stratum({"net_f": {"kind": "power"}}) == "?|power"  # schema-1
    log.append([_net(0, rare), _net(1, rare), _net(2, rare, mode="accuracy")])
    i = 3
    for _ in range(12):  # several rotations of nothing but the common stratum
        log.append([_net(i + k, common) for k in range(5)])
        i += 5
    assert log.keep_path.exists() and log.path.with_suffix(".jsonl.1").exists()
    got = list(log.read())
    ids = [r["i"] for r in got]
    assert {0, 1, 2} <= set(ids)  # plain FIFO would have dropped them long ago
    assert ids == sorted(ids) and ids[-1] == i - 1
    assert len(ids) < i  # old common records were dropped
    assert log.keep_path.stat().st_size <= log.max_bytes // 2  # N halved until it fits
    kept = [json.loads(x) for x in log.keep_path.read_text().splitlines()]
    per = {s: sum(1 for r in kept if ExperienceLog.stratum(r) == s) for s in
           {ExperienceLog.stratum(r) for r in kept}}  # fmt: skip
    assert per[f"speed|{common}"] < 100 and per[f"speed|{rare}"] == 2
    # read order: keep file, then the rotated file, then the current one
    rotated = [json.loads(x)["i"] for x in log.path.with_suffix(".jsonl.1").read_text().split()]
    current = [json.loads(x)["i"] for x in log.path.read_text().split()]
    assert ids == [r["i"] for r in kept] + rotated + current


def test_keep_per_stratum_caps_each_stratum(tmp_path: Path) -> None:
    log = ExperienceLog(tmp_path, max_bytes=3000, keep_per_stratum=3)
    for n in range(30):
        log.append([_net(n, "signal|p0|e2|l0|d1"), _net(1000 + n, "power|p0|e1|l0|d2")])
    kept = [json.loads(x) for x in log.keep_path.read_text().splitlines()]
    assert len(kept) == 6  # the newest 3 of each stratum
    sig = [r["i"] for r in kept if r["i"] < 1000]
    rotated = [json.loads(x)["i"] for x in log.path.with_suffix(".jsonl.1").read_text().split()]
    # the newest of the dropped file: they end right where the rotated file begins
    assert sig == list(range(min(rotated) - 3, min(rotated)))


def test_job_reports_fully_routed_and_endgame(routed: tuple) -> None:
    import copy

    result, settings = routed
    m = result.metrics
    job = records_from_result(result, settings, salt="s")[0]["job"]
    assert {"fully_routed", "endgame"} <= set(job)
    assert job["fully_routed"] is (m.nets_attempted > 0 and m.nets_completed == m.nets_attempted)
    assert job["endgame"] == getattr(result, "endgame", None)
    with_endgame = copy.copy(result)
    with_endgame.endgame = {"stage": "repair", "nets_recovered": 1}
    job = records_from_result(with_endgame, settings, salt="s")[0]["job"]
    assert job["endgame"] == {"stage": "repair", "nets_recovered": 1}
    json.dumps(job)  # stays serialisable
    nothing = copy.copy(result)
    nothing.metrics = copy.copy(m)
    nothing.metrics.nets_attempted = nothing.metrics.nets_completed = 0
    assert records_from_result(nothing, settings, salt="s")[0]["job"]["fully_routed"] is False
