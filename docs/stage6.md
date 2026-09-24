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
