---
name: real100-tune
description: Benchmark DAVESROUTER against the Real100 corpus (100 pinned real KiCad boards) and tune the router for completion without weakening validation. Use when asked to run Real100, measure routing on real boards, find and fix router failures, or check a router change for regressions.
---

# Real100: benchmark and tune the router

Real100 is 100 real KiCad boards (80 PCBNew QA boards + 20 official demos) pinned to one
kicad-source-mirror commit (`benchmarks/real100/manifest.json`). Each board/mode routes
in a fresh subprocess with a hard timeout. The goal of tuning is **more nets routed,
validator-clean, in the same or less time**. The goal is never a better-looking number.

## Hard rules (never break these to gain score)

- Never weaken the exact validator or skip validation. Never mark a net routed without copper.
- Never turn conservative rule handling off in default results. `--conservative no` is
  an expert comparison, reported separately.
- A `RULE_UNKNOWN` failure on a board without a `.kicad_pro` is **correct** behaviour.
  Count it, do not "fix" it.
- Do not raise timeouts to hide hangs, and do not delete or skip difficult boards.
- Do not change source files while a benchmark is running. Workers import `src/` fresh
  for every board, so an edit mid-run contaminates the baseline.
- Timing is machine-dependent. Compare completion, validity, vias, timeouts and crashes.
  Mention time only for the same machine and the same load.

## 0. Setup (once per machine)

```bash
python tools/benchmark_real100.py fetch       # downloads ~208 MB, verifies Git blob SHA-1
python tools/benchmark_real100.py prepare     # strips top-level segment/via/arc only
python tools/benchmark_real100.py inventory   # all 100 must load
```

`benchmarks/real100/work/` is gitignored and holds the downloads, prepared boards
and runs.

## 1. Baseline (before touching code)

Run Speed and Accuracy as two processes in parallel. They use one core each; four
or more cores are recommended.

```bash
python tools/benchmark_real100.py run --profile standard --modes speed    > speed.log &
python tools/benchmark_real100.py run --profile standard --modes accuracy > acc.log &
```

Each command prints its `.jsonl` path at the end. Record:

- the git commit;
- the machine;
- whether the two modes ran in parallel.

Summarise with:

```bash
python tools/real100_compare.py benchmarks/real100/work/runs/<speed>.jsonl benchmarks/real100/work/runs/<acc>.jsonl
```

`smoke` (12 boards, Speed, 45 s) is a quick check after a small change. It is not
enough for a tuning decision.

## 2. Classify every failure

The harness records `failure_reasons` and `failed_examples` per board. Bucket them
into the categories below. Only the last group is router work.

| Bucket | Meaning | Action |
|---|---|---|
| `NOTHING_TO_ROUTE` | no net has two or more unconnected pads (QA boards test the parser, not routing) | none |
| `RULE_UNKNOWN` | no project rules; conservative mode refuses to guess widths | none. Correct refusal |
| `timeout` / `worker_error` | the process died or overran | always investigate: a crash or a hang is a bug |
| `VALIDATION` | the search keeps offering paths the validator refuses | **grid / validator mismatch: fix the grid** |
| `NO_PATH` / `NO_ESCAPE` | the search proved there is no path on the grid | congestion, fine pitch, rules. Tune the search |
| `TIMEOUT` (net) / budget ran out | limits, not geometry | ordering, time slices, pass structure |

To debug one board:

```bash
python .claude/skills/real100-tune/debug_board.py K037 speed 60
```

It prints the rules found, pre-route connectivity, the plan, and every failed net
with its reason and message. For a `VALIDATION` failure, log what the validator
rejected: wrap `engine.validator.validate_segment` and print the collisions. The
fix almost always belongs in `routing/occupancy.py`, making the grid demand what
the validator demands (see Lessons).

## 3. Fix one root cause at a time

1. State the hypothesis in one line, e.g. "grid inflates by `req.value`, validator
   enforces `possibly_stricter`".
2. Make the smallest change that removes the mismatch. Grid changes must make the
   search **stricter or equal** to the validator, never looser.
3. Add a unit test that reproduces the mechanism on a synthetic board, without
   network or Real100 files.
4. A/B on the affected boards only:
   ```bash
   python tools/benchmark_real100.py run --profile standard --ids K037,K022 --modes speed
   python tools/real100_compare.py BASE.jsonl --candidate CAND.jsonl
   ```

## 4. Accept or reject

Re-run the full standard profile, both modes, on the same machine with the same
parallelism. Then:

```bash
python tools/real100_compare.py BASE_speed.jsonl BASE_acc.jsonl \
    --candidate CAND_speed.jsonl CAND_acc.jsonl --fail-on-regression
```

Accept only if **all** of these hold:

- no regression, meaning no board/mode:
  - routes fewer nets;
  - newly times out or crashes;
  - needs more than 25 % extra vias for the same completion;
- total nets completed is ≥ baseline, and strictly more for a tuning change;
- the guard boards are unchanged or better:
  ```bash
  python tools/run_benchmarks.py --out bench_out --modes speed accuracy --workers 0 \
      --boards small_2layer medium_4layer dense_2layer
  ```
  Recorded values: small_2layer 11/11; medium_4layer 40/44 (Speed), 44/44 (Accuracy);
  dense_2layer 35/35. Board-router regressions often show up here first;
- `pytest -q`, `ruff check src tests tools`, `black --check src tests tools` and
  `mypy src` are clean.

If a board regresses, understand why before accepting any trade-off. If you do
accept one, name it in the commit message.

## 5. Record

- Commit message: the mechanism, the before/after totals (e.g. "Speed 1234/1500 →
  1260/1500 nets"), the per-board changes, and the machine.
- `docs/BENCHMARKS.md` → "Real100": totals per mode, the per-bucket failure table,
  commit, machine, and profile/timeout.
- Keep the `.jsonl` files out of git. Paste the totals instead.

## Lessons so far

- **Speed's fine-grid retry pays off only for small failures.** Retrying every proven
  NO_PATH on the 0.1 mm grid cost 13 nets on a 209-net board. `REFINE_MAX_NODES`
  limits it to boxed-in pads.
- **A wide net class cannot reach fine-pitch pins** (no neck-down). This is a board
  or rules issue, not a search bug. Report it; do not tune around it.
