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

## Using the GPU in the app (e.g. Intel Iris Xe on a laptop)
1. Install the latest Intel graphics driver (it provides the Level Zero/OpenCL
   GPU compute runtime dpnp needs).
2. Run the app from a Python install, not the Setup.exe build (see the limitation
   below):
   ```
   git clone -b claude/vigilant-cray-pghyx8 https://github.com/WI7ARD/DAVESROUTER7
   cd DAVESROUTER7
   py -3.12 -m venv .venv
   .venv\Scripts\activate
   pip install -e ".[gpu-intel]"      # NVIDIA instead: pip install -e ".[gpu-cuda]"
   python tools\gpu_probe.py          # should print "status": "AVAILABLE"
   pcbrouter
   ```
3. Settings ▸ Compute ▸ Default backend: **GPU** (every search on the GPU) or
   **Auto** (GPU only for grids ≥ 2 M cells — on typical small boards Auto stays on
   the CPU). The status bar shows `GPU: ready (dpnp)` once the device initialised
   and `Routing: Ready (GPU)`.
4. Open a board and use **Tools ▸ Test GPU on This Board…**: it routes up to 8
   incomplete nets with the CPU A* and with the GPU wavefront, validates every
   route with the exact engine and shows timings (nothing is added to the board).
5. Route as usual (Route Selected Net, Route Board, AI "Run with Router"): the
   Route Review panel's backend line shows `hybrid-gpu (cpu N, gpu M, …)`, i.e.
   how many searches actually ran on the GPU.

Honest expectations: every GPU route still goes through the same simplifier and
exact CPU validator, so the GPU can never make an illegal route legal. The
wavefront has no bend penalties, so GPU routes may have more bends than CPU A*
routes. On an integrated GPU with small boards the GPU is often *not* faster
(transfer/launch overhead); the in-app test tells you which is faster on your
board.

**Limitation:** the Windows installer (Setup.exe) does not bundle dpnp/CuPy (the
GPU runtimes are large and hardware-specific), so the installed app always shows
GPU SKIPPED/unavailable. GPU routing needs the Python install above.
