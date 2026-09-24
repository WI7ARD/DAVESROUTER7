# Roadmap

Each stage ends with tests, a completion audit and an acceptance checklist. A stage may
not start until the previous stage is accepted.

| # | Stage | Key deliverables |
|---|---|---|
| 1 | **Foundation + KiCad board inspector** *(done)* | Domain model, read-only KiCad loader, viewer, inspector, nets/layers panels, settings, logging, compute detection, command bus, AI command schema. |
| 2 | Board model completion | Zones/keepouts (rendered), footprint graphics, net classes and rules from `.kicad_pro`/`.kicad_dru`, connectivity graph + ratsnest (unrouted connections), per-net routing status. |
| 3 | Rules + DRC engine | Constraint resolution (board → net class → net → command), clearance/width/via/edge checks, DRC report panel, spatial index (R-tree) shared with the router. |
| 4 | CPU router core | Grid/maze router for a single net across layers with vias; `RouteProposal` preview and before/after diff in the viewer; accept/reject in memory; "Route Selected Net". |
| 5 | AI provider integration | OpenAI, Anthropic, OpenAI-compatible and local providers behind `AIProvider`; keyring credentials; context policy UI; prompt → validated `PCBCommand` → command bus. |
| 6 | Board-level routing | Net ordering, "Route Board", rip-up & reroute, route optimisation (length, vias, corners), progress/cancel. |
| 7 | Advanced constraints | Differential pairs, length matching/tuning, critical nets, protected areas, locked tracks/components. |
| 8 | GPU acceleration | CUDA backend implementing the `ComputeBackend` interface for batch path search / cost maps, with CPU fallback and parity tests. |
| 9 | Safe KiCad writing | Writer for tracks/vias, validate-by-reparse, workspace snapshots, atomic replace, undo/redo of accepted proposals, export. |
| 10 | Productisation | Windows installer and Linux AppImage, performance work, plugin/scripting API over the command bus, user documentation. |

## Parked ideas (not scheduled)

- 3D board preview.
- KiCad IPC API live connection (KiCad 9+).
- Push-and-shove interactive routing.
