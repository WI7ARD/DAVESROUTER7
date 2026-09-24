"""GPU compute backend — **placeholder architecture only** (Stage 1).

This class reports what detection found so the UI can show it, but it is never
``available`` and :meth:`initialize` always raises. GPU routing kernels are planned
for a later stage (see docs/roadmap.md). It does not pretend to work.
"""

from __future__ import annotations

from pcbrouter.compute.backend import (
    BackendKind,
    BackendUnavailableError,
    Capability,
    ComputeBackend,
    DeviceInfo,
)
from pcbrouter.compute.detection import GpuDetectionResult, GpuStatus

GPU_NOT_IMPLEMENTED_NOTE = "GPU acceleration is available in a later stage (not configured yet)."


class GPUBackend(ComputeBackend):
    def __init__(self, detection: GpuDetectionResult) -> None:
        self._detection = detection

    @property
    def name(self) -> str:
        return "GPU (CUDA)"

    @property
    def kind(self) -> BackendKind:
        return BackendKind.GPU

    @property
    def available(self) -> bool:
        # Hardware may be present, but no GPU kernels exist yet.
        return False

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset()

    @property
    def initialized(self) -> bool:
        return False

    @property
    def detection(self) -> GpuDetectionResult:
        return self._detection

    def device_info(self) -> DeviceInfo:
        det = self._detection
        device = det.cuda_device or (det.devices[0] if det.devices else None)
        details = {
            "Status": det.status.description,
            "CUDA": "Not configured yet",
            "GPU Python packages": ", ".join(det.installed_packages) or "none installed",
        }
        if device is not None:
            if device.driver_version:
                details["Driver"] = device.driver_version
            if device.memory:
                details["Memory"] = device.memory
        return DeviceInfo(
            backend=BackendKind.GPU,
            name=device.name if device else "No GPU candidate",
            details=details,
            notes=(*det.notes, GPU_NOT_IMPLEMENTED_NOTE),
        )

    def initialize(self) -> None:
        reason = (
            "hardware detected" if self._detection.status is GpuStatus.CUDA_DEVICE_DETECTED
            else self._detection.status.description
        )  # fmt: skip
        raise BackendUnavailableError(
            f"GPU backend unavailable ({reason}). {GPU_NOT_IMPLEMENTED_NOTE}"
        )

    def shutdown(self) -> None:
        return None
