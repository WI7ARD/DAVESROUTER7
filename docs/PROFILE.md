# Where routing time goes (measured)

`python tools/profile_route.py BOARD --mode accuracy` runs a whole-board route on
one worker under cProfile and groups self-time by pipeline stage. Measured
2026-09-30 in the development container (4-core Xeon, no GPU), Accuracy mode.

| Stage (share of profiled time) | router_basic (5 nets) | Router_Benchmark_RevA (2L, 32 nets) | modular_4layer (60 nets) | medium_4layer (QFP-64, 44 nets) |
|---|---|---|---|---|
| A* search loop (neighbour expansion, costs) | 43.6 % | 53.9 % | 34.7 % | 30.1 % |
| Python built-ins used by A* (heapq, dicts) | 25.4 % | 26.8 % | 17.5 % | 15.3 % |
| NumPy internals (mostly obstacle rasterisation and the heuristic field) | 13.9 % | 11.9 % | 34.1 % | 38.1 % |
| Obstacle rasterisation (Python side) | 7.4 % | 3.4 % | 6.4 % | 9.1 % |
| Coarse-to-fine helpers | 0.7 % | 2.0 % | 2.6 % | 2.2 % |
| Exact validation (segments, vias, routes) | 1.5 % | 0.8 % | 1.0 % | 1.5 % |
| Everything else | 7.5 % | 1.2 % | 3.7 % | 3.7 % |
| **Router phase timers:** grid build / search | 0.2 s / 0.6 s | 15.4 s / 108.9 s | 36.3 s / 54.2 s | 110.9 s / 123.4 s |

## What this says

- **2-layer boards:** about 80 % of the time is the pure-Python A* loop (heap
  operations and neighbour expansion). This is the part a data-parallel search
  (the relaxation kernel) replaces.
- **4-layer boards with fine-pitch parts:** building the per-net obstacle grid
  (rasterising every pad, track and pour within clearance of the net) is 40–50 %
  of the time (`grid` phase). It is NumPy work split into many small per-shape
  operations. That makes it a GPU candidate only if the shapes are batched, which
  is not done yet.
- **Validation, geometry and planning are negligible** (< 3 %). The exact
  validator is not a bottleneck, so it stays on the CPU for every route.

## Consequences for the GPU backend

- The GPU replaces the **search** inside one net (OrthoRoute's principle). The
  CPU still picks nets, validates and commits every route.
- `routing/search/relax.py` + `compute/sycl_relax.py` implement that as one fused
  integer kernel per sweep. It is bit-identical to the NumPy reference and exactly
  optimal for its cost model (see the tests).
- Grid building is the next candidate for 4-layer boards (batched rasterisation);
  not implemented in 1.1.1.
