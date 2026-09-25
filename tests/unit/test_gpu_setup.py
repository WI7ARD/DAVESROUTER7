"""GPU setup plan and the rule that the GUI process never loads GPU runtimes."""

from __future__ import annotations

import sys

import pytest

from pcbrouter.compute.detection import GpuDetectionResult, GpuDevice, GpuStatus
from pcbrouter.compute.gpu_setup import pip_command, plan_setup
from pcbrouter.compute.probe import probe_gpu_light


def test_plan_recommends_the_vendor_library() -> None:
    iris = GpuDetectionResult(
        GpuStatus.ONEAPI_LIBRARIES_NOT_INSTALLED,
        (GpuDevice("Intel(R) Iris(R) Xe Graphics", "Intel"),),
    )
    plan = plan_setup(iris)
    assert plan.package == "dpnp" and "Iris" in plan.text()
    nvidia = GpuDetectionResult(
        GpuStatus.GPU_LIBRARIES_NOT_INSTALLED, (GpuDevice("RTX", "NVIDIA"),)
    )
    assert plan_setup(nvidia).package == "cupy-cuda12x"
    none = plan_setup(GpuDetectionResult(GpuStatus.CUDA_UNAVAILABLE))
    assert none.package is None and "CPU" in none.text()


def test_pip_command_is_a_fixed_argument_list() -> None:
    cmd = pip_command("dpnp")
    assert cmd[0] == sys.executable and cmd[1:4] == ["-m", "pip", "install"]
    with pytest.raises(ValueError):
        pip_command("dpnp; rm -rf /")


def test_light_probe_never_imports_gpu_runtimes() -> None:
    before = {m for m in ("cupy", "dpnp", "dpctl") if m in sys.modules}
    probe_gpu_light.cache_clear()
    probe_gpu_light()
    after = {m for m in ("cupy", "dpnp", "dpctl") if m in sys.modules}
    assert after == before
