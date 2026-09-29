# Changelog

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
