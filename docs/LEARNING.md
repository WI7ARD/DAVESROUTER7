# Learning from routing (design and status)

The router can learn which **search settings** work for which kinds of nets and
boards. It never learns what is **legal**. This boundary is permanent:

```
LEARNING SYSTEM          chooses a strategy (search settings) — or falls back to fixed
      │
DETERMINISTIC ROUTER     generates candidate copper
      │
EXACT VALIDATOR          decides whether copper is legal
```

Every piece of copper is still checked by the exact validator, and conservative
rule handling still refuses unknown rules. A bad learned choice can cost time,
vias or completion, never produce accepted illegal copper.

## Level 1 — experience log (implemented)

After every board-routing job the app, the command line and the Real100 harness
append one record per net to a local JSON-lines file (schema
`pcbrouter-experience/2`; readers also accept 1). The field-by-field
reference, with units, the version that introduced each field and the
compatibility rules, is [`docs/EXPERIENCE_SCHEMA.md`](EXPERIENCE_SCHEMA.md).

| Part | Contents |
|---|---|
| `board` | a salted hash of the board fingerprint (salt is random per installation) |
| `app`, `router` | the app version, and the app, geometry-engine and rule-engine versions that produced the outcome |
| `board_f` | copper layers, pads, nets, footprints, zones, width and height |
| `board_p` | the board profile (`learning.features`): routable (non-plane) layers, area, nets to route, pads per net, pad density, fine pitch, airwire length, long-net share, routing demand, low-escape share, congestion, … |
| `net_f` | kind (signal/power/diff pair/group), pads, airwire length, escape options, congestion, priority, routing order, context bucket |
| `settings` | mode, grid, heuristic weight, coarse factor, passes, rip-up, budget, workers, strategy |
| `outcome` | status, failure reason, vias, length, passes, attempts, expanded nodes, seconds, ripped up, arms, policy, and `trace`: **one entry per attempt** with the settings actually used (pass, arm, grid, heuristic weight, coarse factor, via cost, wrong-way factor, node limit, time slice) and its result (status, reason, expanded nodes, seconds) |
| `job` | board-level results: nets completed/attempted, rip-ups, runtime, new vias, policy decision, reason and policy id |

**Privacy.** Net names, references, coordinates and file paths are never stored,
and nothing leaves the computer. The log lives in the user data folder:

