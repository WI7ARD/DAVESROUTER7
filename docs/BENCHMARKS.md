# Benchmark results (v1.1.1)

Measured 2026-09-30 in the development container: 4-core Xeon @ 2.8 GHz, no GPU,
Linux. Every run goes through the real command line (`pcbrouter --route`), so it
includes loading, the board's own rules, the preset, the exact validator on every
commit, export, and a reload of the written file.
Reproduce: `python tools/run_benchmarks.py --out bench_out` (boards come from
`tests/fixtures/benchmark_suite.py`; `--boards esc_4layer` adds Davi's ESC).

*Workers* is the requested helper count. With `-1` (Auto) helpers only start when
the nets are spread out enough to route side by side (`estimate_speedup` ≥ 1.5).
Crowded boards therefore run on one worker, which is why their Auto rows equal the
single-worker rows. "Verified" means the exported file passed the internal
geometry check with 0 errors.

## Suite (budget 300 s per run)

| Board | Mode | Workers | Nets | Vias | Length mm | Route s | CPU s | Peak MB main / helper | Verified |
|---|---|---|---|---|---|---|---|---|---|
| tiny (2L) | speed | 1 / auto | 5/5 | 2 | 47.6 | 0.2 | 1.0 | 58 | yes |
| tiny (2L) | accuracy | 1 / auto | 5/5 | 2 | 48.0 | 0.6 | 1.3 | 72 | yes |
| small_2layer | speed | 1 / auto | 11/11 | 12 | 390.2 | 0.6 | 1.4 | 61 | yes |
| small_2layer | accuracy | 1 / auto | 11/11 | 12 | 390.4 | 3.1 | 3.9 | 94 | yes |
| medium_2layer | speed | 1 / auto | 34/34 | 157 | 3297.9 | 49.6 | 50.1 | 129 | yes |
| medium_2layer | accuracy | 1 / auto | 34/34 | 98 | 2895.2 | 40.2 | 41.8 | 196 | yes |
| dense_2layer | speed | 1 / auto | 35/35 | 148 | 2306.5 | 11.3 | 12.1 | 85 | yes |
| dense_2layer | accuracy | 1 / auto | 35/35 | 94 | 2086.3 | 22.7 | 24.1 | 131 | yes |
| modular_2layer | speed | 1 | 60/60 | 220 | 3357.4 | 35.3 | 36.1 | 192 | yes |
| modular_2layer | speed | auto (3) | 60/60 | 220 | 3357.4 | **15.8** | 35.7 | 66 / 166 | yes |
| modular_2layer | accuracy | 1 | 60/60 | 134 | 2913.7 | 31.9 | 33.3 | 123 | yes |
| modular_2layer | accuracy | auto (3) | 60/60 | 134 | 2913.7 | **11.6** | 34.4 | 65 / 102 | yes |
| small_4layer | speed | 1 / auto | 2/2 | 2 | 37.2 | 0.2 | 0.9 | 58 | yes |
| small_4layer | accuracy | 1 / auto | 2/2 | 0 | 37.0 | 0.3 | 1.1 | 67 | yes |
| medium_4layer (QFP-64, 0.5 mm) | speed | 1 / auto | 40/44 | 118 | 2682.3 | 131.2 | 139.3 | 585 | yes |
| medium_4layer (QFP-64, 0.5 mm) | accuracy | 1 / auto | **44/44** | 105 | 2833.1 | 213.3 | 245.7 | 460 | yes |
| modular_4layer | speed | 1 | 60/60 | 173 | 3425.1 | 28.2 | 30.5 | 172 | yes |
| modular_4layer | speed | auto (3) | 60/60 | 173 | 3425.1 | **10.0** | 25.3 | 64 / 153 | yes |
| modular_4layer | accuracy | 1 | 60/60 | 110 | 3174.5 | 77.2 | 86.4 | 188 | yes |
| modular_4layer | accuracy | auto (3) | 60/60 | 111 | 3161.0 | **21.6** | 63.1 | 64 / 163 | yes |
| impossible | speed / accuracy | 1 / auto | 0/1 (exit 1, clear reason) | – | – | 3.7–11.1 | – | 105 | – |

## Parallel scaling (same board, same mode)

| Board / mode | 1 worker | 2 | 3 | 4 |
|---|---|---|---|---|
| modular_4layer accuracy | 76.8 s | 35.7 s | 22.4 s | **17.0 s** |
| modular_4layer speed | 26.4 s | 14.6 s | 10.5 s | **9.2 s** |
| modular_2layer speed | 38.6 s | 21.7 s | 17.4 s | **14.4 s** |
| modular_2layer accuracy | 34.2 s | 17.7 s | 12.2 s | **10.8 s** |

All runs completed every net. 0 commit conflicts reached the board: results that
became illegal are re-routed. Vias and length matched the single-worker run or
differed by at most 3 vias. 4 helpers were fastest on 4 cores, so from this
release *Auto* uses one helper per core (at most 4).

Crowded boards (all nets through one QFP, the benchmark board) measured slower and
*less* complete in parallel before the concurrency gate (medium_4layer Accuracy:
42/44 in 305 s vs 44/44 in 216 s), so Auto runs them on one worker.

## Real boards

| Board | Mode | Workers | Nets | Vias | Route s | New check errors |
|---|---|---|---|---|---|---|
| Router_Benchmark_RevA (2L, 32 nets) | accuracy | 1 | 32/32 | 135 | 83 | 0 |
| kit-dev-coldfire (4L, 209 nets, 600 s) | speed | auto (3) | 150/209 | 300 | 600 | 0 |
| kit-dev-coldfire | speed | 1 | 135/209 | – | 600 | 0 |
| kit-dev-coldfire | accuracy | 1 | 169/209 | – | 600 | 0 |
| hhkittesc ESC (4L, 94 nets, 900 s) | accuracy | auto (3) | 92/94 | 274 | 922 | 0 (31 pre-existing) |

## GPU kernel (SYCL), measured on the OpenCL **CPU** device

No GPU was available. The fused relaxation kernel ran through the real SYCL
runtime on the CPU's OpenCL device (`tools/bench_gpu.py --device opencl:cpu`):

| Board | CPU A* | Device kernel | Result |
|---|---|---|---|
| router_basic | 0.6 s | 0.4 s | 5/5 both, same vias |
| router_dense | 2.9 s | 5.5 s | 11/11 both, 12 vias both |
| router_4layer | 0.3 s | 0.6 s | 2/2 both |
| Router_Benchmark_RevA | 83 s | 288 s | 32/32 both (133 vs 135 vias, 3985 vs 4048 mm), 0 errors |

Per sweep, the kernel is 10× faster than the NumPy version of the same
algorithm, but on a CPU "device" it does not beat the A*. Iris Xe numbers must
come from a real GPU. Run `python tools/bench_gpu.py <boards> --report
gpu_report.json` there; until then *Auto* keeps Intel GPUs on the CPU router.
