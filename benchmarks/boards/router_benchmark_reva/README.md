# Router_Benchmark_RevA

Purpose: a deliberately **unrouted 2-layer KiCad board** for testing an autorouter.

## Files
- `Router_Benchmark_RevA.kicad_pcb` — native KiCad board with footprints and ratsnest nets.
- `Router_Benchmark_RevA.kicad_pro` — project/net-class settings.
- `Router_Benchmark_RevA.kicad_dru` — KiCad custom DRC/routing rules.
- `Router_Benchmark_RevA.dsn` — Specctra DSN export for autorouters; routing classes are embedded.
- `Router_Benchmark_RevA.net` — KiCad-style legacy netlist export.
- `nets.csv` — flat net/end-point export for parser tests.
- `router_rules.json` — explicit machine-readable routing rules.
- `manifest.json` — expected counts and validation targets.

## Board challenge
- Board: 120 mm x 80 mm, 2 copper layers.
- Electrical pads: 104.
- Nets: 32 total (30 signal + 2 power).
- Initial copper tracks/vias: **0 / 0**.
- The placement intentionally creates many crossing connections between left/right, top/bottom and two central DIP-style matrices.

## Required rules
| Class | Track width | Clearance | Via |
|---|---:|---:|---:|
| DEFAULT | 0.25 mm | 0.20 mm | 0.80 / 0.40 mm |
| BUS | 0.30 mm | 0.20 mm | 0.80 / 0.40 mm |
| FAST | 0.35 mm | 0.25 mm | 0.80 / 0.40 mm |
| POWER | 0.80 mm | 0.30 mm | 1.00 / 0.50 mm |

Global edge clearance: 0.50 mm. Through vias only. Route on `F.Cu` and `B.Cu`.

## Pass condition for your router
1. 100% connections routed.
2. 0 shorts.
3. 0 width/clearance/edge-rule violations.
4. No traces outside the board.
5. No blind/buried vias.
6. Preserve all net names and endpoint membership.

The `.dsn` is the best direct input for a Specctra-style routing pipeline; the KiCad files let you visually inspect and DRC the result after import.