- Windows: `%LOCALAPPDATA%\AI PCB Router\experience\`
- Linux: `~/.local/share/<app>/experience/`

The file is capped at 50 MB, and the previous file is kept once as `.1`.
Before an old `.1` is dropped, its records are merged into
`experience.keep.jsonl` (**stratified retention**): the newest 100 records per
stratum (mode + context bucket) are kept, halving that number until the keep
file is at most half the cap. Plain first-in-first-out rotation would lose rare
kinds of nets (diff pairs, large power nets) first.

**Turning it off:**

- in the app: Settings ▸ Routing ▸ *Keep a local routing log to learn from*;
- for one CLI run: `--no-experience`.

Real100 runs write to `benchmarks/real100/work/experience/` (gitignored)
unless `--no-experience` is given.

## Level 2 — contextual bandit over search settings (implemented; EXPERIMENTAL)

This section describes version 3 of the policy (v3). The per-board selector
and the `medium_4layer` analysis further down come from the evaluation of the
earlier versions and still apply.

- **Arms** (`pcbrouter.learning.policy.ARMS`): per-net search variants that only
  change *how* the router searches, never the rules:

  | Arm | Change |
  |---|---|
  | `preset` | the mode's normal settings |
  | `no_coarse` | no coarse-to-fine pre-search |
  | `finer_grid` | half the grid pitch (not below 0.05 mm) |
  | `greedier` | heuristic weight × 1.25, at least 1.25, **at most 1.5** |
  | `fewer_vias` | minimise vias |

  `greedier` is capped at 1.5, the Speed preset's documented bound (paths at
  most 1.5 × optimal), so no arm weakens that promise. On Speed (already 1.5)
  it changes nothing. Any arm that leaves the request unchanged is recorded
  as `preset`, so its outcome counts for the preset.

- **Context** (`bucket` and `attempt_context`), for example
  `signal|p0|e2|l0|d1|retry:no_path`:

  | Part | Bands |
  |---|---|
  | kind | `signal`, `power`, `diff_pair`, `group` |
  | `p` pads | ≤ 2, 3–4, 5–12, > 12 |
  | `e` escape options | ≤ 2, 3–6, 7–20, > 20 |
  | `l` routable layers | ≤ 2, 3–4, > 4 |
  | `d` board density | routing demand (airwire length per routable area and layer, 1/mm): `d0` ≤ 0.03, `d1` ≤ 0.13, `d2` above; `d?` when a record has no board profile |
  | attempt | `first`, or a retry by the previous attempt's failure reason: `retry:limit` (TIMEOUT, VIA_LIMIT, LAYER_RESTRICTION), `retry:no_path` (NO_PATH), `retry:other` (any other reason), `retry:unknown` (no reason recorded, e.g. old records) |

  Routable layers = copper layers minus the ones a zone mostly covers: a
  4-layer board with two planes routes like a 2-layer board. The density edges
  are the tertiles of the 41 routable Real100 boards, so each band holds about
  a third of the corpus. A retry after running out of budget needs different
  help than a retry after a proven NO_PATH, so they are learned apart.

  Contexts are derived at training time from the raw recorded features, never
  read from the stored bucket, so older logs stay trainable when the context
  definition changes (see `docs/EXPERIENCE_SCHEMA.md`).

- **Reward and cost:** an attempt is a trial for its arm and a win when it
  routed the net. Its cost is the attempt's search time from the trace (for a
  single-attempt record without a trace, the net's search time). Each
  (context, arm) keeps its wins, trials and **median attempt cost**.

- **Decision rule (frozen policies).** In each context an arm may replace the
  preset only when, with at least 8 trials, either:

  1. it **routes significantly more**: the lower end of its 95 % Wilson
     interval is above the upper end of the preset's; or
  2. it is **much cheaper at no loss**: its median cost is at most half the
     preset's and its mean success is at least the preset's.

  Among the arms that pass, the one with the highest cost-discounted value
  `p · τ / (τ + cost)` is chosen, where `p` is the posterior mean success and
  `τ` is the preset's median cost in that context; it must also score at
  least the preset's value. Board jobs run under time budgets, so a few more
  routed nets at ten times the time should not win. With 8 trials a Wilson
  interval is about ± 0.3, so small leads never count. A context where
  the preset was never measured never changes. (Before v3 the rule was a flat
  0.05 margin over the preset's mean; the `margin` field is still read from
  old files but no longer decides.)

- **Policies:** `fixed` (default), `random:SEED` (exploration), a trained
  policy file (frozen, the rule above), or a **policy set** (one policy per
  mode; the same arm means different things on top of the Speed and Accuracy
  presets). An unfrozen policy samples the posterior, discounted by cost the
  same way.

- **Version stamp.** A trained policy file records `router_signature()`: the
  app version, the geometry-engine version and the rule-engine version it was
  learned on. If any of them differs from the running router, the router
  does not use the policy: the job runs the fixed router and records
  `FALLBACK_FIXED` with the reason ("policy was trained on another router
  (…); retrain it"). Policy files from before v3 carry no stamp and are not
  checked.

### Why a per-net policy is not enough (the `medium_4layer` regression)

The first learned policy gained on held-out Real100 boards but lost nets on the
`medium_4layer` guard board (Speed 40 → 38, Accuracy 44 → 39). Traced per
attempt with identical settings, net order and a single worker:

- the lost nets fail **NO_PATH after a few hundred expansions** on their first
  attempt. A* weight and arm choice cannot change whether a path exists on a
  given grid, so the space was sealed off by the copper of nets routed before
  them, not by a lack of time;
- the earlier nets had routed *successfully* with `greedier` (and, when that
  arm is removed, with `finer_grid`): their different route shapes consumed
  channels on a board where 91 % of nets are long and 82 % have little escape
  room;
- in Accuracy the resulting tangle also defeats rip-up (1 of 10 rip-ups
  succeed, against 3 of 6 for the fixed router), and the job uses its whole
  budget;
- ablations: greedier contexts back to the preset → 40/44 (same as fixed);
  any other change to those first attempts → 38–39/44.

A per-net reward ("did *this* net route?") cannot see what a net's copper does
to the nets after it. `medium_4layer` also has four free signal layers, while
the 4-copper Real100 training boards have planes: the old context (copper
layers) lumped them together. Two further faults showed up in the evaluation:
the held-out split leaked (Real100 K067 and K088 are variants of one design,
one in each half), and single A/B runs on time-budgeted boards vary by up to
±35 nets with machine load.

### Policy selection: when to trust a learned policy

`pcbrouter.learning.selector.SelectivePolicy` decides **per board** before
routing. The version stamp is checked first: a policy learned on another
router is never used (see *Version stamp* above).

```
board profile ──► out of distribution? ─────────────── yes ──► FALLBACK_FIXED (reason)
                      │ no
                      ▼
      similar training boards measured better ───────── no ──► FALLBACK_FIXED (reason)
      under this policy (board-level A/B evidence)?
                      │ yes
                      ▼
      LEARNED: the policy picks search settings per net (decision rule per context)
