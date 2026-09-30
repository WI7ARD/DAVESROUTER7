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
