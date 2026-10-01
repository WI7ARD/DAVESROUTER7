# Benchmark results (v1.1.1)

Measured 2026-09-30 in the development container: 4-core Xeon @ 2.8 GHz, no GPU,
Linux. Every run goes through the real command line (`pcbrouter --route`), so it
includes loading, the board's own rules, the preset, the exact validator on every
commit, export, and a reload of the written file.
Reproduce: `python tools/run_benchmarks.py --out bench_out` (boards come from
`tests/fixtures/benchmark_suite.py`; `--boards esc_4layer` adds Davi's ESC).

## Real100: 100 real KiCad boards

`benchmarks/real100/` pins 100 boards from the KiCad source mirror: 80 PCBNew QA
boards and 20 official demos. The board files are fetched on demand and verified
against their Git blob SHA-1; they are not vendored. Each board/mode routes in a
fresh subprocess with a hard timeout. See `benchmarks/real100/README.md`.

```
python tools/benchmark_real100.py all --profile smoke       # 12 boards, Speed, 45 s each
python tools/benchmark_real100.py all --profile standard    # 98 boards, both modes, 180 s
```

### First baseline (standard profile, 180 s hard timeout per board/mode)

Measured 2026-09-30 in the development container (4-core Xeon, no GPU).
Router at `9d846e5` (no router changes up to `a30da80`), harness 1.0. Speed and
Accuracy ran as two processes in parallel, one core each.

| | Speed | Accuracy |
|---|---|---|
| Boards run | 98 | 98 |
| Nothing to route (QA parser fixtures without multi-pad nets) | 47 | 47 |
| Fully routed | 25 | 26 |
| Partially routed | 8 | 9 |
| Failed: no project file (RULE_UNKNOWN, correct in conservative mode) | 9 | 9 |
| Failed within seconds with a project file (K055, K091, K092, K095; per-net reasons recorded from harness 1.1 on) | 4 | 4 |
| Failed after searching (K059) | 1 | 1 |
| Hard timeout | 4 | 2 |

Across both modes, 1,390 nets were routed; every committed route passed the
exact validator.

What the failures are:
- **K059:** the project states no copper-to-edge clearance. Conservative
  validation then refuses every track, but the router only found that out
  after 176 s of searching (fix pending: refuse up front).
- **K037:** unsupported custom DRU rules (`insideCourtyard()`) attach a
  stricter possible clearance that the validator enforces but the search grid
  ignored, so 2 nets failed VALIDATION (fix pending).
- **Hard timeouts:** harness 1.0 gave the router 175 s regardless of load
  time, and the largest boards load for about 20 s. Harness 1.1 subtracts load
  time from the route budget.
- **The large demos (K067, K088, K098, K081):** 20–60 % of nets in 180 s.
  Budget-limited, like the other dense 4+ layer boards.

### Tuning round 1 (harness 1.1, the 41 boards that route, both modes)

Baseline-B is `0d12c26` (v1.1.1); the candidate is `928cc79`. Same machine, and
Speed and Accuracy ran in parallel for both.

| | v1.1.1 | + round 1 |
|---|---|---|
| Nets routed | 1,539 | **1,755 (+216, +14 %)** |
| VALIDATION failures | 138 | 5 |
| INVALID_REQUEST | 234 | 0 |
| K022 (Speed / Accuracy) | 13 / 12 of 64 | **60 / 62 of 64** |
| K092 | 0 / 0 of 117 | **55 / 56 of 117** |
| K037 Accuracy | 11/14 | 13/14 |

What changed:
1. The search grid now demands the clearance the validator enforces, including
   the "possibly stricter" bound of unsupported custom rules.
2. An unknown copper-to-edge clearance fails up front with RULE_UNKNOWN.
3. A net-class via below the board's minimum means routing without vias,
   instead of refusing the net.
4. Unsupported DRU conditions are evaluated three-valued, so a rule that cannot
   apply to a pair (e.g. `A.NetClass == 'HV' && A.insideArea(...)` for a non-HV
   net) no longer constrains it.

Three budget-limited Accuracy boards showed −1 to −2 nets. Re-running the old and
new router on them under identical conditions gave 125 vs 127 nets, and the old
router alone varies by up to 8 nets on K088 between runs. So these differences
are timing noise at the 160 s budget, not regressions. The guard boards
(small_2layer, medium_4layer, dense_2layer) are unchanged in nets and vias.

Tuning against this corpus continues. The workflow is in
`.claude/skills/real100-tune/SKILL.md`.

### Learned search policy v3 (paired A/B, commit de74b6f)

Same machine (4 logical CPUs), Real100 standard profile, both modes. Baseline and
candidate ran side by side (same load), repeated. Tool:
`tools/real100_compare.py --baseline fixed --candidate P --repeat N`.

**Training boards** (31, in-sample; the learned policy **without** the selector,
2 repeats) — the board-level evidence the selector uses:

| Board / mode | Fixed | Learned | Mean Δ |
|---|---|---|---|
| K098 Accuracy | 27, 27 | 48, 49 | +21.5 |
| K086 Speed | 82, 82 | 92, 92 | +10.0 |
| K099 Accuracy | 37, 40 | 44, 44 | +5.5 |
| K035 Accuracy | 62, 62 | 64, 64 | +2.0 |
| K022 Accuracy | 62, 62 | 63, 63 | +1.0 |
| K092 Accuracy | 56, 56 | 34, 33 | **−22.5** |
| K094 Accuracy | 34, 34 | 32, 32 | −2.0 |
| K097 Accuracy | 41, 41 | 40, 40 | −1.0 |

Total 1,493.5 → 1,506.5 nets; 53 of 62 board/mode pairs unchanged; 0 new-copper
DRC errors either way. The same choice (a greedier search for 3–4-pad nets on
dense boards) gains 21.5 nets on K098 and loses 22.5 on K092: without the
selector this policy is **REJECTED** by the gate.

**Held-out boards** (10, never trained on; near-duplicates grouped) **and the
guard boards**, the selective policy, 3 repeats:

- the selector chose `FALLBACK_FIXED` on all 60 held-out runs and on every guard
  board: 5 boards out of distribution (K025, K036, K050, medium_4layer by
  distance; medium_4layer most unlike on long-net share 0.91), K067/K088
  Accuracy because a similar training board (K092) lost nets, the rest because
  similar boards showed no gain;
- held-out nets 441.0 → 443.7 (timing noise between two identical strategies),
  0 regressed boards; guard boards 176 → 176 (medium_4layer 40/44 Speed,
  44/44 Accuracy, as the fixed router);
- **identical-strategy noise** on the budget-limited boards: K067 Accuracy
  81 / 89 / 93 nets across three runs of the same fixed router.

Gate: **EXPERIMENTAL** — no harm (no regression, validity and failures
unchanged, reproducible), no measured benefit on held-out boards. The fixed
router stays the default; the policy ships only as an opt-in.

### Completion endgame (paired A/B, 3 repeats, worktrees 6319fb9 vs 4db1d5b)

| Measure | Fixed router | + endgame |
|---|---|---|
| Fully routed board/modes (82) | 53 | **54** (K096 Speed 0/3 -> 3/3) |
| Guard boards, mean nets | 176 | 177 (medium_4layer Speed 40 -> 41) |
| New-copper DRC errors | 0 | 0 |

The automatic gate said REJECTED (rule 2): K092 Speed -1 (3/3 runs), K094,
K098 Accuracy, K099 Accuracy within noise. Per-run endgame statistics show the
endgame **never triggered** on any of those boards (82-334 nets missing, far
above its max(4, 15 %) trigger); where it did trigger it finished nets (K022
Speed +1, K096 Speed fully routed). A board where the endgame never ran is not
evidence against it, so it is kept. (K092 Speed takes the same code path with
or without it; the consistent -1 is the time budget meeting side-by-side
process scheduling.)

## Procedure (how to get comparable numbers)

1. Use a quiet machine: nothing else CPU-heavy, on AC power, the same power plan.
   Budget-limited boards (the 4-layer and real boards) route *fewer nets* when
   the CPU is shared, not just slower.
2. Run `python tools/run_benchmarks.py --out bench_out --trials 3 [--boards …]`.
   The table reports the **median** route time and keeps the min–max range in
   `results.json`. Completion, vias and length come from the median run;
   single-worker runs are deterministic, so they repeat exactly.
3. `bench_out/environment.json` records:
   - the SHA-256 of every board file;
   - the pcbrouter version and git commit;
   - the Python and NumPy versions;
   - the OS and the CPU model / logical core count.

   Compare two runs only when the board hashes match. Treat a time difference
   under about 10 % as noise.
4. Parallel runs (`--workers -1` or ≥ 2) are not bit-for-bit repeatable (see
   KNOWN_LIMITATIONS.md). Compare their completion and validity, and use time
   ranges.
5. Speed-ups are per board. A number like "17× faster" holds for the board and
   mode it was measured on, never in general.

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
| kit-dev-coldfire | speed | 1 | **152/209** | 308 | 613 | 0 |
| kit-dev-coldfire | accuracy | 1 | 169/209 | – | 600 | 0 |

Speed's fine-grid retry is restricted to small failures in 1.1.1. The A/B below is
coldfire Speed, 1 worker, 600 s, same code otherwise (`--trials 1`):

| Fine-grid retry | Nets | Vias |
|---|---|---|
| unconditional | 121/209 | – |
| off | 134/209 | 258 |
| only after a failure of ≤ 20 000 expanded nodes (released) | **152/209** | 308 |

The suite boards were re-run after the change (1 worker, 300 s budget). Nets,
vias and lengths matched the table above: small_2layer 11/11 both modes,
medium_4layer 40/44 (Speed) and 44/44 (Accuracy), dense_2layer 35/35 both modes.
| hhkittesc ESC (4L, 94 nets, 900 s budget) | accuracy | auto (4) | 93/94 | 278 | 548 | 0 (31 pre-existing) |
| hhkittesc ESC + **Power class 3.0/0.6 mm** (1200 s) | accuracy | auto (4) | 79/94 | 234 | 959 | 0 (31 pre-existing) |

The Power-class ESC run fails mainly at U3's 0.65 mm-pitch pins. A 3 mm net-class
width cannot enter them, and the router has no neck-down. See
`benchmarks/boards/hhkittesc/results/README.md`.

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
