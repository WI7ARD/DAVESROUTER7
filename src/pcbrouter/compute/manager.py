"""Selects and owns the active compute backend.

Policy: the user's preferred backend is honoured only if it is available; otherwise
we fall back to the CPU (always available) and record why. Stage 1 therefore always
ends up on the CPU.
"""

from __future__ import annotations

import logging

from pcbrouter.compute.backend import BackendKind, BackendUnavailableError, ComputeBackend
from pcbrouter.compute.cpu_backend import CPUBackend
from pcbrouter.compute.detection import GpuDetectionResult, detect_gpu
from pcbrouter.compute.gpu_backend import GPUBackend

log = logging.getLogger(__name__)


class ComputeManager:
    def __init__(
        self,
        cpu: CPUBackend | None = None,
        gpu_detection: GpuDetectionResult | None = None,
    ) -> None:
        """``gpu_detection=None`` runs detection synchronously; pass a
        ``GpuStatus.PENDING`` result and call :meth:`update_gpu_detection` later to
        detect in the background instead."""
        self.cpu = cpu or CPUBackend()
        self.gpu = GPUBackend(gpu_detection if gpu_detection is not None else detect_gpu())
        self._active: ComputeBackend = self.cpu
        self.fallback_reason: str | None = None

    @property
    def active(self) -> ComputeBackend:
        return self._active

    @property
    def backends(self) -> tuple[ComputeBackend, ...]:
        return (self.cpu, self.gpu)

    def select(self, preferred: BackendKind) -> ComputeBackend:
        self.fallback_reason = None
        candidate: ComputeBackend = self.gpu if preferred is BackendKind.GPU else self.cpu
        if candidate is not self.cpu:
            try:
                candidate.initialize()
            except BackendUnavailableError as exc:
                self.fallback_reason = str(exc)
                log.warning("compute.fallback preferred=%s reason=%s", preferred.value, exc)
                candidate = self.cpu
        if candidate is self.cpu:
            self.cpu.initialize()
        if self._active is not candidate:
            self._active.shutdown()
        self._active = candidate
        log.info(
            "compute.selected backend=%s gpu_status=%s",
            candidate.name, self.gpu.detection.status.value,
        )  # fmt: skip
        return candidate

    def update_gpu_detection(self, detection: GpuDetectionResult) -> None:
        self.gpu = GPUBackend(detection)
        log.info("compute.gpu_detected %s", detection.summary())

    def shutdown(self) -> None:
        for backend in self.backends:
            backend.shutdown()
