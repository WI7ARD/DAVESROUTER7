# User guide

## What the router does
AI PCB Router adds tracks and vias to a KiCad board so that unrouted connections
(the ratsnest) are completed, obeying the board's own design rules. It never moves
footprints and never changes copper that is already on the board. Every track and via
it creates is checked by an exact geometry checker before it is kept, and the result
is written to a new file.

## Supported boards
| | Supported |
|---|---|
| KiCad files | KiCad 6, 7, 8, 9, 10 `.kicad_pcb` (older files load read-only; export needs KiCad 6+) |
| Copper layers | 2 layers (F.Cu, B.Cu) and 4 layers (F.Cu, In1.Cu, In2.Cu, B.Cu); boards with more layers load, but routing is only tested on 2 and 4 |
| Vias | Through vias (blind/buried vias are not generated) |
| Tracks | Straight segments at 0/45/90° (no arcs) |

## Design rules
Rules are read from the board's KiCad project files next to the board:

* `.kicad_pro` — net classes (track width, clearance, via diameter and drill), net
  class assignments/patterns, board minimums (e.g. copper-to-edge clearance);
* `.kicad_dru` — custom rules (conditions on net names, classes, layers, item types).

The router never invents a rule. If no rule states a width or clearance for a net, the
net is reported as **RULE UNKNOWN** with what to add. A rule the app cannot interpret is
listed in the Rules panel and in the result, not silently ignored.

## Routing modes
* **Accuracy** (default) — fine 0.1 mm grid (or your Settings value), optimal search
  inside a corridor around a coarse route, up to 3 passes with rip-up and retry.
* **Speed** — 0.2 mm grid, bounded weighted search (at most 1.5× the optimal cost),
  2 passes, no rip-up. Good for a quick first result.

Both modes: layers get a preferred direction (first layer horizontal, next vertical,
…), which keeps crossing-heavy boards routable; pass 1 gives every net a fair share
of the time budget, pass 2 finishes the rest.

## Parallel routing
Settings ▸ Routing ▸ **Parallel routing**: *Auto* (recommended: one helper per CPU core, at
most 4, on boards with 12+ nets whose nets are spread out enough to route side by side), *Single worker*, or 2–4 workers. Helper processes route
nets that do not overlap at the same time; the main process validates and commits each
result, and re-routes anything that became illegal. Parallel routing is used with the
CPU backend only. *Single worker* gives identical results run after run; parallel runs
may differ slightly (all results are validated).

## GPU acceleration (optional, Intel)
The CPU router is the default and always works. On Intel GPUs (Iris Xe, Arc) the
app can run each net's path search as a fused GPU kernel:

1. `pcbrouter.exe --gpu-check` prints a staged report. It shows the library, the
   SYCL devices, the selected GPU (Level Zero first), a memory allocation on it, a
   kernel run whose result is checked against the CPU, the routing kernel checked
   against the CPU, and a small benchmark. `gpu_ready: true` means all of that
   passed; otherwise the `verdict` names the stage that failed and why.
2. Settings ▸ Compute ▸ GPU (or `--route --backend gpu`) routes on the GPU. Every
   GPU path still goes through the same exact validator as CPU paths.
   *Auto* uses the CPU until measurements show the GPU is faster on a board size.
   If a GPU search fails, that search is redone on the CPU and the routing card
   says so. `--backend gpu` on the command line fails with a clear message when no
   usable GPU exists.

GPU paths ignore bend costs (the search state has no direction), so they can
differ slightly in shape from CPU paths. Both obey identical hard rules.

## Reading the result
The Routing Jobs panel and the status bar show, e.g.
`FULLY_ROUTED — 32/32 nets (100 %), 136 new via(s), 4050.8 mm, 58 s`.

* **FULLY_ROUTED** — every planned net is connected.
* **PARTIALLY_ROUTED** — some nets are not; each shows why, e.g.
  `no legal path on the routing grid between (93.8, 43.7) and (21.3, 43.7) mm` or
  `not attempted: the board time budget (600 s) ran out`.
