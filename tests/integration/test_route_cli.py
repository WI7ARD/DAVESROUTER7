"""``pcbrouter --route``: exit codes, error messages, and the source board staying
byte-for-byte unchanged on every path (unless --overwrite, which backs it up)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pcbrouter.app.application import main
from pcbrouter.app.route_cli import EXIT_ERROR, EXIT_OK, EXIT_PARTIAL
from pcbrouter.kicad.loader import load_board
from tests.fixtures.benchmark_suite import write_suite
from tests.integration.test_kicad10 import kicad10_copy

BOARDS = Path(__file__).parent.parent / "fixtures" / "boards"


def board_copy(tmp_path: Path, name: str = "router_basic") -> Path:
    for ext in (".kicad_pcb", ".kicad_pro"):
        shutil.copyfile(BOARDS / f"{name}{ext}", tmp_path / f"{name}{ext}")
    return tmp_path / f"{name}.kicad_pcb"


def route(*args: object) -> int:
    return main(["--route", "--workers", "0", *[str(a) for a in args]])


def test_full_route_writes_a_new_verified_board(tmp_path: Path) -> None:
    src = board_copy(tmp_path)
    before = src.read_bytes()
    out, rep = tmp_path / "out.kicad_pcb", tmp_path / "r.json"
    assert route(src, "--output", out, "--report", rep) == EXIT_OK
    report = json.loads(rep.read_text(encoding="utf-8"))
    assert report["verified"] is True and report["exit_code"] == EXIT_OK
    assert report["metrics"]["nets_completed"] == report["metrics"]["nets_attempted"]
    assert len(load_board(out).board.tracks) > 1
    assert src.read_bytes() == before


def test_partial_and_impossible_boards(tmp_path: Path) -> None:
    suite = write_suite(tmp_path, ["partial", "impossible"])
    before = {k: p.read_bytes() for k, p in suite.items()}
    rep = tmp_path / "p.json"
    assert route(suite["partial"], "--output", tmp_path / "p.kicad_pcb", "--report", rep,
                 "--timeout", "60") == EXIT_PARTIAL  # fmt: skip
    report = json.loads(rep.read_text(encoding="utf-8"))
    assert list(report["failed_nets"]) == ["TRAPPED"] and report["verified"] is True
    assert route(suite["impossible"], "--output", tmp_path / "i.kicad_pcb",
                 "--timeout", "60") == EXIT_ERROR  # fmt: skip
    assert not (tmp_path / "i.kicad_pcb").exists()
    assert {k: p.read_bytes() for k, p in suite.items()} == before


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--layers", "F.Cu,X.Cu"], "Unknown copper layer(s) X.Cu"),
        (["--grid", "5"], "--grid must be between"),
        (["--timeout", "0"], "must be more than 0 seconds"),
        (["--output", "out.txt"], "must be a .kicad_pcb file"),
    ],
)
def test_bad_options_are_explained(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str], message: str
) -> None:
    src = board_copy(tmp_path)
    before = src.read_bytes()
    extra = [str(tmp_path / e) if e == "out.txt" else e for e in extra]
    assert route(src, *extra) == EXIT_ERROR
    assert message in capsys.readouterr().out
    assert src.read_bytes() == before


def test_malformed_and_missing_boards(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.kicad_pcb"
    bad.write_text('(kicad_pcb (version 20240108) (net 0 "") (footprint', encoding="utf-8")
    assert route(bad, "--output", tmp_path / "o.kicad_pcb") == EXIT_ERROR
    out = capsys.readouterr().out
    assert "Could not read bad.kicad_pcb" in out and "Traceback" not in out
    assert not (tmp_path / "o.kicad_pcb").exists()
    assert route(tmp_path / "missing.kicad_pcb") == EXIT_ERROR
    assert "Board not found" in capsys.readouterr().out


def test_overwrite_needs_the_flag_and_makes_a_backup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = board_copy(tmp_path)
    before = src.read_bytes()
    assert route(src, "--output", src) == EXIT_ERROR
    assert "Refusing to overwrite the source board" in capsys.readouterr().out
    assert src.read_bytes() == before
    assert route(src, "--output", src, "--overwrite") == EXIT_OK
    backups = list((tmp_path / "pcbrouter-backups").glob("router_basic-*.kicad_pcb.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == before
    assert src.read_bytes() != before and len(load_board(src).board.tracks) > 1
    # routing the already-routed board: nothing to do, success, nothing written
    again = src.read_bytes()
    assert route(src, "--output", tmp_path / "again.kicad_pcb") == EXIT_OK
    assert "Nothing to route" in capsys.readouterr().out
    assert not (tmp_path / "again.kicad_pcb").exists() and src.read_bytes() == again


def test_explicit_gpu_without_a_gpu_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from pcbrouter.compute import probe

    monkeypatch.setattr(probe, "probe_gpu", lambda: probe.GpuProbe(False, None, reason="no GPU"))
    monkeypatch.delenv("PCBROUTER_SIMULATE_GPU", raising=False)
    src = board_copy(tmp_path)
    assert route(src, "--backend", "gpu", "--output", tmp_path / "g.kicad_pcb") == EXIT_ERROR
    assert "GPU requested but not usable" in capsys.readouterr().out
    assert not (tmp_path / "g.kicad_pcb").exists()


def test_layers_restrict_routing(tmp_path: Path) -> None:
    src = board_copy(tmp_path)
    out, rep = tmp_path / "f.kicad_pcb", tmp_path / "f.json"
    route(src, "--layers", "F.Cu", "--output", out, "--report", rep)
    report = json.loads(rep.read_text(encoding="utf-8"))
    assert report["routing_layers"] == ["F.Cu"]
    if out.exists():
        board = load_board(out).board
        new = [t for t in board.tracks if t.layer != "F.Cu"]
        assert len(new) <= 1  # only the pre-existing fixture track may be elsewhere
        assert len(board.vias) == 0


def test_route_a_kicad10_board(tmp_path: Path) -> None:
    src = kicad10_copy(tmp_path)
    before = src.read_bytes()
    out = tmp_path / "k10_routed.kicad_pcb"
    assert route(src, "--output", out) == EXIT_OK
    assert "(version 20260206)" in out.read_text(encoding="utf-8")
    assert src.read_bytes() == before
