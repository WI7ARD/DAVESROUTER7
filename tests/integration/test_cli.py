from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from pcbrouter import __version__
from pcbrouter.app.application import build_services, main
from pcbrouter.app.cli import EXIT_COMMAND_INVALID, EXIT_LOAD_FAILED, EXIT_USAGE
from pcbrouter.compute import BackendKind
from pcbrouter.settings import AppSettings, ComputeBackendChoice


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip().endswith(__version__)
    assert __version__ == "0.5.0-stage5"


def test_inspect_outputs_json(
    fixture_path: Callable[[str], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code = main([str(fixture_path("vias.kicad_pcb")), "--inspect", "--no-log-file"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["counts"]["vias"] == 3
    assert data["copper_layers"] == ["F.Cu", "B.Cu"]
    assert data["size_mm"] == [30.0, 20.0]
    assert set(data["timing_ms"]) == {"read", "parse", "build"}


def test_inspect_errors(
    tmp_path: Path, fixture_path: Callable[[str], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--inspect", "--no-log-file"]) == EXIT_USAGE
    assert main([str(tmp_path / "x.kicad_pcb"), "--inspect", "--no-log-file"]) == EXIT_LOAD_FAILED
    bad = fixture_path("malformed_unbalanced.kicad_pcb")
    assert main([str(bad), "--inspect", "--no-log-file"]) == EXIT_LOAD_FAILED
    assert "malformed" in capsys.readouterr().err


def test_check_command(
    fixture_path: Callable[[str], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    board = str(fixture_path("four_layer.kicad_pcb"))
    ok = main(
        [board, "--no-log-file", "--check-command", '{"operation": "route_net", "target": "/DATA"}']
    )
    assert ok == 0
    assert json.loads(capsys.readouterr().out)["command_check"]["valid"] is True
    bad = main(
        [board, "--no-log-file", "--check-command", '{"operation": "route_net", "target": "NOPE"}']
    )
    assert bad == EXIT_COMMAND_INVALID


def test_services_always_end_up_on_cpu() -> None:
    settings = AppSettings(default_compute_backend=ComputeBackendChoice.GPU)
    services = build_services(settings, detect_gpu_now=False)
    assert services.compute.active.kind is BackendKind.CPU
    assert services.compute.fallback_reason is not None
    assert services.bus.read_only is True


def test_log_file_is_written(fixture_path: Callable[[str], Path], isolated_app_dirs: Path) -> None:
    assert main([str(fixture_path("traces.kicad_pcb")), "--inspect"]) == 0
    log_file = isolated_app_dirs / "logs" / "pcbrouter.log"
    content = log_file.read_text(encoding="utf-8")
    assert f"version={__version__}" in content
    assert "board.load.done" in content and "parse_ms=" in content and "footprints=2" in content
    assert "compute.selected backend=CPU" in content
