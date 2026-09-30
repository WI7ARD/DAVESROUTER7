# Learning from routing (design and status)

The router can learn which **search settings** work for which kinds of nets. It
never learns what is **legal**: every piece of copper is still checked by the exact
validator, and conservative rule handling still refuses unknown rules. A bad
learned choice can make routing slower or less complete, never wrong.

## Level 1 — experience log (implemented)

After every board-routing job the app, the command line and the Real100 harness
append one record per net to a local JSON-lines file:

| Part | Contents |
|---|---|
| `board` | a salted hash of the board fingerprint (salt is random per installation) |
| `board_f` | copper layers, pads, nets, footprints, zones, width and height |
| `net_f` | kind (signal/power/diff pair/group), pads, airwire length, escape options, congestion, priority, routing order |
| `settings` | mode, grid, heuristic weight, coarse factor, passes, rip-up, budget, workers, strategy |
| `outcome` | status, failure reason, vias, length, passes, attempts, expanded nodes, seconds, ripped up |

**Privacy.** Net names, references, coordinates and file paths are never stored,
and nothing leaves the computer. The log lives in the user data folder:

- Windows: `%LOCALAPPDATA%\AI PCB Router\experience\`
- Linux: `~/.local/share/<app>/experience/`

The file is capped at 50 MB, and the previous file is kept once as `.1`.

**Turning it off:**

- in the app: Settings ▸ Routing ▸ *Keep a local routing log to learn from*;
- for one CLI run: `--no-experience`.

Real100 runs write to `benchmarks/real100/work/experience/` (gitignored)
unless `--no-experience` is given. This is the offline training and
evaluation set: about 3,900 nets per standard run.

## Level 2 — contextual bandit over search settings (implemented; being evaluated)

- **Arms** (`pcbrouter.learning.policy.ARMS`): per-net search variants that only
  change *how* the router searches, never the rules:

  | Arm | Change |
  |---|---|
  | `preset` | the mode's normal settings |
  | `no_coarse` | no coarse-to-fine pre-search |
  | `finer_grid` | half the grid pitch (not below 0.05 mm) |
  | `greedier` | heuristic weight × 1.25 (at least 1.25) |
  | `fewer_vias` | minimise vias |

  The exact validator still checks every route, so an arm can make routing
  slower or less complete, never illegal.
- **Context:** a coarse bucket of kind × pads × escape options × copper layers,
  plus first attempt vs retry (retries get a bigger time slice, so they are
  learned separately).
- **Reward:** 1 when the attempt routed the net, else 0. Arm costs (seconds and
  expanded nodes) are reported alongside, not yet part of the reward.
- **Policies:** `fixed` (the preset everywhere, the default), `random:SEED`
  (exploration: every arm on every kind of net), and a trained `policy.json`
  (frozen Thompson: picks the arm with the best posterior mean, but only when
  it has at least 8 trials and beats the preset by at least 0.05).

### Training a policy

1. **Collect exploration data** (random arms, so the data is unbiased):

   ```
   python tools/benchmark_real100.py run --profile standard --policy random:1
   ```

2. **Train:**

   ```
   python tools/train_policy.py --log benchmarks/real100/work/experience \
       --out policy.json --report policy_report.json
   ```

   The trainer (`pcbrouter.learning.trainer`):
   - uses only records logged by the random policy (`--policies` to change);
   - splits by **board** (`--holdout 0.3`, `--seed`), so no board is in both
     halves;
   - learns the frozen policy from the training boards;
   - scores it on the held-out boards by **replay**: with random logging, the
     attempts whose logged arm matches the policy's choice are an unbiased
     sample of what the policy would get. The preset is scored the same way,
     and the difference comes with a 95 % interval and a plain verdict
     (better / worse / no clear difference);
   - prints per-context wins/trials and the median cost per arm.

   The policy file holds counts and the training summary only, no board
   identities.
3. **Accept it only on Real100:**

   ```
   python tools/benchmark_real100.py run --profile standard --ids <held-out> --policy policy.json
   ```

   Compare against the same boards with `--policy fixed` using
   `tools/real100_compare.py`. No regressions allowed (same rules as
   `.claude/skills/real100-tune`). The replay number predicts per-attempt
   success, not board completion under a time budget, so it is a filter, not
   the acceptance test.

4. **Use it:** `pcbrouter BOARD.kicad_pcb --route --policy policy.json`
   (also `tools/run_benchmarks.py --policy`). The CLI warns when a policy trained
   for one mode is used in the other. Train one policy per mode (`--mode`): the
   same arm means different things on top of the Speed and Accuracy presets.

**Guard rails:** frozen policies are deterministic; `fixed` stays the default
until a trained policy has passed the A/B; the desktop app does not load a
policy yet.

## Level 3 — learned difficulty and ordering (later)

A small gradient-boosted-tree model predicts which nets are hard (from `net_f`
and `board_f`), to route them first or give them more time. Trees rather than a
neural network: tabular features, little data, and results that can be explained.

## Level 4 — neural networks (not yet)

When neural nets make sense:

- **Data:** tens of thousands of routed nets across a wide variety of boards.
  One Real100 standard run gives about 3,900. Levels 2 and 3 must have shown
  that learned choices beat the fixed ones.
- **A task where they beat trees:** for example a congestion map predicted from
  board images, used as a search cost. That is research-grade work, so it would
  be judged only on Real100 completion and validity.
- **Out of scope:** end-to-end RL that draws copper. Published results do not
  beat A* + rip-up on real boards, and the validator would still have to gate
  every segment.
