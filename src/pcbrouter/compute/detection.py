"""Hardware detection for CPU and (candidate) GPUs.

Detection must never raise, never import heavy GPU libraries (we only check that
they are *installed* via ``importlib.util.find_spec``), and never block for long:
every subprocess has a short timeout. All external interactions are injectable so
the logic is unit-testable on machines without GPUs.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)

SUBPROCESS_TIMEOUT_S = 3.0
#: Python packages a GPU backend can build on (CuPy: NVIDIA CUDA; dpnp: Intel oneAPI).
CUDA_PYTHON_PACKAGES: tuple[str, ...] = ("cupy", "numba", "torch", "cuda", "dpnp")
#: Subset that provides a usable array backend: only these count as "device ready".
#: torch/numba/cuda alone must not report CUDA_DEVICE_DETECTED (they cannot run
#: the wavefront kernel); the app then falls back to CPU with an honest reason.
GPU_ARRAY_PACKAGES: tuple[str, ...] = ("cupy", "dpnp")

_PCI_VENDORS = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel"}

Runner = Callable[[Sequence[str]], str | None]
Which = Callable[[str], str | None]
SpecFinder = Callable[[str], bool]


# ------------------------------------------------------------------ CPU
@dataclass(frozen=True, slots=True)
class CpuInfo:
    model: str
    logical_threads: int | None
    architecture: str
    system: str


def _linux_cpu_model() -> str | None:
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                key, _, value = line.partition(":")
                if key.strip() in ("model name", "Hardware", "Processor") and value.strip():
                    return value.strip()
    except OSError:
        return None
    return None


def _windows_cpu_model() -> str | None:
    try:
        import winreg

        key = winreg.OpenKey(  # type: ignore[attr-defined, unused-ignore]
            winreg.HKEY_LOCAL_MACHINE,  # type: ignore[attr-defined, unused-ignore]
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        )
        value, _ = winreg.QueryValueEx(key, "ProcessorNameString")  # type: ignore[attr-defined, unused-ignore]
        return str(value).strip() or None
    except Exception:
        return None


def detect_cpu() -> CpuInfo:
    model: str | None = None
    if sys.platform.startswith("linux"):
        model = _linux_cpu_model()
    elif sys.platform == "win32":
        model = _windows_cpu_model()
    model = model or platform.processor() or "Unknown CPU"
    return CpuInfo(
        model=model,
        logical_threads=os.cpu_count(),
        architecture=platform.machine() or "unknown",
        system=f"{platform.system()} {platform.release()}".strip(),
    )


# ------------------------------------------------------------------ GPU
class GpuStatus(Enum):
    CUDA_DEVICE_DETECTED = "cuda_device_detected"
    GPU_LIBRARIES_NOT_INSTALLED = "gpu_libraries_not_installed"
    UNSUPPORTED_GPU = "unsupported_gpu"
    ONEAPI_DEVICE_DETECTED = "oneapi_device_detected"  # Intel GPU + dpnp installed
    ONEAPI_LIBRARIES_NOT_INSTALLED = "oneapi_libraries_not_installed"  # Intel GPU, no dpnp
    CUDA_UNAVAILABLE = "cuda_unavailable"
    DETECTION_ERROR = "detection_error"
    PENDING = "pending"

    @property
    def short_label(self) -> str:
        """Compact form for the status bar."""
        return {
            GpuStatus.CUDA_DEVICE_DETECTED: "CUDA device (not used yet)",
            GpuStatus.GPU_LIBRARIES_NOT_INSTALLED: "CUDA device, no GPU libs",
            GpuStatus.UNSUPPORTED_GPU: "unsupported GPU",
            GpuStatus.ONEAPI_DEVICE_DETECTED: "Intel GPU (oneAPI)",
            GpuStatus.ONEAPI_LIBRARIES_NOT_INSTALLED: "Intel GPU, no dpnp",
            GpuStatus.CUDA_UNAVAILABLE: "CUDA unavailable",
            GpuStatus.DETECTION_ERROR: "detection failed",
            GpuStatus.PENDING: "detecting…",
        }[self]

    @property
    def description(self) -> str:
        return {
            GpuStatus.CUDA_DEVICE_DETECTED: "CUDA-capable device detected",
            GpuStatus.GPU_LIBRARIES_NOT_INSTALLED:
                "CUDA-capable device detected, but GPU Python libraries are not installed",
            GpuStatus.UNSUPPORTED_GPU:
                "GPU found, but it is not a supported (NVIDIA CUDA or Intel oneAPI) device",
            GpuStatus.ONEAPI_DEVICE_DETECTED:
                "Intel GPU detected and the oneAPI dpnp package is installed (experimental)",
            GpuStatus.ONEAPI_LIBRARIES_NOT_INSTALLED:
                "Intel GPU detected; install the optional dpnp package (Intel oneAPI) to use it",
            GpuStatus.CUDA_UNAVAILABLE: "CUDA unavailable (no NVIDIA driver/device found)",
            GpuStatus.DETECTION_ERROR: "GPU detection failed",
            GpuStatus.PENDING: "GPU detection in progress",
        }[self]  # fmt: skip


@dataclass(frozen=True, slots=True)
class GpuDevice:
    name: str
    vendor: str
    driver_version: str | None = None
    memory: str | None = None


@dataclass(frozen=True, slots=True)
class GpuDetectionResult:
    status: GpuStatus
    devices: tuple[GpuDevice, ...] = ()
    installed_packages: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def cuda_device(self) -> GpuDevice | None:
        return next((d for d in self.devices if d.vendor == "NVIDIA"), None)

    @property
    def array_module(self) -> str | None:
        """Which GPU array library applies: "cupy" (NVIDIA), "dpnp" (Intel)."""
        if self.status is GpuStatus.CUDA_DEVICE_DETECTED:
            return "cupy"
        if self.status is GpuStatus.ONEAPI_DEVICE_DETECTED:
            return "dpnp"
        return None

    def summary(self) -> str:
        names = ", ".join(d.name for d in self.devices) or "none"
        return f"{self.status.description} (devices: {names})"


def default_runner(cmd: Sequence[str]) -> str | None:
    """Run a command and return stdout, or ``None`` on any failure/timeout."""
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash on Windows
        proc = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
            check=False,
            creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def default_spec_finder(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _nvidia_smi_devices(which: Which, run: Runner) -> list[GpuDevice]:
    exe = which("nvidia-smi")
    if not exe:
        return []
    out = run([exe, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"])
    devices = []
    for line in (out or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if parts and parts[0]:
            devices.append(
                GpuDevice(
                    name=parts[0],
                    vendor="NVIDIA",
                    driver_version=parts[1] if len(parts) > 1 else None,
                    memory=parts[2] if len(parts) > 2 else None,
                )
            )
    return devices


def _linux_pci_display_devices(drm_root: Path = Path("/sys/class/drm")) -> list[GpuDevice]:
    devices: list[GpuDevice] = []
    try:
        cards = sorted(drm_root.glob("card[0-9]*"))
    except OSError:
        return devices
    for card in cards:
        if "-" in card.name:  # connectors like card0-HDMI-A-1
            continue
        vendor_file = card / "device" / "vendor"
        try:
            vendor_id = vendor_file.read_text(encoding="ascii").strip().lower()
        except OSError:
            continue
        vendor = _PCI_VENDORS.get(vendor_id, f"PCI vendor {vendor_id}")
        devices.append(GpuDevice(name=f"{vendor} display adapter ({card.name})", vendor=vendor))
    return devices


def _windows_display_devices(run: Runner) -> list[GpuDevice]:
    out = run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }",
        ]
    )
    devices = []
    for line in (out or "").splitlines():
        name = line.strip()
        if not name:
            continue
        upper = name.upper()
        vendor = (
            "NVIDIA" if "NVIDIA" in upper
            else "AMD" if ("AMD" in upper or "RADEON" in upper)
            else "Intel" if "INTEL" in upper
            else "Unknown"
        )  # fmt: skip
        devices.append(GpuDevice(name=name, vendor=vendor))
    return devices


def detect_gpu(
    *,
    which: Which = shutil.which,
    run: Runner = default_runner,
    has_package: SpecFinder = default_spec_finder,
    platform_name: str | None = None,
    other_devices: Callable[[], list[GpuDevice]] | None = None,
) -> GpuDetectionResult:
    """Detect GPU candidates. Never raises; failures yield ``DETECTION_ERROR``."""
    try:
        installed = tuple(p for p in CUDA_PYTHON_PACKAGES if has_package(p))
        usable = tuple(p for p in GPU_ARRAY_PACKAGES if has_package(p))
        nvidia = _nvidia_smi_devices(which, run)
        if nvidia:
            status = (
                GpuStatus.CUDA_DEVICE_DETECTED if usable
                else GpuStatus.GPU_LIBRARIES_NOT_INSTALLED
            )  # fmt: skip
            return GpuDetectionResult(status, tuple(nvidia), installed)

        plat = platform_name or sys.platform
        if other_devices is not None:
            others = other_devices()
        elif plat.startswith("linux"):
            others = _linux_pci_display_devices()
        elif plat == "win32":
            others = _windows_display_devices(run)
        else:
            others = []

        notes: tuple[str, ...] = ()
        if any(d.vendor == "NVIDIA" for d in others):
            notes = (
                "NVIDIA hardware present but nvidia-smi is unavailable; "
                "the NVIDIA driver may not be installed.",
            )
            return GpuDetectionResult(GpuStatus.CUDA_UNAVAILABLE, tuple(others), installed, notes)
        if any(d.vendor == "Intel" for d in others):
            status = (
                GpuStatus.ONEAPI_DEVICE_DETECTED if "dpnp" in installed
                else GpuStatus.ONEAPI_LIBRARIES_NOT_INSTALLED
            )  # fmt: skip
            return GpuDetectionResult(status, tuple(others), installed)
        if others:
            return GpuDetectionResult(GpuStatus.UNSUPPORTED_GPU, tuple(others), installed)
        return GpuDetectionResult(GpuStatus.CUDA_UNAVAILABLE, (), installed)
    except Exception as exc:  # detection must never break startup
        log.warning("compute.gpu_detection_failed error=%r", exc)
        return GpuDetectionResult(GpuStatus.DETECTION_ERROR, notes=(repr(exc),))
