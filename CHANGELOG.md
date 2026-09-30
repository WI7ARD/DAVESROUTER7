# Changelog

## Unreleased

**Learning, level 1: routing experience log** (`docs/LEARNING.md`)
- After each board-routing job (app, CLI, Real100) one record per net is added
  to a local log: board/net features, search settings, outcome and effort.
- The log is anonymised: net names and coordinates are never stored, and the
  board is identified by a salted hash. It is capped at 50 MB and never leaves
  the computer.
- Turn it off in Settings ▸ Routing, or with `--no-experience` for one CLI run.
  It is the data the next level learns search settings from.

**Learning, level 2: search-variant policies and a trainer** (`docs/LEARNING.md`)
- Per-net search variants ("arms": preset, no coarse-to-fine, finer grid,
  greedier, fewer vias) chosen by a policy: `fixed` (default), `random:SEED`
  for exploration, or a trained `policy.json`. The validator still checks
  every route. The log records which arm each attempt used.
- `tools/train_policy.py` (`python -m pcbrouter.learning.trainer`) trains a
  frozen policy from exploration records. It splits by board, reports a
  replay estimate on held-out boards against the preset with a 95 % interval,
  and prints the cost per arm.
- `pcbrouter --route --policy policy.json`, `tools/run_benchmarks.py --policy`
  and Real100 `--policy` use a trained policy. The desktop app does not load
  one yet.

**Routing (Real100 tuning round 1: 1,539 → 1,755 nets on the 41 routable boards)**
- The search grid demands exactly the clearance the validator enforces. This
  includes the "possibly stricter" bound of unsupported custom rules, whose
  absence caused repeated VALIDATION failures.
- A board without a copper-to-edge clearance is refused up front (RULE_UNKNOWN)
  instead of after minutes of futile search.
- A net-class via below the board's minimum hole size means routing without
  vias, instead of refusing every net. An explicitly requested undersized via
  is still refused.
- Unsupported DRU conditions (`insideArea`, `insideCourtyard`, `A.Name`, …) are
  evaluated three-valued. A rule that cannot apply to a pair no longer
  constrains it; unknown parts are never assumed false.

## v1.1.1 — 4-layer routing, parallel routing, installable release

**Fixed**
- **Partial acceptance could leave a net disconnected.** A board job that ripped up net X to route Y records the removal against X. Accepting only Y then applied the removal of X's old route without X's new route. Now:
  - each removed object belongs to its owner net;
  - each net's result lists the nets whose copper was moved for it (`dependencies`, found from copper overlap);
  - accepting a net also accepts that closure (the table says which nets come along);
  - the accept command re-checks connectivity on the working board and is atomic: it refuses, and undoes itself, if any net that was connected would open.
- **A rip-up could leave the displaced net unrouted.** A rip-up that would leave any displaced net disconnected is now rolled back, including nets accepted in earlier jobs.
- **AI board routing ignored the approved policy.** It ran with default settings (rip-up on) while the approval said "no rip-up". See *AI constraint handling*.
- **`max_ripups_per_net` was per pass.** It now counts the whole job.
- **Speed's fine-grid retry cost completion on congested boards.** It now follows only small failures (boxed-in fine-pitch pads).
- **Intel GPU: the GPU check crashed with `SyclProgramCompilationError`** (reported on an Iris Xe). The GPU is selected through Level Zero, but dpctl builds OpenCL C only for OpenCL queues. Now:
  - the routing kernel is built and run on the same GPU's OpenCL device;
  - if no backend can build it, the GPU check reports why instead of crashing;
  - routing stays on the CPU and does not retry the build for every search.
- **A development-stage banner ("Stage 10 · …") was shown in the toolbar.** It is removed, and the About dialog uses the release wording.
- **Version drift.** The router recorded `1.0.0` in route metadata; it now records the release version, and a test ties the package, `pyproject.toml`, changelog, README and installer together.

**Safety / correctness**
- **Accepted copper is user-approved.** Accepting a board batch commits `USER_ACCEPTED` provenance (it was `ROUTER_GENERATED`). Later jobs rip up accepted or applied-optimisation copper only with `ripup_user_accepted`, which AI plans can never set.
- **`preserve_existing`** (router setting): rip-up may only move copper created by the current job. It is the default for AI plans.
- Copper loaded from the file and locked copper are never ripped up, as before.

**AI constraint handling**
- **One policy from approval to execution.** An AI plan carries a `BoardPolicy`: rip-up, preserve-existing, mode, priorities, differential pairs, and board-wide layer/via limits. Execution builds its `BoardRouterSettings` only from it. The approval panel's **What will run**, the running message and the `ROUTER_RESULT` facts are all produced from that same settings object.
- **Capability matrix** (`docs/CAPABILITIES.md`, `ai/capabilities.py`). Every constraint field is classified per operation:
  - ENFORCED, SOFT, ANALYSIS ONLY or UNSUPPORTED;
  - the approval panel labels each constraint;
  - unsupported constraints make the command INVALID instead of being "recorded only": `avoid_nets`, `avoid_net_classes`, `keep_near` / `keep_away_from`, impedance, shielding, component movement, blind/micro vias, clearance on routing commands, widths on `route_board`, and rip-up on single-net routing.
