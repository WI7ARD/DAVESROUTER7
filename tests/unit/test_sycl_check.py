"""Staged GPU diagnostic (compute/sycl_check.py) with fake SYCL modules: every
failure mode stops at the right stage with the real exception text, and "GPU
available" is never claimed from a device object alone."""

from __future__ import annotations

import sys
import types
from typing import Any

import numpy as np
import pytest

from pcbrouter.app.application import main
from pcbrouter.compute import sycl_check


class FakeDevice:
    def __init__(self, name: str, dtype: str, flt: str) -> None:
        self.name, self.filter_string = name, flt
        self.device_type = f"device_type.{dtype}"
        self.backend = f"backend_type.{flt.split(':')[0]}"
        self.driver_version, self.global_mem_size, self.max_compute_units = "1.2.3", 2**33, 96
        self.vendor = "Intel(R) Corporation"


class FakeArray:
    def __init__(self, data: np.ndarray, device: FakeDevice, wrong: bool = False) -> None:
        self.data, self.sycl_device, self.wrong = data, device, wrong

    def __mul__(self, k: int) -> FakeArray:
        return FakeArray(self.data * k, self.sycl_device, self.wrong)

    def __add__(self, k: int) -> FakeArray:
        return FakeArray(self.data + k, self.sycl_device, self.wrong)

    def __mod__(self, k: int) -> FakeArray:
        return FakeArray(self.data % k, self.sycl_device, self.wrong)

    def sum(self) -> FakeArray:
        return FakeArray(np.asarray(self.data.sum() + (1 if self.wrong else 0)), self.sycl_device)


def install_fakes(monkeypatch: pytest.MonkeyPatch, devices: list[FakeDevice], *,
                  alloc_on: FakeDevice | None = None, alloc_error: bool = False,
                  wrong: bool = False) -> None:  # fmt: skip
    dpctl = types.ModuleType("dpctl")
    dpctl.__version__ = "9.9"  # type: ignore[attr-defined]
    dpctl.get_devices = lambda: devices  # type: ignore[attr-defined]

    def sycl_device(flt: str) -> FakeDevice:
        for d in devices:
            if d.filter_string.startswith(flt):
                return d
        raise RuntimeError(f"Could not create a SyclDevice with the selector string '{flt}'")

    def select_gpu() -> FakeDevice:
        return next(d for d in devices if "gpu" in d.device_type) if any(
            "gpu" in d.device_type for d in devices) else (_ for _ in ()).throw(
            RuntimeError("Device unavailable."))  # fmt: skip

    dpctl.SyclDevice = sycl_device  # type: ignore[attr-defined]
    dpctl.select_gpu_device = select_gpu  # type: ignore[attr-defined]
    dpctl.SyclQueue = lambda dev: types.SimpleNamespace(sycl_device=dev)  # type: ignore[attr-defined]
    dpnp = types.ModuleType("dpnp")
    dpnp.__version__ = "8.8"  # type: ignore[attr-defined]
    dpnp.int64 = np.int64  # type: ignore[attr-defined]

    def arange(n: int, dtype: Any = None, sycl_queue: Any = None) -> FakeArray:
        if alloc_error:
            raise MemoryError("USM allocation failed")
        return FakeArray(np.arange(n, dtype=np.int64), alloc_on or sycl_queue.sycl_device, wrong)

    dpnp.arange = arange  # type: ignore[attr-defined]
    dpnp.asnumpy = lambda a: a.data  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dpctl", dpctl)
    monkeypatch.setitem(sys.modules, "dpnp", dpnp)
    relax_mod = types.ModuleType("pcbrouter.compute.sycl_relax")
    relax_mod.relax_selftest = lambda q: (True, "identical")  # type: ignore[attr-defined]
    relax_mod.benchmark_relax = lambda q: {"summary": "fast"}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pcbrouter.compute.sycl_relax", relax_mod)


