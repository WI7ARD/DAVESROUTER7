"""The GPU hardware gate: every GPU-dependent job probes first and is SKIPPED (not
failed) without a device. Also enforces that no module bypasses the gate."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from pcbrouter.compute.probe import (
    SKIPPED,
    GpuJobSkipped,
    GpuProbe,
    gpu_gate,
    probe_gpu,
    require_gpu,
)

ROOT = Path(__file__).resolve().parents[2]


def test_probe_never_raises_and_reports_a_reason() -> None:
    probe = probe_gpu()
    assert isinstance(probe.available, bool)
    if not probe.available:
        assert probe.reason and probe.status == SKIPPED


def test_require_gpu_marks_the_job_skipped() -> None:
    absent = GpuProbe(False, None, (), "no CUDA or oneAPI GPU device found")
    assert not gpu_gate("unit-test", absent).available
    with pytest.raises(GpuJobSkipped) as info:
        require_gpu("future-gpu-stage", absent)
    assert SKIPPED in str(info.value) and info.value.job == "future-gpu-stage"
    present = GpuProbe(True, "cupy", ("RTX",))
    assert require_gpu("x", present) is present


def test_selftest_is_skipped_without_gpu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util
    import json

    spec = importlib.util.spec_from_file_location("gpu_selftest", ROOT / "tools/gpu_selftest.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    absent = GpuProbe(False, None, (), "no CUDA or oneAPI GPU device found")
    monkeypatch.setattr(mod, "gpu_gate", lambda job: absent)
    monkeypatch.chdir(tmp_path)
    assert mod.main() == 0  # skipped is not a failure
    data = json.loads((tmp_path / "gpu_selftest_result.json").read_text())
    assert data["status"] == SKIPPED and "routes" not in data


def test_every_gpu_entry_point_goes_through_the_gate() -> None:
    """Static guard for current *and future* GPU stages: GPU libraries may only be
    loaded by compute/gpu_backend.py (whose initialize() is gated) and compute/probe.py;
    any tool that touches the GPU must call gpu_gate()."""
    allowed = {"src/pcbrouter/compute/gpu_backend.py", "src/pcbrouter/compute/probe.py"}
    pattern = re.compile(r"^\s*(import (cupy|dpnp|dpctl)|from (cupy|dpnp|dpctl)\b)", re.M)
    offenders = []
    for path in [*ROOT.glob("src/**/*.py"), *ROOT.glob("tools/*.py")]:
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if rel not in allowed and (pattern.search(text) or "import_array_module(" in text):
            offenders.append(rel)
        if rel.startswith("tools/") and ("GPUBackend(" in text or "gpu.xp" in text):
            assert "gpu_gate(" in text, f"{rel} uses the GPU without the hardware gate"
    assert not offenders, f"GPU libraries loaded outside the gated backend: {offenders}"


@pytest.mark.gpu
def test_real_gpu_wavefront_matches_cpu_rules() -> None:
    """Runs only on a machine with a GPU (auto-SKIPPED otherwise)."""
    from pcbrouter.compute.detection import detect_gpu
    from pcbrouter.compute.gpu_backend import GPUBackend
    from pcbrouter.kicad.loader import load_board
    from pcbrouter.kicad.rule_adapter import load_project_rules
    from pcbrouter.routing.request import RouteRequest
    from pcbrouter.routing.router import Router
    from pcbrouter.routing.search.wavefront import wavefront_search
    from pcbrouter.routing.working_board import WorkingBoard

    gpu = GPUBackend(detect_gpu())
    gpu.initialize()
    board = ROOT / "tests/fixtures/boards/router_basic.kicad_pcb"
    wb = WorkingBoard(load_board(board).board, load_project_rules(board))
    res = Router(
        wb.engine, search_fn=lambda p, **kw: wavefront_search(p, xp=gpu.xp, **kw)
    ).route_net(RouteRequest("A", candidates=1))
    assert res.best is not None and wb.engine.validator.validate_route(res.best.proposal).legal
    _ = np
