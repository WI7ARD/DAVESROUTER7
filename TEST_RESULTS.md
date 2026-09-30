# Test results

Only results that were actually run are listed.

## v1.1.1 release candidate (executed 2026-09-30; container: 4 CPU cores, no GPU, offscreen Qt)

**Linux, full suite with coverage** at 82f4fde (`pytest -q -rs --cov=pcbrouter tests`, Python 3.12):
**863 passed, 9 skipped, 0 failed in 27 min 54 s; line+branch coverage 83 %.**

| Category | Result |
|---|---|
| Unit + integration (routing, rules, geometry, AI pipeline, commands, jobs, export, settings, CLI) | ran, passed |
| GUI tests (PySide6, `QT_QPA_PLATFORM=offscreen`) | ran, passed |
| New 1.1.1 regression tests (`test_partial_acceptance`, `test_ai_policy`, `test_accept_provenance`, `test_version_consistency`, `test_speed_refine`) | ran, passed (107 tests) |
| Security (AI output never becomes geometry, no shell/file access, free text cannot change the policy, unknown keys rejected) | ran, passed |
| SYCL kernel tests (`test_sycl_relax.py`, 7) | **skipped here**: dpctl is not installed in the main venv. They ran and passed earlier in a separate SYCL venv (below) |
| Real-GPU route (`test_gpu_gate.py`, 1) | **skipped**: no GPU device |
| Wine/NSIS installer build (`test_windows_installer.py`, 1) | **skipped**: needs makensis + Wine. Covered by Windows CI instead |
| Benchmarks (`tools/run_benchmarks.py`) | ran separately; see docs/BENCHMARKS.md |
| Timeout bound | `--budget 5` / `--budget 20` on medium_4layer stopped at 5.2 s / 20.2 s of routing |

`ruff check src tests tools`, `black --check src tests tools` and `mypy src` were clean.
CI (`ci.yml`) runs the suite on Linux and Windows with Python 3.12 and 3.13. The coverage
floor is 81 %.

**Real SYCL runtime** (dpctl 0.22 / dpnp 0.20 with Intel's OpenCL CPU runtime; a separate venv): the GPU tests run instead of skipping. They check that:
- the fused kernel is bit-identical to NumPy (1, 2 and 4 layers, random and real router grids);
- the staged diagnostic passes all 10 stages on `opencl:cpu` and stops at `gpu_selected` for `gpu`;
- the whole `router_dense` board routes in GPU mode with 0 fallbacks and 0 check errors.

**Windows CI, run 87 at 55908d8:**
- the test suite passed;
- the installer (355.6 MB) was built;
- the smoke test passed against the **installed** app: version, docs installed, CLI inspect, worker process, bundled dpnp, and the staged GPU diagnostic in a child of the frozen exe (verdict "stopped at gpu_selected: no GPU device", as expected on a GPU-less runner);
- product routing with the installed `pcbrouter.exe`: tiny 5/5; medium_2layer 34/34; modular_4layer 60/60 with 3 parallel helpers (60 results, 0 conflicts, 33 s); medium_4layer Speed 40/44;
- the impossible board fails with exit 1 and names the net; a malformed file gives a message and writes nothing; every source board is unchanged; outputs reopen and pass the internal check;
- the GUI starts with a corrupt settings file and the file is set aside; uninstall is refused while running and is clean afterwards.

**Benchmarks:** see docs/BENCHMARKS.md. **Profile:** see docs/PROFILE.md.

## Stabilization pass (executed 2026-09-29)

## Automated suite
`.venv/bin/python -m pytest -q` on the router commit (963a98a, before the merge of 1.1.0):
**729 passed, 2 skipped, 0 failed.** The skips need makensis+Wine and a CUDA/oneAPI GPU.

New tests include:
- `test_astar_heuristic.py`: per-group boxes, `merge_boxes` admissibility, coarse-to-fine incl. fallback and proven no-path;
- `test_router.py::test_direction_costs_and_group_boxes_keep_search_optimal`: against an independent Dijkstra;
- `test_board_router.py`: budget ≠ cancel; ROUTE_FAILED report; both presets route the dense fixture DRC-clean.

`ruff`, `black --check` and `mypy src` were clean at that commit.

## Router_Benchmark_RevA (Davi's board)
The board is 120×80 mm, 2 layers, 104 pads and 32 nets, routed with its own `.kicad_pro`/`.kicad_dru`. Everything was headless through `BoardRouter` with the GUI's preset construction, run on its own.

| Mode | Before (origin/main) | After |
|---|---|---|
| Speed | 21/32, 170 s | **32/32, ~25 s**, 171 vias, length 1.44× airwire, **0 DRC errors** |
| Accuracy | 15/32 at the 600 s budget | **32/32, ~58 s**, 136 vias, length 1.41× airwire, **0 DRC errors** |

## Real KiCad demo boards
Tracks and vias were stripped for routing work. The boards are local only, never committed. DRC counts the errors added by routing; the pre-existing zone-fill errors are not counted.

| Board | origin/main (default settings) | After: Speed | After: Accuracy |
|---|---|---|---|
| pic_programmer (KiCad 10, 2 layers, 33 nets) | 33/33, 167.6 s, 531 MB | 33/33, 12.2 s, 0 new | 33/33, 24.3 s, 0 new |
| complex_hierarchy (2 layers, 49 nets) | 49/49, 65.1 s | 49/49, 12.9 s, 0 new | 49/49, 20.2 s, 0 new |
| kit-dev-coldfire (4 layers, 209 nets) | 18/209 in 600 s ("CANCELLED") | **16/209** in 600 s budget (PARTIALLY_ROUTED), 0 new | **16/209** in 600 s, 0 new |
| video (4 layers, 371 nets) | 17/371 in 600 s ("CANCELLED") | not re-run with the presets | not re-run |

## Real application window (`tools/e2e_gui_check.py`, Xvfb, CPU)
The flow drives the real `MainWindow` actions: open → Route Board → Cancel → Route Board → accept → Internal Geometry Check → export (new file) → reopen → close.

| Board / mode | Load | Cancel | Route | DRC after accept | Export → reopen | Source unchanged | Max GUI gap while routing |
|---|---|---|---|---|---|---|---|
| Benchmark / Accuracy | 54 ms, 32 nets listed | 239 ms, board unchanged | 32/32, 56.6 s, 11 distinct progress texts, spinner running | 0 errors | 264/264 tracks, 136/136 vias | yes | 219 ms (at job start), otherwise ≤ 172 ms |
| Benchmark / Speed | — | 238 ms | 32/32, 24.2 s | 0 errors | identical | yes | no stall > 100 ms for the first 19 s |
| pic_programmer / Accuracy | 381 ms, 111 nets | 3.3 s (during board preparation; before the added cancel points) | 33/33, 24.2 s | 4 errors, all pre-existing zone fills; export labelled UNVERIFIED | 185/185, 30/30 | yes | 170 ms |
| pic_programmer / Speed | — | 2.2 s (preparation) | 33/33 | same | identical | yes | 140 ms; DRC-draw stall 1779 → 287 ms after the overlay fix |
| router_basic / Accuracy | 26 ms | not exercised (finished in < 3 s) | 5/5, 0.8 s | 0 errors | 12/12, 2/2 | yes | 89 ms |

Earlier run of router_basic with the cancel pressed after 0.3 s: cancel 263 ms, board unchanged.

## Not tested here
- Real Intel Iris Xe / dpnp execution.
- `kicad-cli` DRC of the exports.
- KiCad 10 itself opening the exported file.
