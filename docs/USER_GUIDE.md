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
Settings ▸ Routing ▸ **Parallel routing**: *Auto* (recommended: CPU cores − 1, at most
4, on boards with 12+ nets), *Single worker*, or 2–4 workers. Helper processes route
nets that do not overlap at the same time; the main process validates and commits each
result, and re-routes anything that became illegal. Parallel routing is used with the
CPU backend only. *Single worker* gives identical results run after run; parallel runs
may differ slightly (all results are validated).

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
