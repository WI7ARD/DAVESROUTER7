"""Compute backends (CPU now, GPU later) and hardware detection."""

from __future__ import annotations

from pcbrouter.compute.backend import (
    BackendKind,
    BackendUnavailableError,
    Capability,
    ComputeBackend,
    DeviceInfo,
)
from pcbrouter.compute.cpu_backend import CPUBackend
from pcbrouter.compute.detection import (
    CpuInfo,
    GpuDetectionResult,
    GpuDevice,
    GpuStatus,
    detect_cpu,
    detect_gpu,
)
from pcbrouter.compute.gpu_backend import GPUBackend
from pcbrouter.compute.manager import ComputeManager

__all__ = [
    "BackendKind",
    "BackendUnavailableError",
    "CPUBackend",
    "Capability",
    "ComputeBackend",
    "ComputeManager",
    "CpuInfo",
    "DeviceInfo",
    "GPUBackend",
    "GpuDetectionResult",
    "GpuDevice",
    "GpuStatus",
    "detect_cpu",
    "detect_gpu",
]
