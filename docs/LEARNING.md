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

## Level 2 — contextual bandit over search settings (next)

- **Arms:** a handful of pre-validated per-net search variants. For example:
  - the current preset;
  - no coarse-to-fine;
  - a finer grid for fine-pitch escapes;
  - a higher via cost;
  - a heuristic weight of 1.2.

  Each is already safe on its own; the validator gates everything.
- **Context:** a coarse bucket of net features (kind × pad count × escape options ×
  layer count).
- **Reward:** routed within its time slice, minus a small cost for vias and
  time.
- **Policy:** Thompson sampling per bucket, trained offline on the Real100
  log, then updated online from the user's own log.
- **Guard rails:**
  - a *frozen* switch for benchmarks and for deterministic, repeatable runs;
  - the policy only reorders among safe arms;
  - accepted only if `tools/real100_compare.py` shows no regression against
    the fixed router (same rules as `.claude/skills/real100-tune`).

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
