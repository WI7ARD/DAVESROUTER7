# Stage 3 — PCB geometry + design-rule engine

Version 0.3.0-stage3. The deterministic engine is authoritative; the AI never
decides legality.

```
KiCad board ─▶ KiCad adapter ─▶ domain model ─┬─▶ geometry engine ─┐
 .kicad_pro/.kicad_dru ─▶ rule adapter ───────┴─▶ rule resolver ───┤
                                                  spatial index ◀──┘
                                                        ▼
                                  collision engine ─▶ route validator ─▶ VALID / INVALID / RULE_UNKNOWN
                                                        ▼
                                              (Stage 4 router)
```

* [geometry_engine.md](geometry_engine.md), [rules_engine.md](rules_engine.md),
  [internal_drc.md](internal_drc.md), [connectivity.md](connectivity.md)
* Route legality API: `RouteValidator.validate_segment / validate_via /
  validate_route` with `RouteProposal` (in memory only, never added to the board).
* Occupancy grid (`routing.occupancy`, uint8 cells, width-dependent inflation,
  0.10/0.20/0.25/0.50 mm), geometric congestion estimate, pin escape analysis.
* UI: status fields `Geometry / Rules / Internal DRC / Routing`; Routing Rules
  inspector; Internal Geometry Check dock; Validate Test Segment / Via; Routing
  Grid; clearance envelope; debug overlays; overrides; Help ▸ Geometry Diagnostics;
  exports `rules_snapshot.json`, `geometry_summary.json`.
* AI: ROUTING_RULES / CONNECTIVITY sections are BOARD FACTs; prompts about widths or
  clearances get a `DETERMINISTIC RULE CHECK` shown next to (and above) the model.
* Benchmark: `python tools/bench_geometry.py`.
