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


def _cuda_devices() -> tuple[list[str], list[str]]:
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
    if names and importlib.util.find_spec("cupy") is not None:
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


def _oneapi_devices() -> tuple[list[str], list[str]]:
    checks: list[str] = []
    det = detect_gpu()
    intel = [d.name for d in det.devices if d.vendor == "Intel"]
    if not intel:
        checks.append("no Intel GPU adapter found")
        return [], checks
    checks.append(f"Intel adapter(s): {', '.join(intel)}")
    if importlib.util.find_spec("dpctl") is not None:
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
    """Cached probe (call :func:`reset_probe` after installing drivers/libraries)."""
    try:
        cuda, checks = _cuda_devices()
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
        intel, more = _oneapi_devices()
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


def reset_probe() -> None:
    probe_gpu.cache_clear()


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
