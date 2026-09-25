"""GPU compute backend (Stage 6): optional — NVIDIA CUDA via CuPy, or Intel GPUs
(e.g. Iris Xe, Arc) via oneAPI dpnp. Both are NumPy-compatible array libraries, so
the same wavefront kernel runs on either.

``available`` is True only when CuPy imports **and** a CUDA device can actually be
used (a tiny kernel is run in :meth:`initialize`). Nothing here is required to
launch the application: without CuPy/CUDA the backend reports why and the CPU is
used. The GPU never decides PCB legality — it accelerates the wavefront search;
every route still passes the CPU exact validator (see docs/stage6.md).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pcbrouter.compute.backend import (
    BackendKind,
    BackendUnavailableError,
    Capability,
    ComputeBackend,
    DeviceInfo,
)
from pcbrouter.compute.detection import GpuDetectionResult
from pcbrouter.compute.probe import GpuProbe, gpu_gate, probe_gpu

log = logging.getLogger(__name__)

GPU_NOT_IMPLEMENTED_NOTE = (
    "GPU acceleration is optional: NVIDIA GPUs need a CUDA driver and CuPy "
    "(pip install 'ai-pcb-router[gpu-cuda]'); Intel GPUs need the oneAPI GPU runtime "
    "and dpnp (pip install 'ai-pcb-router[gpu-intel]'). The CPU router works without."
)
#: bytes per grid cell per layer on the device (dist, step f32; passable, target,
#: via, diagonal masks; temporaries) with a 2x safety factor
BYTES_PER_CELL_LAYER = 64
VRAM_HEADROOM = 0.7  # use at most 70 % of the free device memory


def import_array_module(name: str) -> Any:
    """Import "cupy" or "dpnp" (optional dependencies; ImportError handled by callers)."""
    import importlib

    if name not in ("cupy", "dpnp"):
        raise ImportError(f"unknown GPU array module {name!r}")
    return importlib.import_module(name)


class GPUBackend(ComputeBackend):
    def __init__(
        self,
        detection: GpuDetectionResult,
        loader: Any = import_array_module,
        probe: Callable[[], GpuProbe] | None = None,
    ) -> None:
        self._detection = detection
        self._loader = loader
        #: hardware gate: with no device the GPU is never initialised (job SKIPPED)
        self._probe = probe or probe_gpu
        self._xp: Any = None
        self._error: str | None = None
        self._free_bytes: int | None = None
        self._total_bytes: int | None = None
        self._device_name: str | None = None

    @property
    def name(self) -> str:
        return {"dpnp": "GPU (Intel oneAPI)"}.get(self._detection.array_module or "", "GPU (CUDA)")

    @property
    def kind(self) -> BackendKind:
        return BackendKind.GPU

    @property
    def available(self) -> bool:
        if self._xp is not None:
            return True
        if self._detection.array_module is None or not self._probe().available:
            return False
        try:
            self.initialize()
        except BackendUnavailableError:
            return False
        return True

    @property
    def capabilities(self) -> frozenset[Capability]:
        if self._xp is None:
            return frozenset()
        return frozenset({Capability.GRID_ROUTING, Capability.BATCH_PATHFINDING})

    @property
    def initialized(self) -> bool:
        return self._xp is not None

    @property
    def detection(self) -> GpuDetectionResult:
        return self._detection

    @property
    def xp(self) -> Any:
        if self._xp is None:
            raise BackendUnavailableError(self._error or "GPU backend not initialised")
        return self._xp

    @property
    def last_error(self) -> str | None:
        return self._error

    def free_memory(self) -> int | None:
        if self._xp is None:
            return None
        if self._detection.array_module != "cupy":
            return None
        try:
            free, total = self._xp.cuda.runtime.memGetInfo()
            self._free_bytes, self._total_bytes = int(free), int(total)
        except Exception as exc:  # pragma: no cover - needs CUDA
            log.warning("gpu.meminfo_failed error=%r", exc)
        return self._free_bytes

    def fits(self, cells: int, layers: int) -> tuple[bool, str]:
        """Estimated VRAM check for a search grid (prevents out-of-memory)."""
        need = cells * layers * BYTES_PER_CELL_LAYER
        free = self.free_memory()
        if free is None:
            # dpnp has no portable free-memory query; integrated GPUs share system
            # RAM, so use a fixed conservative cap instead of guessing
            if self._xp is not None and self._detection.array_module == "dpnp":
                cap = 1 << 30
                ok = need <= cap
                return ok, f"needs ~{need / 2**20:.0f} MiB (cap {cap >> 20} MiB, shared memory)"
            return False, "free GPU memory unknown"
        if need > free * VRAM_HEADROOM:
            return False, f"needs ~{need / 2**20:.0f} MiB, {free / 2**20:.0f} MiB free"
        return True, f"~{need / 2**20:.0f} MiB of {free / 2**20:.0f} MiB free"

    def device_info(self) -> DeviceInfo:
        det = self._detection
        device = det.cuda_device or (det.devices[0] if det.devices else None)
        details = {
            "Status": det.status.description,
            "Array library": (
                f"ready ({self._detection.array_module})"
                if self._xp is not None
                else (self._error or "not initialised")
            ),
            "GPU Python packages": ", ".join(det.installed_packages) or "none installed",
        }
        if self._total_bytes:
            details["Device memory"] = f"{self._total_bytes / 2**30:.1f} GiB"
        if device is not None:
            if device.driver_version:
                details["Driver"] = device.driver_version
            if device.memory:
                details["Memory"] = device.memory
        notes = det.notes if self._xp is not None else (*det.notes, GPU_NOT_IMPLEMENTED_NOTE)
        return DeviceInfo(
            backend=BackendKind.GPU,
            name=self._device_name or (device.name if device else "No GPU candidate"),
            details=details,
            notes=tuple(notes),
        )

    def initialize(self) -> None:
        if self._xp is not None:
            return
        gate = gpu_gate("gpu-backend-init", self._probe())
        if not gate.available:
            self._error = gate.summary()
            raise BackendUnavailableError(f"{self._error} {GPU_NOT_IMPLEMENTED_NOTE}")
        module = self._detection.array_module or gate.library
        if module is None:
            self._error = f"GPU backend unavailable ({self._detection.status.description})."
            raise BackendUnavailableError(f"{self._error} {GPU_NOT_IMPLEMENTED_NOTE}")
        try:
            cp = self._loader(module)
            probe = cp.arange(16, dtype=cp.float32)
            if float((probe * 2).sum()) != 240.0:  # tiny kernel: device really works
                raise RuntimeError("GPU self-test returned a wrong result")
            if module == "cupy":
                props = cp.cuda.runtime.getDeviceProperties(0)
                name = props.get("name", b"CUDA device")
                self._device_name = name.decode() if isinstance(name, bytes) else str(name)
            else:
                dev = getattr(probe, "sycl_device", None)
                self._device_name = str(getattr(dev, "name", "Intel GPU (oneAPI)"))
        except Exception as exc:
            self._error = f"GPU backend unavailable ({exc!r})."
            log.warning("gpu.init_failed error=%r", exc)
            raise BackendUnavailableError(f"{self._error} {GPU_NOT_IMPLEMENTED_NOTE}") from exc
        self._xp = cp
        self._error = None
        self.free_memory()
        log.info(
            "gpu.initialized device=%s free_mib=%s",
            self._device_name,
            (self._free_bytes or 0) // 2**20,
        )

    def shutdown(self) -> None:
        if self._xp is not None and self._detection.array_module == "cupy":
            try:
                self._xp.get_default_memory_pool().free_all_blocks()
            except Exception:  # pragma: no cover - needs CUDA
                log.debug("gpu.pool_free_failed", exc_info=True)