* **CANCELLED** — you pressed Cancel; routes found so far can still be reviewed.

"Internal checks passed" means the app's own geometry check found no errors. It is
not KiCad's DRC: the export summary says **ROUTED** vs **ROUTED + KiCad DRC** (the
latter only when `kicad-cli` is installed and Settings ▸ Export ▸ Run KiCad DRC is on).

## Accepting part of a result
Tick the nets you want and press **Accept Checked**. If routing a net moved another
net's route (a rip-up), accepting it also accepts the moved route. The Message
column says so ("Accepting it also accepts X"), and the status line names the nets
that came along. An acceptance that would leave any connected net open is refused
and changes nothing.

Accepted copper is yours: later routing jobs never rip it up. To re-route an
accepted net, undo the acceptance or use the Workbench's local reroute.

## AI commands: what is enforced
Each constraint in an AI proposal is labelled **enforced**, **preference (not
guaranteed)**, **reported after routing** or rejected as **not supported**. The
**What will run** block shows the mode, rip-up and whether existing routes are
kept. It is produced from the settings the router will actually use. Differential
pairs are routed together, but gap, skew and impedance are not controlled. Length
targets are measured and reported, not tuned. Full table: `CAPABILITIES.md`.

## Learning from your routing (local)
With Settings ▸ Routing ▸ *Keep a local routing log to learn from* on (the
default), each board-routing job adds anonymised per-net records to a local file:

- features, search settings and the outcome for each net;
- no net names, no coordinates;
- nothing leaves the computer unless you export it and send it yourself.

The router will use this log to choose search settings for your kinds of boards;
the exact validator still checks every route. See `LEARNING.md`.

## Sharing learning data (optional)
More real boards make the router learn better. If you want to help, you can
export your log and send the file to the developer. This is entirely optional,
and the app never sends anything by itself.

- In the app: **File ▸ Export Learning Data…**, choose where to save
  (default name `pcbrouter-learning-data-<date>.zip`). A summary then shows how
  many records and boards the file holds, its size, and what is and is not in it.
- On the command line: `pcbrouter.exe --export-experience data.zip`
  (`--overwrite` replaces an existing file).

**What the file contains.** A zip with two plain-text files:

- `records.jsonl`: one JSON object per line, one line per routed net: net and
  board features (pad counts, lengths, layer counts, …), the search settings and
  the outcome, plus the app version;
- `manifest.json`: counts (records, boards, by mode and app version) and a note
  on the contents.

**What it does not contain.** No net names, reference designators,
coordinates, file paths or board files. Boards are identified only by a salted
hash, and the salt (kept in the `experience` folder) is **not** included, so the
ids cannot be matched to your boards.

**Check it yourself.** Unzip the file and open `records.jsonl` in any text
editor, or read it with a few lines of Python:

```python
import json, zipfile
with zipfile.ZipFile("pcbrouter-learning-data-2026-10-01.zip") as z:
    for line in z.read("records.jsonl").splitlines()[:3]:
        print(json.dumps(json.loads(line), indent=1))
```

Field reference: `EXPERIENCE_SCHEMA.md`.

## Files
* Output: `<name>_routed.kicad_pcb` next to the source (a sidecar `.pcbrouter.json`
  records provenance). Overwriting the source must be enabled explicitly (Settings ▸
  Export) and makes a backup first.
* Settings: `%APPDATA%\AI PCB Router\settings.json` (Settings ▸ Restore Defaults resets
  them). Logs: `%LOCALAPPDATA%\AI PCB Router\logs\`.

## Command line
`pcbrouter.exe board.kicad_pcb --route` routes without opening the window (same
rules, presets, parallel helpers and checks); `pcbrouter.exe --help` lists every
option and the exit codes (0 fully routed, 3 partially routed, 1 error).
