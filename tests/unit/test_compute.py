from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from pcbrouter.compute import (
    BackendKind,
    BackendUnavailableError,
    Capability,
    ComputeManager,
    CPUBackend,
    CpuInfo,
    GPUBackend,
    GpuDetectionResult,
    GpuDevice,
    GpuStatus,
    detect_cpu,
    detect_gpu,
)
from pcbrouter.compute.detection import _linux_pci_display_devices

SMI_OUTPUT = "NVIDIA GeForce RTX 4070, 555.42, 12282 MiB\n"


def fake_which(found: bool):  # type: ignore[no-untyped-def]
    return lambda name: "/usr/bin/nvidia-smi" if (found and name == "nvidia-smi") else None


def fake_run(output: str | None):  # type: ignore[no-untyped-def]
    def run(cmd: Sequence[str]) -> str | None:
        return output

    return run


class TestCPU:
    def test_detect_cpu(self) -> None:
        info = detect_cpu()
        assert info.model
        assert info.logical_threads == os.cpu_count()

    def test_cpu_backend_always_available(self) -> None:
        cpu = CPUBackend(CpuInfo("Test CPU", 16, "x86_64", "Linux"))
        assert cpu.available and cpu.kind is BackendKind.CPU
        assert Capability.MULTITHREADING in cpu.capabilities
        assert cpu.device_info().details["Threads"] == "16"
        cpu.initialize()
        cpu.initialize()
        assert cpu.initialized
        cpu.shutdown()
        cpu.shutdown()
        assert not cpu.initialized

    def test_unknown_thread_count(self) -> None:
        cpu = CPUBackend(CpuInfo("X", None, "arm64", "Linux"))
        assert cpu.device_info().details["Threads"] == "unknown"
        assert Capability.MULTITHREADING not in cpu.capabilities


class TestGpuDetection:
    def test_no_gpu_at_all(self) -> None:
        r = detect_gpu(
            which=fake_which(False),
            run=fake_run(None),
            has_package=lambda _: False,
            other_devices=list,
        )
        assert r.status is GpuStatus.CUDA_UNAVAILABLE
        assert r.devices == ()

    def test_nvidia_without_python_libraries(self) -> None:
        r = detect_gpu(
            which=fake_which(True), run=fake_run(SMI_OUTPUT), has_package=lambda _: False
        )
        assert r.status is GpuStatus.GPU_LIBRARIES_NOT_INSTALLED
        assert r.cuda_device is not None
        assert r.cuda_device.name == "NVIDIA GeForce RTX 4070"
        assert r.cuda_device.driver_version == "555.42"
        assert r.cuda_device.memory == "12282 MiB"

    def test_nvidia_with_libraries(self) -> None:
        r = detect_gpu(
            which=fake_which(True), run=fake_run(SMI_OUTPUT), has_package=lambda p: p == "cupy"
        )
        assert r.status is GpuStatus.CUDA_DEVICE_DETECTED
        assert r.installed_packages == ("cupy",)

    def test_nvidia_smi_failing_falls_back_to_other_devices(self) -> None:
        r = detect_gpu(
            which=fake_which(True),
            run=fake_run(None),
            has_package=lambda _: False,
            other_devices=lambda: [GpuDevice("Radeon RX 7800", "AMD")],
        )
        assert r.status is GpuStatus.UNSUPPORTED_GPU

    def test_nvidia_hardware_without_driver(self) -> None:
        r = detect_gpu(
            which=fake_which(False),
            run=fake_run(None),
            has_package=lambda _: False,
            other_devices=lambda: [GpuDevice("NVIDIA display adapter", "NVIDIA")],
        )
        assert r.status is GpuStatus.CUDA_UNAVAILABLE
        assert r.notes and "driver" in r.notes[0]

    def test_detection_never_raises(self) -> None:
        def boom(name: str) -> str | None:
            raise RuntimeError("simulated")

        r = detect_gpu(which=boom, run=fake_run(None), has_package=lambda _: False)
        assert r.status is GpuStatus.DETECTION_ERROR

    def test_real_machine_detection_does_not_raise(self) -> None:
        r = detect_gpu()
        assert isinstance(r.status, GpuStatus)
        assert r.status is not GpuStatus.PENDING

    def test_linux_sysfs_parsing(self, tmp_path: Path) -> None:
        for card, vendor in (("card0", "0x8086"), ("card1", "0x10de")):
            dev = tmp_path / card / "device"
            dev.mkdir(parents=True)
            (dev / "vendor").write_text(vendor + "\n")
        (tmp_path / "card0-HDMI-A-1").mkdir()
        devices = _linux_pci_display_devices(tmp_path)
        assert [d.vendor for d in devices] == ["Intel", "NVIDIA"]


class TestBackendsAndManager:
    def test_gpu_backend_is_a_placeholder(self) -> None:
        gpu = GPUBackend(
            GpuDetectionResult(
                GpuStatus.CUDA_DEVICE_DETECTED, (GpuDevice("RTX", "NVIDIA", "550", "8 GiB"),)
            )
        )
        assert not gpu.available
        assert gpu.capabilities == frozenset()
        info = gpu.device_info()
        assert info.name == "RTX"
        assert info.details["CUDA"] == "Not configured yet"
        assert any("later stage" in n for n in info.notes)
        with pytest.raises(BackendUnavailableError):
            gpu.initialize()
        gpu.shutdown()  # safe no-op

    def test_manager_falls_back_to_cpu(self) -> None:
        mgr = ComputeManager(gpu_detection=GpuDetectionResult(GpuStatus.CUDA_UNAVAILABLE))
        active = mgr.select(BackendKind.GPU)
        assert active is mgr.cpu and mgr.cpu.initialized
        assert mgr.fallback_reason is not None and "later stage" in mgr.fallback_reason
        assert mgr.select(BackendKind.CPU) is mgr.cpu
        assert mgr.fallback_reason is None
        mgr.shutdown()
        assert not mgr.cpu.initialized

    def test_background_detection_update(self) -> None:
        mgr = ComputeManager(gpu_detection=GpuDetectionResult(GpuStatus.PENDING))
        assert mgr.gpu.detection.status is GpuStatus.PENDING
        mgr.update_gpu_detection(GpuDetectionResult(GpuStatus.UNSUPPORTED_GPU))
        updated = mgr.gpu.detection
        assert updated.status is GpuStatus.UNSUPPORTED_GPU
