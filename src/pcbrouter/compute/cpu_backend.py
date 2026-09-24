"""CPU compute backend — always available."""

from __future__ import annotations

import logging

from pcbrouter.compute.backend import BackendKind, Capability, ComputeBackend, DeviceInfo
from pcbrouter.compute.detection import CpuInfo, detect_cpu

log = logging.getLogger(__name__)


class CPUBackend(ComputeBackend):
    def __init__(self, cpu_info: CpuInfo | None = None) -> None:
        self._cpu = cpu_info or detect_cpu()
        self._initialized = False

    @property
    def name(self) -> str:
        return "CPU"

    @property
    def kind(self) -> BackendKind:
        return BackendKind.CPU

    @property
    def available(self) -> bool:
        return True

    @property
    def capabilities(self) -> frozenset[Capability]:
        caps = {Capability.GEOMETRY}
        if (self._cpu.logical_threads or 1) > 1:
            caps.add(Capability.MULTITHREADING)
        return frozenset(caps)

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def cpu_info(self) -> CpuInfo:
        return self._cpu

    def device_info(self) -> DeviceInfo:
        threads = self._cpu.logical_threads
        return DeviceInfo(
            backend=BackendKind.CPU,
            name=self._cpu.model,
            details={
                "Threads": str(threads) if threads is not None else "unknown",
                "Architecture": self._cpu.architecture,
                "System": self._cpu.system,
            },
        )

    def initialize(self) -> None:
        if not self._initialized:
            self._initialized = True
            log.info(
                "compute.cpu.initialized model=%r threads=%s",
                self._cpu.model, self._cpu.logical_threads,
            )  # fmt: skip

    def shutdown(self) -> None:
        if self._initialized:
            self._initialized = False
            log.info("compute.cpu.shutdown")