IRIS = FakeDevice("Intel(R) Iris(R) Xe Graphics", "gpu", "level_zero:gpu:0")
IRIS_OCL = FakeDevice("Intel(R) Iris(R) Xe Graphics", "gpu", "opencl:gpu:0")
CPU = FakeDevice("Intel(R) Core(TM) i5-1235U", "cpu", "opencl:cpu:0")


def stages(diag: sycl_check.Diagnostic) -> dict[str, bool]:
    return {s.name: s.ok for s in diag.stages}


def test_every_stage_passes_on_a_working_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fakes(monkeypatch, [CPU, IRIS_OCL, IRIS])
    diag = sycl_check.run_diagnostic("gpu")
    assert all(stages(diag).values()) and list(stages(diag)) == list(sycl_check.STAGES)
    sel = next(s for s in diag.stages if s.name == "gpu_selected")
    assert "level_zero:gpu" in sel.detail  # Level Zero preferred over OpenCL
    assert diag.compute_verified and "every stage passed" in diag.verdict()


def test_no_gpu_stops_at_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fakes(monkeypatch, [CPU])
    diag = sycl_check.run_diagnostic("gpu")
    assert stages(diag) == {"dpctl_import": True, "dpnp_import": True, "devices": True,
                            "gpu_selected": False}  # fmt: skip
    assert "Device unavailable" in (diag.stages[-1].error or "")
    assert not diag.compute_verified and "stopped at 'gpu_selected'" in diag.verdict()


def test_array_on_the_wrong_device_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fakes(monkeypatch, [CPU, IRIS], alloc_on=CPU)  # default-device trap
    diag = sycl_check.run_diagnostic("gpu")
    assert stages(diag)["allocation"] is False
    assert "landed on opencl:cpu:0" in (diag.stages[-1].error or "")


def test_allocation_failure_and_wrong_result(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fakes(monkeypatch, [IRIS], alloc_error=True)
    diag = sycl_check.run_diagnostic("gpu")
    assert stages(diag)["allocation"] is False and "USM allocation failed" in str(
        diag.stages[-1].error
    )
    install_fakes(monkeypatch, [IRIS], wrong=True)
    diag = sycl_check.run_diagnostic("gpu")
    assert stages(diag)["operation"] is True and stages(diag)["verified"] is False
    assert not diag.compute_verified


def test_missing_dpctl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "dpctl", None)
    diag = sycl_check.run_diagnostic("gpu")
    assert stages(diag) == {"dpctl_import": False}


def test_crashing_child_is_a_report_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sycl_check, "_child_command",
                        lambda t: [sys.executable, "-c", "import os; os.abort()"])  # fmt: skip
    rep = sycl_check.run_in_child("gpu")
    assert rep["compute_verified"] is False and "crashed" in rep["verdict"]
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
    monkeypatch.setattr(sycl_check, "_child_command", lambda t: sleeper)
    rep = sycl_check.run_in_child("gpu", timeout_s=1)
    assert "hung" in rep["verdict"]


def test_child_flag_prints_one_json_line(capsys: pytest.CaptureFixture[str],
                                         monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    install_fakes(monkeypatch, [CPU])
    assert main(["--gpu-probe-child", "gpu"]) == 0
    import json

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["last_passed"] == "devices" and out["compute_verified"] is False


def test_gpu_check_exit_code_follows_verified_compute(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from pcbrouter.compute import probe

    monkeypatch.setattr(probe, "gpu_library_report",
                        lambda: {"library": "dpnp", "library_loads": True})  # fmt: skip
    failed = {"compute_verified": False, "stages": [], "verdict": "GPU: stopped at 'gpu_selected'"}
    monkeypatch.setattr(sycl_check, "run_in_child", lambda t: failed)
    assert main(["--gpu-check"]) == 1
    assert "stopped at 'gpu_selected'" in capsys.readouterr().out
    monkeypatch.setattr(sycl_check, "run_in_child", lambda t: {
        "compute_verified": True, "verdict": "GPU: every stage passed",
        "stages": [{"name": "fused_kernel", "ok": True}]})  # fmt: skip
    assert main(["--gpu-check"]) == 0
