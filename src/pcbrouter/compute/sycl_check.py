"""Staged Intel GPU (SYCL) diagnostic behind ``pcbrouter --gpu-check``.

Every stage says what it found or the exact exception; the verdict names the last
stage that really worked. A device object existing is *not* "GPU works":

    dpctl_import → dpnp_import → devices → gpu_selected → queue → allocation
      → operation → verified → fused_kernel → benchmark

The GPU is chosen explicitly (Level Zero preferred, then OpenCL, then
``dpctl.select_gpu_device()``) and every array is created on that queue, then
checked to live on that device — dpnp's default device can be a CPU.

Native SYCL calls can crash the process when a driver is broken, so the app runs
this module in a child process (``--gpu-probe-child``, see ``run_in_child``) and
reports a crash as a failed stage instead of dying.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

#: tried in order when no explicit target is given: Level Zero is Intel's native
#: GPU runtime; OpenCL is the fallback the same driver also provides
GPU_FILTERS = ("level_zero:gpu", "opencl:gpu")
CHILD_FLAG = "--gpu-probe-child"
CHILD_TIMEOUT_S = 180.0
STAGES = (
    "dpctl_import",
    "dpnp_import",
    "devices",
    "gpu_selected",
    "queue",
    "allocation",
    "operation",
    "verified",
    "fused_kernel",
    "benchmark",
)


@dataclass
class Stage:
    name: str
    ok: bool
    detail: str = ""
    error: str | None = None
    value: Any = None


@dataclass
class Diagnostic:
    target: str
    stages: list[Stage] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", error: str | None = None,
            value: Any = None) -> bool:  # fmt: skip
        self.stages.append(Stage(name, ok, detail, error, value))
        return ok

    @property
    def last_ok(self) -> str | None:
        done = None
        for st in self.stages:
            if not st.ok:
                break
            done = st.name
        return done

    @property
    def compute_verified(self) -> bool:
        return any(s.name == "verified" and s.ok for s in self.stages)

    def verdict(self) -> str:
        failed = next((s for s in self.stages if not s.ok), None)
        what = "GPU" if self.target == "gpu" else f"device {self.target}"
        if failed is None:
            return f"{what}: every stage passed (detected, computed, verified, benchmarked)"
        why = failed.detail or failed.error
        return f"{what}: stopped at '{failed.name}' ({why}); last passed: {self.last_ok}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "stages": [asdict(s) for s in self.stages],
            "last_passed": self.last_ok,
            "compute_verified": self.compute_verified,
            "verdict": self.verdict(),
        }


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:400]


def describe_device(dev: Any) -> dict[str, Any]:
    """Name, backend, type, driver, memory and filter string of a SYCL device."""

    def get(attr: str) -> Any:
        try:
            v = getattr(dev, attr)
            return v() if callable(v) else v
        except Exception:  # an attribute a backend does not expose
            return None

    backend = get("backend")
    dtype = get("device_type")
    mem = get("global_mem_size")
    return {
        "name": get("name"),
        "vendor": get("vendor"),
        "backend": str(backend).split(".")[-1] if backend is not None else None,
        "device_type": str(dtype).split(".")[-1] if dtype is not None else None,
        "driver_version": get("driver_version"),
        "global_mem_mb": round(mem / 2**20) if isinstance(mem, int) else None,
        "max_compute_units": get("max_compute_units"),
        "filter_string": get("filter_string"),
    }


def select_device(dpctl: Any, target: str) -> tuple[Any | None, str]:
    """(device, how) for ``target``: "gpu" (Level Zero → OpenCL → select_gpu_device)
    or an explicit filter string such as "opencl:cpu" (used by the tests)."""
    tried: list[str] = []
    filters = GPU_FILTERS if target == "gpu" else (target,)
    for f in filters:
        try:
            return dpctl.SyclDevice(f), f"filter {f}"
        except Exception as exc:  # not present: try the next one
            tried.append(f"{f}: {_err(exc)}")
    if target == "gpu":
        try:
            return dpctl.select_gpu_device(), "dpctl.select_gpu_device()"
        except Exception as exc:
            tried.append(f"select_gpu_device: {_err(exc)}")
    return None, "; ".join(tried)


def run_diagnostic(target: str = "gpu", bench: bool = True) -> Diagnostic:
    """Run every stage on ``target``; never raises."""
    from pcbrouter.compute.gpu_runtime import prepare_gpu_runtime

    diag = Diagnostic(target)
    prepare_gpu_runtime()
    try:
        import dpctl
    except Exception as exc:
        diag.add("dpctl_import", False, "dpctl (SYCL device control) is not available", _err(exc))
        return diag
    diag.add("dpctl_import", True, f"dpctl {getattr(dpctl, '__version__', '?')}")
    try:
        import dpnp
    except Exception as exc:
        diag.add("dpnp_import", False, "dpnp (GPU arrays) failed to load", _err(exc))
        return diag
    diag.add("dpnp_import", True, f"dpnp {getattr(dpnp, '__version__', '?')}")
    try:
        devices = [describe_device(d) for d in dpctl.get_devices()]
    except Exception as exc:
        diag.add("devices", False, "SYCL device enumeration failed", _err(exc))
        return diag
    gpus = [d for d in devices if d.get("device_type") == "gpu"]
    diag.add("devices", True, f"{len(devices)} SYCL device(s), {len(gpus)} GPU(s)", value=devices)
    dev, how = select_device(dpctl, target)
    if dev is None:
        diag.add("gpu_selected", False,
                 "no GPU device found (install/update the Intel graphics driver: it "
                 "provides the Level Zero / OpenCL GPU runtime)" if target == "gpu"
                 else f"no {target} device", how)  # fmt: skip
        return diag
    info = describe_device(dev)
    diag.add("gpu_selected", True, f"{info['name']} via {how} ({info['backend']})", value=info)
    try:
        q = dpctl.SyclQueue(dev)
    except Exception as exc:
        diag.add("queue", False, "could not create a SYCL queue on the device", _err(exc))
        return diag
    diag.add("queue", True, "explicit SYCL queue created")
    n = 1 << 20
    try:
        a = dpnp.arange(n, dtype=dpnp.int64, sycl_queue=q)
        placed = describe_device(a.sycl_device).get("filter_string")
        if placed != info["filter_string"]:
            raise RuntimeError(f"array landed on {placed}, not {info['filter_string']}")
    except Exception as exc:
        diag.add("allocation", False, "could not allocate on the selected device", _err(exc))
        return diag
    diag.add("allocation", True, f"{n * 8 >> 20} MiB int64 array on {placed}")
    try:
        got = int(dpnp.asnumpy(((a * 3 + 1) % 1000).sum()))
    except Exception as exc:
        diag.add("operation", False, "kernel launch failed", _err(exc))
        return diag
    diag.add("operation", True, "element-wise kernel + reduction executed")
    ref = int(((np.arange(n, dtype=np.int64) * 3 + 1) % 1000).sum())
    if not diag.add("verified", got == ref, f"device {got} vs NumPy {ref}",
                    None if got == ref else "wrong result"):  # fmt: skip
        return diag
    try:
        from pcbrouter.compute.sycl_relax import relax_selftest

        ok, detail = relax_selftest(q)
    except Exception as exc:
        diag.add("fused_kernel", False, "relaxation kernel failed", _err(exc))
        return diag
    if not diag.add("fused_kernel", ok, detail, None if ok else "mismatch with NumPy"):
        return diag
    if bench:
        try:
            diag.add("benchmark", True, **_benchmark(dpnp, q))
        except Exception as exc:
            diag.add("benchmark", False, "benchmark failed", _err(exc))
    return diag


def _benchmark(dpnp: Any, q: Any) -> dict[str, Any]:
    """Time the routing relaxation on a 1024x1024x2 grid: device vs NumPy."""
    from pcbrouter.compute.sycl_relax import benchmark_relax

    res = benchmark_relax(q)
    return {"detail": res["summary"], "value": res}


# ------------------------------------------------------------------ child process
def child_main(target: str = "gpu") -> int:
    """Entry for ``--gpu-probe-child``: print the diagnostic as one JSON line."""
    try:
        out = run_diagnostic(target).to_dict()
    except BaseException:  # last resort: still answer the parent
        out = {"target": target, "stages": [], "verdict": traceback.format_exc()[-400:]}
    sys.stdout.write(json.dumps(out, default=str) + "\n")
    sys.stdout.flush()
    return 0


def _child_command(target: str) -> list[str]:
    if getattr(sys, "frozen", False):  # the app executable handles the flag itself
        return [sys.executable, CHILD_FLAG, target]
    return [sys.executable, "-m", "pcbrouter", CHILD_FLAG, target]


def run_in_child(target: str = "gpu", timeout_s: float = CHILD_TIMEOUT_S) -> dict[str, Any]:
    """The diagnostic in a separate process: a native crash or hang in the GPU driver
    becomes a failed stage in the report instead of killing the caller."""
    from pcbrouter.utils.process import hidden_console_kwargs

    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            _child_command(target), capture_output=True, text=True, timeout=timeout_s,
            check=False, **hidden_console_kwargs(),
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        return {"target": target, "stages": [], "compute_verified": False,
                "verdict": f"the GPU check hung for {timeout_s:.0f} s and was stopped "
                "(driver problem?)"}  # fmt: skip
    except OSError as exc:
        return {"target": target, "stages": [], "compute_verified": False,
                "verdict": f"could not start the GPU check: {exc}"}  # fmt: skip
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    if proc.returncode != 0 or not lines:
        tail = (proc.stderr or proc.stdout).strip()[-400:]
        return {"target": target, "stages": [], "compute_verified": False,
                "verdict": f"the GPU check process crashed (exit code {proc.returncode}); "
                f"output: {tail}"}  # fmt: skip
    report: dict[str, Any] = json.loads(lines[-1])
    report["child_seconds"] = round(time.perf_counter() - t0, 2)
    return report
