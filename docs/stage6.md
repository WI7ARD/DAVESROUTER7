# Stage 6 — optional GPU acceleration

Version 0.6.0-stage6. The CPU A* router is the authority and the reference; the GPU
is an optional accelerator for the search only.

```
board model ─▶ occupancy / cost arrays (NumPy, CPU) ─▶ [device copy] ─▶ wavefront (xp)
    ─▶ distance field ─▶ path (host) ─▶ same simplifier ─▶ same exact CPU validator
```

## Algorithm choice
A* is a sequential priority-queue search and maps poorly to GPUs. The GPU path uses
an **array wavefront** (parallel Bellman-Ford/Lee relaxation over the whole grid,
8 directions + via transitions), written once against an array module `xp`:
NumPy (CPU), **CuPy** (NVIDIA CUDA) or **dpnp** (Intel oneAPI: Iris Xe, Arc). The
stop rule is exact (positive costs: stop when no changed cell can beat the best
target). Documented differences from A*: no bend penalties in the state and no via
*limits* — requests with `max_vias` always use the CPU A*.

## Backends and selection
* `compute/gpu_backend.py`: `available` only when the array library imports **and**
  a self-test kernel returns the right result on the device. Reports device name,
  memory (CUDA), status. Intel integrated GPUs share system RAM: a fixed 1 GiB cap
  is used because dpnp has no portable free-memory query.
* VRAM guard: ~64 bytes per cell per layer must fit in 70 % of free device memory,
  otherwise that search runs on the CPU (no tiling yet).
* `routing/backend.py`: CPU / GPU / AUTO (Settings ▸ Compute). AUTO uses the GPU
  only from 2 M cells. Any GPU exception → logged `gpu.fallback`, the same search
  reruns on the CPU, the job continues. The result's `metrics.backend` says what ran.
* Detection: NVIDIA via `nvidia-smi`; Intel/AMD via `/sys/class/drm` (Linux) or
  `Win32_VideoController` (Windows). Status for an Intel GPU without dpnp:
  "Intel GPU detected; install the optional dpnp package".
* Install extras: `pip install ai-pcb-router[gpu-cuda]` (CuPy for CUDA 12) or
  `[gpu-intel]` (dpnp; also needs Intel's GPU compute runtime). Neither is needed
  to run the application.

## Measured here (container, no GPU): `python tools/bench_router.py`
| net | CPU A* | CPU NumPy wavefront |
|---|---|---|
| USB_N | 0.02 s | 0.16 s |
| VBUS | 0.01 s | 0.46 s |
| S3 | 0.43 s | 0.84 s |

The wavefront on the CPU is slower than A* (it relaxes every cell each
iteration); it exists so the exact GPU code path is tested on every machine. **GPU
timings: NOT RUN** — no CUDA or oneAPI device was available. On an integrated GPU
such as Iris Xe, expect modest gains at best for boards of this size; the benefit
grows with grid size. Occupancy and cost-field generation stay on the CPU (NumPy),
which is already ~12 ms for a 40 × 30 mm board at 0.1 mm.

## Hardware gate (mandatory for every GPU-dependent job)
Nothing GPU-related is scheduled until a **hardware probe** says a device exists
(`compute/probe.py`):

1. CUDA: `nvidia-smi -L` lists devices, and CuPy's `getDeviceCount()` confirms them
   when CuPy is installed;
2. Intel oneAPI: an Intel display adapter is present, and dpctl can create a SYCL
   GPU device when dpctl is installed;
3. the matching array library (CuPy / dpnp) must be importable.

No device → the job is **SKIPPED** with a reason (never FAILED) and the work runs on
the CPU. Where the gate is applied today:

| job | behaviour without a device |
|---|---|
| `GPUBackend.available` / `initialize()` | False / `BackendUnavailableError("GPU SKIPPED: …")`; the array library is never imported |
| routing acceleration (`routing/backend.router_for`) | CPU router named `cpu (GPU SKIPPED: …)` |
| `tools/gpu_selftest.py` | writes `{"status": "SKIPPED", …}`, exit 0 |
| `tools/bench_router.py` | prints `GPU benchmark: SKIPPED (reason)` |
| pytest `@pytest.mark.gpu` | auto-skipped with reason `GPU SKIPPED: …` |
| CI `.github/workflows/gpu.yml` | probe step (`tools/gpu_probe.py`) sets `available`; GPU steps have `if: available == 'true'`, otherwise a `GPU SKIPPED` notice and the job succeeds |

**Rule for any future GPU stage:** call `gpu_gate(job)` (returns the probe) or
`require_gpu(job)` (raises `GpuJobSkipped`) *before* scheduling GPU work; mark
tests `@pytest.mark.gpu`; in CI, condition the steps on the probe output.
`tests/unit/test_gpu_gate.py` enforces part of this statically: GPU libraries may
be imported only in `gpu_backend.py`/`probe.py`, and every tool that touches the GPU
backend must call `gpu_gate(`. The probe is cached; `reset_probe()` re-runs it after
installing drivers or libraries.