```

- **Out of distribution:** standardized distance from the board's profile to the
  nearest training board, against a radius = the 75th percentile of the
  training boards' own nearest-neighbour distances. The reason names the
  features that differ most, with the training range.
- **Evidence:** board-level A/B results (policy minus fixed, nets completed) on
  the most similar training boards within the radius. Any similar board that
  lost nets, or no measured gain, means fallback. This is the check that sees
  what the per-net reward cannot.
- Every decision is recorded (`BoardRoutingResult.policy_decision`, the CLI
  report, Real100 result lines and experience records): decision, reason,
  policy id, nearest distance, radius, similar boards and their evidence.
- The fixed router is always the fallback and stays the default.

### Training and evaluating a policy

1. **Exploration data** (random arms, unbiased):

   ```
   python tools/benchmark_real100.py run --profile standard --policy random:1
   ```

2. **Train** one policy per mode, split by board group:

   ```
   python tools/train_policy.py --log benchmarks/real100/work/experience \
       --per-mode --no-require-evidence --out policy_inner.json --report report.json
   ```

   - only random-arm records (`--policies`);
   - only records written by the running app version by default (`--app X,Y`
     for other versions, `--app any` for all). Router behaviour changes between
     versions, so mixed-version data describes different routers. The report
     lists the versions read and the versions used;
   - boards with near-identical profiles are grouped (`--dup-radius`) and a
     group is never split between training and held-out;
   - replay estimate on held-out boards (per-attempt success vs the preset,
     95 % interval) — a filter, not the acceptance test;
   - the file records the policy id (content hash), the dataset id (hash of the
     training records), the router signature, the training boards' profiles
     (support) and the config. The report gives, for every context that
     changed from the preset, the arm and the rule that allowed it ("routes
     significantly more" or "much cheaper at no loss").

3. **Measure board-level evidence on the training boards** (paired, repeated):

   ```
   python tools/real100_compare.py --baseline fixed --candidate policy_inner.json \
       --repeat 2 --ids <training boards> --profiles --json evidence.json
   ```

4. **Retrain with evidence** (a selective policy set):

   ```
   python tools/train_policy.py --log ... --per-mode --evidence evidence.json --out policy.json
   ```

   Evidence is attached only to training boards; results measured on other
   boards are ignored.

5. **Acceptance** on held-out boards and the guard boards:

   ```
   python tools/real100_compare.py --baseline fixed --candidate policy.json \
       --repeat 3 --ids <held-out boards> --guard --check-validity --json ab.json
   ```

   Baseline and candidate run side by side, `--repeat` times; each board gets
   one paired difference per repeat and a verdict (improved / unchanged /
   regressed / noisy). The gate (`pcbrouter.benchmark.ab`):

   1. aggregate improvement (≥ 1 % more nets, more boards improved than regressed);
   2. no regressed Real100 board and no regressed guard board;
   3. no loss of validity (`--check-validity`: DRC errors on router-added copper);
   4. no more crashes or worker timeouts;
   5. reproduced (≥ 2 repeats, no improved board ever went the other way).

   **ACCEPTED** needs all five; a failure of 2–4 is **REJECTED**; otherwise
   **EXPERIMENTAL** (no harm found, benefit not established).

6. **Use it:** `pcbrouter BOARD.kicad_pcb --route --policy policy.json`
   (also `tools/run_benchmarks.py --policy`, Real100 `--policy`). The report
   says which policy routed the board and why.

**Reproducibility:** frozen policies are deterministic; `random:SEED` is seeded
per (net, attempt); policy and dataset ids identify exactly what ran. Timing
still matters on budget-limited boards, which is why the A/B pairs its runs
and repeats them.

### In the desktop app (opt-in)

Settings ▸ Routing ▸ *Experimental: learned search strategy*. It is off by
default, and the checkbox is enabled only when the app ships a policy file,
`src/pcbrouter/learning/data/policy_real100.json` (a policy set trained on
Real100). That file is shipped only if its evaluation (step 5) is not
**REJECTED**; no such file is in the repository yet.

When the option is on, the board-routing job loads that file. The version
check and the selector still apply per board, and the routing report says
whether the learned policy or the fixed router ran and why. A missing or
unreadable file means the fixed router, never a failed job.

### Status and results

> **Pending.** The v3 evaluation (training on the current app version, then
> the paired A/B of step 5 on held-out and guard boards) has not been run yet.
> Its gate status (ACCEPTED / EXPERIMENTAL / REJECTED), totals per mode, the
> guard-board results and the decision about shipping the bundled policy will
> be recorded here.

## Level 3 — learned difficulty and ordering (later)

A small gradient-boosted-tree model predicts which nets are hard (from `net_f`,
`board_p` and the attempt trace), to route them first, give them more time, or
choose the retry strategy after a cheap first attempt fails:

```
cheap attempt ──► success: keep
      │ failure (reason, expansions)
      ▼
difficulty model ──► retry strategy: finer grid / longer slice / other layer / rip-up
```

Trees rather than a neural network: tabular features, little data, results that
can be explained. The level 2 finding points the same way: first-attempt
choices affect other nets, so the most promising place for learning is *after*
a net has shown it is hard.

## Level 4 — neural networks (not yet)

When neural nets make sense:

- **Data:** tens of thousands of validated net-routing attempts across diverse
  real boards, not a few thousand concentrated in Real100.
- **Simpler methods have plateaued:** the bandit and trees must be shown to
  stop improving on the same evaluation, with the paired A/B gate.
- **A task where they beat trees:** for example a congestion map predicted from
  board images, used as a search cost. That is research-grade work, judged only
  on Real100 completion and validity.
- **Out of scope:** end-to-end RL that draws copper. Published results do not
  beat A* + rip-up on real boards, and the validator would still have to gate
  every segment.