- **Differential pairs** are a soft preference: routed together, with a corridor. Gap, skew and impedance are not controlled; gap and skew are measured and reported.
- **Length targets** are measured and reported after routing. There is no length tuning.
- `set_routing_priority` now orders later AI board jobs. The Speed/Accuracy preset applies to AI requests too.

**Routing**
- **4-layer boards:**
  - Via cells are no longer marked free by own-net copper on other layers. This was the main 4-layer blocker.
  - Pass 1 gives every net a fair time slice, so all nets get a try before the budget runs out.
  - Result on kit-dev-coldfire (4 layers, 209 nets, 600 s): 16/209 → 150/209 (Speed, parallel) and 169/209 (Accuracy).
- **Faster searches:**
  - coarse-to-fine search in a corridor, with a proven no-path check;
  - per-connection heuristic target boxes;
  - preferred layer directions, with an admissible heuristic.
- **Router_Benchmark_RevA** (2 layers, 32 nets): Speed 21/32 in 170 s → 32/32 in ~30 s; Accuracy 15/32 → 32/32 in ~60–90 s. 0 DRC errors.
- **Parallel routing** (Settings ▸ Routing ▸ Parallel routing: Auto / Single worker / 2–4):
  - Helper processes route nets whose regions don't overlap.
  - The main process validates and commits each result with the exact validator; a conflict is re-routed.
  - Cancel reaches every helper.
  - If the helpers can't start, routing continues on one worker.
- **Clear failure reasons:**
  - a time-budget stop is reported as a time-budget stop, not as a cancel;
  - each failed net says why and where (coordinates, nodes, time);
  - nets that were never tried say so.

**GPU (Intel, optional; CPU stays the default)**
- **`--gpu-check` is a staged diagnostic**, run in a crash-isolated child process (this also works in the installed app):
  - stages: library → SYCL devices (backend, driver, memory) → explicitly selected GPU (Level Zero, then OpenCL) → queue → allocation (verified to land on the GPU) → kernel → result verified against NumPy → routing kernel → benchmark;
  - exit 0 only when computation on the GPU was verified.
- **The dpnp backend is pinned to the selected GPU.** dpnp's default device can be a CPU.
- **New integer relaxation search** (`routing/search/relax.py`) with a fused SYCL kernel (`compute/sycl_relax.py`):
  - one kernel launch per sweep, with arrays kept on the device;
  - distance fields bit-identical to the NumPy reference;
  - exactly optimal against an independent Dijkstra;
  - its routes pass the exact validator.

**Removed**
- The Freerouting integration: menu entries, setup dialog, `--freeroute` / `--setup-freerouting`, and the KiCad Specctra bridge. Old settings files that still contain Freerouting fields load normally.

**Product / packaging**
- **Headless routing:** `pcbrouter board.kicad_pcb --route [--mode speed|accuracy] [--workers N] [--timeout S] [--backend cpu|auto|gpu] [--layers F.Cu,B.Cu] [--grid MM] [--output F] [--report JSON] [--kicad-drc] [--overwrite]`.
  - Exit codes: 0 fully routed, 3 partial (file written), 1 error, 2 usage.
  - The source board is never modified unless `--overwrite` is given; then a timestamped backup is written first.
  - Explicit `--backend gpu` fails clearly when no GPU is usable.
- **First-run welcome page** (Help ▸ Welcome) and **Settings ▸ Restore Defaults**.
- **New docs:** quick start, user guide and troubleshooting (installed with the app), plus known limitations and benchmark results.
- **Benchmark suite** (`tests/fixtures/benchmark_suite.py`, `tools/run_benchmarks.py`): tiny, small/medium/dense 2-layer, small/medium 4-layer, and an impossible board.
- **Windows CI** installs the built setup and routes suite boards with the *installed* `pcbrouter.exe`:
  - 2-layer single worker;
  - 2- and 4-layer parallel;
  - the impossible board must fail cleanly;
  - a malformed file must give a message;
  - every output must reopen, and every source must stay unchanged;
  - the app must start with a corrupt settings file.
- **Tagged releases (`v*`)** publish the installer `.exe`, its `.sha256` and `SHA256SUMS.txt` on GitHub Releases. The installer is still not code-signed, so browsers may warn. Verify the checksum.
- **GUI responsiveness:**
  - the AI context no longer rasterizes congestion on the GUI thread;
  - the DRC overlay draws 6× faster;
  - the live preview uses a solid pen.

**Testing**
- New regression suites:
  - `test_partial_acceptance.py`: dependency closure, tampered results refused atomically, rip-up rollback, every selection keeps nets connected;
  - `test_ai_policy.py`: approval text == executed settings for every rip-up/preserve/mode combination, preserve-existing enforced end to end, and every constraint field classified and carried or rejected;
  - `test_accept_provenance.py`;
  - `test_version_consistency.py`.
