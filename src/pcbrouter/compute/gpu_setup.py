"""One-click GPU setup: what does this machine need, and install it.

Used by Tools ▸ Set Up GPU… and ``pcbrouter --setup-gpu``. The only command ever
run is a fixed ``<this python> -m pip install <package>`` for one of two known
packages (argument list, no shell); nothing comes from boards, files or AI output.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass

from pcbrouter.compute.detection import GpuDetectionResult

PACKAGES = {"NVIDIA": "cupy-cuda12x", "Intel": "dpnp"}
MODULES = {"cupy-cuda12x": "cupy", "dpnp": "dpnp"}
DRIVER_HINT = {
    "NVIDIA": "an NVIDIA driver with CUDA 12 support (nvidia.com/drivers)",
    "Intel": "the latest Intel graphics driver (intel.com/iris-xe-drivers or Intel "
    "Driver & Support Assistant) — it contains the GPU compute runtime",
}


@dataclass
class GpuSetupPlan:
    vendor: str | None  # "NVIDIA" | "Intel" | None
    device: str | None
    package: str | None
    installed: bool
    can_install: bool
    steps: list[str]

    @property
    def ready(self) -> bool:
        return self.package is not None and self.installed

    def text(self) -> str:
        head = (
            f"GPU: {self.device} ({self.vendor})"
            if self.device
            else "No NVIDIA or Intel GPU was found on this computer."
        )
        return head + "\n" + "\n".join(f"• {s}" for s in self.steps)


def can_install_packages() -> tuple[bool, str]:
    if getattr(sys, "frozen", False):
        return False, (
            "This is the installed (Setup.exe) build: it cannot add Python packages. "
            "GPU routing needs the Python install (see docs/stage6.md, 'Using the GPU')."
        )
    if importlib.util.find_spec("pip") is None:
        return False, "pip is not available in this Python environment"
    return True, ""


def pip_command(package: str) -> list[str]:
    if package not in MODULES:
        raise ValueError(f"not a supported GPU package: {package!r}")
    return [sys.executable, "-m", "pip", "install", "--upgrade", package]


def plan_setup(detection: GpuDetectionResult) -> GpuSetupPlan:
    gpu = next((d for d in detection.devices if d.vendor == "NVIDIA"), None) or next(
        (d for d in detection.devices if d.vendor == "Intel"), None
    )
    ok, why = can_install_packages()
    if gpu is None:
        return GpuSetupPlan(
            None,
            None,
            None,
            False,
            ok,
            [
                "Routing runs on the CPU — nothing to install.",
                "GPU routing supports NVIDIA (CUDA) and Intel (Iris Xe / Arc) GPUs.",
            ],
        )
    package = PACKAGES[gpu.vendor]
    installed = importlib.util.find_spec(MODULES[package]) is not None
    steps = [f"Driver: install or update {DRIVER_HINT[gpu.vendor]}."]
    if installed:
        steps.append(f"GPU library '{package}' is installed.")
    elif ok:
        steps.append(f"Click 'Install GPU support' (runs: pip install {package}).")
    else:
        steps.append(why)
    steps += [
        "Choose GPU in Settings ▸ Compute (the button below does it for you).",
        "Open a board and use Tools ▸ Test GPU on This Board to check it works.",
    ]
    return GpuSetupPlan(gpu.vendor, gpu.name, package, installed, ok, steps)
