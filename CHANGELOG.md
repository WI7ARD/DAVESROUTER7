# Changelog

## v1.1.1 — 4-layer routing, parallel routing, installable release

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

**Removed**
- The Freerouting integration: menu entries, setup dialog, `--freeroute` / `--setup-freerouting`, and the KiCad Specctra bridge. Old settings files that still contain Freerouting fields load normally.

**Product**
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