- **Real100** (`benchmarks/real100/`, `tools/benchmark_real100.py`):
  - 100 real KiCad boards (80 QA + 20 demos), pinned to a kicad-source-mirror commit and verified by Git blob hash;
  - an unrouted derivative of each is built locally; the stripping is verified idempotent, with per-board `SOURCE.json`;
  - every board/mode is routed in an isolated process with a hard timeout;
  - `tools/real100_compare.py` reports regressions between runs;
  - the `real100-tune` skill documents the benchmark-and-tune workflow;
  - first baseline in docs/BENCHMARKS.md.
- CI (`ci.yml`):
  - lint and types;
  - tests on Linux and Windows with Python 3.12 and 3.13;
  - a coverage floor.

  GPU tests skip without a device.
- `tools/run_benchmarks.py` records board SHA-256, versions, git commit and CPU, and takes `--trials N` (median).

**Documentation**
- `docs/CAPABILITIES.md` (new).
- Source-mutation wording is exact: normal workflows use working copies; only the explicit expert overwrite replaces the source, after confirmation and a backup.
- Stage design notes are marked historical, including the obsolete Stage 6 dpnp description.

**Known limitations** — see `KNOWN_LIMITATIONS.md`. In short:
- dense 4-layer boards are partly routed;
- no impedance control or length tuning;
- differential pairs are soft;
- no Iris Xe measurements yet;
- the installer is unsigned.

## v1.1.0 — audit repairs + dense-board routing

**Router correctness on dense boards** — custom-rule `=~` regex scope (an
unparsable `A.NetName =~ 'BUS.*'` used to poison every width check into
RULE_UNKNOWN); KiCad-canonical lowercase `A.Type` matching (`Track` never
matched `track`); sound occupancy grids (half-cell-diagonal segment margin so
the grid stops promising paths the validator refuses); same-net copper wins
foreign-clearance ties (escape sources stay passable); exact group attribution
(no more first-remaining fallback); bounded limit escalation on TIMEOUT for
single-net routes; stale success messages cleared.

**Repair stages 1–5** — constraints dialog layout, history exception safety,
single bulk-apply; typing-safe shortcuts, undoable constraints/corridors,
dialog singletons, scrolled settings; hover throttle, linear board sync, leaner
A* loop, cancellable grid builds; prompt-injection hardening (history replay +
case-insensitive tags), strategy safety checks, insecure-HTTP key block, model
cache TTL, usage caps, context-window guard; version cross-check, `.sha256`
sidecar, installer size guard, docs truth pass.

**Measured** — 120×80 mm 104-pad 32-net stress board: Speed ~18/32 valid in
~15 min, Accuracy adds more; small/medium boards route fully in
seconds–minutes. Full-stack via spans and sub-hour monster boards remain known
limitations (see docs/stability.md).

## v1.0.0 — first stable release

Everything in 0.9.1–0.9.2, plus: pour-aware connection setup (single-pass
group raster with cancel points and live progress), cancellable big-polygon
raster paths, versioned releases (`1.0.0`, plain PEP 440), public README/docs
index/changelog, stability contracts, bundled license documents, hidden helper
consoles, and the proprietary Henderson Engineering EULA.

## v0.9.1-stage9 — reliability + speed + AI foundations

**Router reliability** — routing worker isolation fixes (freerouting wait,
hang cancellation, monitor race, shutdown), timeout/cancel propagation
(pause deadlines, cancellable grid builds, optimize budgets, search bounds),
GPU reliability (failed-init latching, runtime rescans, honest detection,
VRAM guards), UI responsiveness (thread-safe engine, non-blocking submit).

**Live routing UX** — routed traces preview as they complete; cancel/timeout
keeps streamed nets for review and accept instead of discarding everything;
RULE_UNKNOWN batches explain missing widths.

**Router speed (R1–R7, measured on i5-1235U)** — phase timings in metrics/UI/
bench; shared grid inputs across passes; A* hot-loop opts; weighted search
(133× fewer expansions, proven ≤1.5× cost bound); proven-exact slack pruning;
honest wavefront backtrack; DRC-clean rate; determinism tests; failure-aware
retry budgets; congestion-aware default ordering (33% faster); threaded
occupancy builds; per-library AUTO GPU thresholds; **Speed/Accuracy toggle**
in the Route panel (17× faster, same completion on the dense fixture).

**Power nets** — smallest-group-first sources, boundary-preserving source
thinning, instant RULE_UNKNOWN refusal with guidance instead of minutes of
futile search.

**AI** — reasoning disabled for structured local requests (with fallback);
request timeouts honor profile timeouts; planner output budget 8192→2048;
capability-aware model list (size/context/thinking badges, RAM warnings);
per-board memory (notes + approved decisions, approval-gated); versioned
planner strategies with deterministic benchmark; interaction ratings.

**Packaging** — per-user SYCL runtime discovery (loader + bundle); native GPU
queries isolated in child processes (a broken driver can no longer segfault
the app); portable NSIS build path documented.

## v0.9.0-stage9 — Stage 9 + audit baseline

Full-board CPU/GPU routing, Freerouting engine, validated commit/undo, export
to new files, crash recovery, diagnostics. Full architecture audit with a
prioritized repair plan (routing freezes, UI stalls, GPU init, packaging).
