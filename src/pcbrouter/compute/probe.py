"""Hardware probe that gates every GPU job (self-test, benchmark, acceleration).

Before any GPU work is scheduled, :func:`probe_gpu` answers one question: is there
a usable GPU *device* right now? It never raises and never imports heavy GPU
libraries unless a driver-level check suggests a device exists:

1. CUDA: ``nvidia-smi -L`` lists devices (driver present) and, if CuPy is
   installed, ``cupy.cuda.runtime.getDeviceCount()`` confirms them;
2. Intel oneAPI: the display adapter list shows an Intel GPU and, if dpnp/dpctl
   is installed, a SYCL GPU device can be created.

No device → ``GpuProbe.available is False`` with a reason; callers must then mark
the GPU job **SKIPPED** (not failed) and use the CPU.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from pcbrouter.compute.detection import detect_gpu

log = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 5.0
SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class GpuProbe:
    available: bool
    library: str | None  # "cupy" | "dpnp" | None
    devices: tuple[str, ...] = ()
    reason: str = ""
    checks: tuple[str, ...] = field(default_factory=tuple)

    @property
    def status(self) -> str:
        return "AVAILABLE" if self.available else SKIPPED

    def summary(self) -> str:
        if self.available:
            return f"GPU available via {self.library}: {', '.join(self.devices) or '?'}"
        return f"GPU {SKIPPED}: {self.reason}"


def _cuda_devices(deep: bool = True) -> tuple[list[str], list[str]]:
    checks: list[str] = []
    exe = shutil.which("nvidia-smi")
    if exe is None:
        checks.append("nvidia-smi not found (no NVIDIA driver)")
        return [], checks
    try:
        out = subprocess.run(
            [exe, "-L"], capture_output=True, text=True, timeout=PROBE_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        checks.append(f"nvidia-smi failed: {exc!r}")
        return [], checks
    names = [
        ln.split(":", 1)[1].split("(")[0].strip()
        for ln in out.stdout.splitlines()
        if ln.startswith("GPU ") and ":" in ln
    ]
    checks.append(f"nvidia-smi: {len(names)} device(s)")
    if deep and names and importlib.util.find_spec("cupy") is not None:
        from pcbrouter.compute.gpu_runtime import prepare_gpu_runtime

        prepare_gpu_runtime()
        try:
            cp = importlib.import_module("cupy")
            count = int(cp.cuda.runtime.getDeviceCount())
            checks.append(f"CuPy runtime: {count} device(s)")
            if count == 0:
                return [], checks
        except Exception as exc:  # driver/runtime mismatch etc.
            checks.append(f"CuPy runtime check failed: {exc!r}")
            return [], checks
    return names, checks


def _oneapi_devices(deep: bool = True) -> tuple[list[str], list[str]]:
    checks: list[str] = []
    det = detect_gpu()
    intel = [d.name for d in det.devices if d.vendor == "Intel"]
    if not intel:
        checks.append("no Intel GPU adapter found")
        return [], checks
    checks.append(f"Intel adapter(s): {', '.join(intel)}")
    if deep and importlib.util.find_spec("dpctl") is not None:
        from pcbrouter.compute.gpu_runtime import prepare_gpu_runtime

        prepare_gpu_runtime()
        try:
            dpctl = importlib.import_module("dpctl")
            devices = dpctl.get_devices(device_type="gpu")
            checks.append(f"dpctl: {len(devices)} SYCL GPU device(s)")
            if not devices:
                return [], checks
            return [str(getattr(d, "name", "Intel GPU")) for d in devices], checks
        except Exception as exc:
            checks.append(f"dpctl check failed: {exc!r}")
            return [], checks
    return intel, checks


@lru_cache(maxsize=1)
def probe_gpu() -> GpuProbe:
    """Cached full probe (imports CuPy/dpctl to confirm devices). Use it only where
    GPU work will run — the routing worker process. Call :func:`reset_probe` after
    installing drivers/libraries."""
    return _probe(deep=True)


@lru_cache(maxsize=1)
def probe_gpu_light() -> GpuProbe:
    """Probe for the GUI process: driver-level checks and "is the package
    installed" only. Never imports CuPy/dpnp/dpctl, so no GPU runtime is ever
    initialised in the GUI (a vendor runtime crash cannot take the window down)."""
    return _probe(deep=False)


def _probe(deep: bool) -> GpuProbe:
    try:
        cuda, checks = _cuda_devices(deep)
        if cuda:
            has_lib = importlib.util.find_spec("cupy") is not None
            if not has_lib:
                return GpuProbe(
                    False,
                    None,
                    tuple(cuda),
                    "CUDA device present but CuPy is not installed",
                    tuple(checks),
                )
            return GpuProbe(True, "cupy", tuple(cuda), "", tuple(checks))
        intel, more = _oneapi_devices(deep)
        checks += more
        if intel:
            if importlib.util.find_spec("dpnp") is None:
                return GpuProbe(
                    False,
                    None,
                    tuple(intel),
                    "Intel GPU present but dpnp is not installed",
                    tuple(checks),
                )
            return GpuProbe(True, "dpnp", tuple(intel), "", tuple(checks))
        return GpuProbe(False, None, (), "no CUDA or oneAPI GPU device found", tuple(checks))
    except Exception as exc:  # the probe must never break the application
        log.warning("gpu.probe_failed error=%r", exc)
        return GpuProbe(False, None, (), f"probe error: {exc!r}")


def gpu_library_report() -> dict[str, Any]:
    """Which copy of the app is this, and can it load the GPU library? (No GUI.)"""
    import sys

    from pcbrouter import __version__
    from pcbrouter.compute.gpu_runtime import prepare_gpu_runtime, runtime_dirs

    report: dict[str, Any] = {
        "app": (
            "installed app (Setup.exe build)"
            if getattr(sys, "frozen", False)
            else f"Python install: {sys.executable}"
        ),
        "version": __version__,
        "runtime_dirs": [str(d) for d in runtime_dirs()],
    }
    prepare_gpu_runtime()
    lib = next((m for m in ("dpnp", "cupy") if importlib.util.find_spec(m) is not None), None)
    report["library"] = lib
    report["library_loads"] = False
    if lib is None:
        report["problem"] = "no GPU library in THIS copy of the app" + (
            " (this installer build has none bundled)"
            if getattr(sys, "frozen", False)
            else f"; install it into this Python: {sys.executable} -m pip install dpnp"
        )
        return report
    try:
        mod = importlib.import_module(lib)
        report["library_loads"] = True
        report["library_version"] = getattr(mod, "__version__", "?")
    except Exception as exc:  # DLL/runtime problems are reported, not raised
        report["problem"] = f"{lib} is present but failed to load: {exc!r}"
        return report
    if lib == "dpnp":
        try:
            dpctl = importlib.import_module("dpctl")

            devices = dpctl.get_devices()
            report["sycl_devices"] = [f"{d.name} ({d.device_type})" for d in devices]
            gpus = [d for d in devices if "gpu" in str(d.device_type)]
            report["gpu_device_found"] = bool(gpus)
            if gpus:
                x = mod.arange(1000, dtype=mod.float32, device=gpus[0])
                report["gpu_compute_test"] = float(x.sum()) == 499500.0
            else:
                report["problem"] = (
                    "dpnp loads but no SYCL GPU device: install/update the Intel graphics "
                    "driver (it provides the GPU compute runtime)"
                )
        except Exception as exc:
            report["problem"] = f"device query failed: {exc!r}"
    return report


def reset_probe() -> None:
    probe_gpu.cache_clear()
    probe_gpu_light.cache_clear()


class GpuJobSkipped(RuntimeError):  # noqa: N818 - a skip, not an error
    """Raised by :func:`require_gpu` when no GPU device is present: callers report
    the job as SKIPPED (never as a failure) and continue on the CPU."""

    def __init__(self, job: str, probe: GpuProbe) -> None:
        super().__init__(f"{job}: {SKIPPED} — {probe.reason}")
        self.job = job
        self.probe = probe


def gpu_gate(job: str, probe: GpuProbe | None = None) -> GpuProbe:
    """THE gate for every GPU-dependent job (self-test, benchmark, acceleration,
    any future GPU stage): probe the hardware first and log the decision. Returns
    the probe; ``probe.available`` False means the job must be SKIPPED."""
    result = probe if probe is not None else probe_gpu()
    if result.available:
        log.info(
            "gpu.gate job=%s status=AVAILABLE library=%s devices=%s",
            job,
            result.library,
            ", ".join(result.devices),
        )
    else:
        log.info("gpu.gate job=%s status=%s reason=%s", job, SKIPPED, result.reason)
    return result


def require_gpu(job: str, probe: GpuProbe | None = None) -> GpuProbe:
    """Like :func:`gpu_gate` but raises :class:`GpuJobSkipped` when unavailable."""
    result = gpu_gate(job, probe)
    if not result.available:
        raise GpuJobSkipped(job, result)
    return result


def main(argv: list[str] | None = None) -> int:
    """``python -m pcbrouter.compute.probe [--github-output PATH]``: print the probe
    and (for CI) write ``available=true|false`` and ``reason=…`` to the given file.
    Always exits 0 — "no GPU" is a SKIPPED job, not a failure."""
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Probe for a usable GPU device.")
    parser.add_argument("--github-output", default=None)
    parser.add_argument("--job", default="ci-gpu-stage")
    args = parser.parse_args(argv)
    result = gpu_gate(args.job)
    print(  # noqa: T201 - command-line output
        json.dumps(
            {
                "status": result.status,
                "library": result.library,
                "devices": list(result.devices),
                "reason": result.reason,
                "checks": list(result.checks),
            },
            indent=1,
        )
    )
    if args.github_output:
        reason = result.reason.replace("\n", " ")
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write(f"available={'true' if result.available else 'false'}\n")
            fh.write(f"reason={reason}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
