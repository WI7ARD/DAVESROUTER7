# DAVESROUTER Benchmark Suite — 100 Real KiCad Boards

A reproducible, isolated benchmark corpus for testing DAVESROUTER against **100 real KiCad `.kicad_pcb` files** instead of only synthetic fixtures.

The suite contains:

- **80 KiCad PCBNew QA/regression boards** — parser, geometry, rules, zones, pads, vias, tuning, and historical edge cases.
- **20 official KiCad demo designs** — larger, more representative PCB projects, including dense and multilayer designs.
- **100 pinned source identities** — every board is tied to a Git blob SHA and to one pinned KiCad repository commit.
- **local unrouting** — existing top-level track segments, track arcs, and vias are removed from a generated benchmark copy while footprints, pads, nets, zones, keepouts, board outline, rules, project files, and unknown constructs are preserved.
- **hard process isolation** — every board/mode is routed in a fresh Python subprocess with a hard timeout.
- **Speed vs Accuracy comparisons** — both presets use DAVESROUTER's real `BoardRouter` and rules/connectivity engines.
- **machine-readable results** — JSONL, CSV, JSON inventory, and Markdown reports.

## Corpus source

The manifest is pinned to:

- Repository: `https://github.com/KiCad/kicad-source-mirror`
- Commit: `fec63a6f5197a584e27b1bc3e7b8431be9c05a78`
- Commit date: `2026-09-30T08:23:01Z`

The suite **does not vendor third-party board bytes**. `fetch` retrieves them directly from the pinned source. This keeps DAVESROUTER's source archive small and makes provenance auditable.

KiCad's `LICENSE.README` states that files under `demos/*` are licensed CC BY-SA 4.0. Other source/QA files remain subject to the licenses/notices in the KiCad repository and any file-specific notices. Generated unrouted copies are created locally for benchmarking and are not part of the DAVESROUTER distribution.

## Quick start

From the DAVESROUTER repository root:

```powershell
python tools\benchmark_real100.py list
python tools\benchmark_real100.py fetch
python tools\benchmark_real100.py prepare
python tools\benchmark_real100.py inventory
python tools\benchmark_real100.py run --profile smoke
```

The `run` command prints the result `.jsonl` path. Turn it into CSV + Markdown with:

```powershell
python tools\benchmark_real100.py report benchmarks\real100\work\runs\real100-smoke-YYYYMMDDTHHMMSSZ.jsonl
```

Or perform the whole flow in one command:

```powershell
python tools\benchmark_real100.py all --profile smoke
```

## Profiles

### `smoke`

Routes a fixed 12-board cross-section in **Speed** mode with a 45-second hard timeout per board. It still makes sense to run `inventory` across all 100 first.

Use this after routine router changes.

### `standard`

Routes all 98 non-torture boards in both **Speed** and **Accuracy** modes. Default hard timeout is 180 seconds per board/mode.

Use this for release candidates and substantial routing changes.

### `full`

Attempts all 100 boards in both modes, including the two enormous official KiCad demo boards (roughly 69 MB and 85 MB source files). Default hard timeout is 900 seconds per board/mode.

Use this as a torture/release qualification run, not as an everyday unit test.

## Useful commands

```powershell
# See corpus composition
python tools\benchmark_real100.py list

# Re-fetch everything and verify pinned Git blob identities
python tools\benchmark_real100.py fetch --force

# Regenerate local unrouted derivatives
python tools\benchmark_real100.py prepare --force

# Parser/adapter compatibility across all 100
python tools\benchmark_real100.py inventory

# Run only a few named boards
python tools\benchmark_real100.py run --profile standard --ids K001,K081,K096

# Run only Speed
python tools\benchmark_real100.py run --profile standard --modes speed

# Bound a debugging run
python tools\benchmark_real100.py run --profile standard --max-boards 10 --timeout 60

# Expert comparison only: disable conservative unknown-rule refusal.
# This changes the safety mode and must be reported separately from default results.
python tools\benchmark_real100.py run --profile smoke --conservative no
```

## Directory layout

After fetching/running:

```text
benchmarks/real100/
├── README.md
├── manifest.json
└── work/                       # gitignored
    ├── downloads/
    │   ├── K001/
    │   └── ...
    ├── prepared/
    │   ├── K001/
    │   └── ...
    ├── fetch_status.json
    ├── prepare_status.json
    ├── inventory.json
    ├── inventory.csv
    └── runs/
        ├── real100-*.jsonl
        ├── real100-*.csv
        └── real100-*.md
```

## What `prepare` removes

Only direct children of the outer KiCad board S-expression with these symbols are removed:

- `(segment ...)`
- `(via ...)`
- `(arc ...)` track arcs

The scanner is quote/escape aware and only removes top-level routing objects. It does **not** rebuild or normalize the rest of the KiCad file.

It intentionally retains:

- components/footprints;
- pads and net assignments;
- board outline;
- zones and copper pours;
- keepouts/rule areas;
- layer stack;
- net table;
- setup information;
- project rules (`.kicad_pro`);
- custom rules (`.kicad_dru`);
- graphics/text/groups;
- unknown constructs.

Zones may still electrically connect some power nets. The benchmark records pre-route connectivity so this remains visible rather than pretending every board begins with every net disconnected.

## Integrity model

Each manifest entry records the expected Git blob SHA-1 from the pinned KiCad repository tree. `fetch` recomputes Git's blob identity from downloaded bytes and refuses a mismatched board.

The generated prepared board also receives a normal SHA-256 in `prepare_status.json`.

## Metrics

Per run the suite records, where available:

- worker status / timeout / error;
- DAVESROUTER route status;
- nets attempted/completed/failed;
- completion percentage;
- total route length;
- new via count;
- expanded search nodes;
- rip-ups / reroutes / passes;
- clean-route percentage;
- routing phase timings;
- pre/post connectivity;
- source board complexity;
- project-rule availability;
- conservative-rule mode;
- total wall time.

## Interpreting failures

A corpus failure is data, not automatically a router bug.

Classify failures into at least:

1. **LOAD/PARSE** — DAVESROUTER cannot ingest the KiCad board.
2. **RULE_UNKNOWN/UNSUPPORTED** — safe conservative mode correctly refuses an unknown rule.
3. **NO_PATH/NO_ESCAPE** — routing search could not solve the geometry under current limits.
4. **TIMEOUT** — the entire isolated worker exceeded the benchmark deadline.
5. **VALIDATION** — candidate geometry failed exact validation.
6. **PARTIAL** — some but not all requested connections were completed.
7. **CRASH/WORKER ERROR** — unexpected exception or process failure.

Do not improve benchmark scores by weakening exact validation or silently disabling conservative rules. If expert-mode results are collected, publish them as a separate comparison.

## Benchmark stability

For meaningful before/after comparisons, keep fixed:

- Real100 manifest version and pinned source ref;
- DAVESROUTER version/commit;
- machine and OS;
- Python version;
- compute backend;
- profile and timeout;
- conservative-rule setting.

Timing results are machine-dependent. Completion, validity, crashes, timeouts, and route-quality changes are usually more portable than absolute seconds.
