# Roadmap

Each stage ends with tests, a completion audit and an acceptance checklist. A stage may
not start until the previous stage is accepted.

| # | Stage | Key deliverables |
|---|---|---|
| 1 | **Foundation + KiCad board inspector** *(done)* | Domain model, read-only KiCad loader, viewer, inspector, nets/layers panels, settings, logging, compute detection, command bus. |
| 2 | **AI provider layer + prompt-to-constraint compiler** *(done)* | OpenAI / Anthropic / OpenAI-compatible providers, keyring credentials, context builder, anonymisation, v2 command schema, semantic validation, proposal preview/approval, history. No execution. |
| 3 | Board rules, connectivity and DRC | Net classes and rules from `.kicad_pro`/`.kicad_dru`, zones/keepouts, connectivity graph + ratsnest, spatial index, deterministic DRC ("DRC RESULT" claims), persistent constraint set. |
| 4 | CPU router core | Grid/maze router for one net across layers with vias; route proposals with before/after preview; executes approved `route_net` commands. |
| 5 | Board-level routing | Net ordering, `route_group`/`route_board`, rip-up & reroute, optimisation (`optimize_net`, `reduce_vias`), progress/cancel. |
| 6 | Advanced constraints | Differential pairs, length matching/tuning, critical nets, protected areas, locked items enforced by the router. |
| 7 | AI agent loop | Deterministic board-query tools for the model, multi-step plans, AI-assisted review of routing results — still approval-gated. |
| 8 | GPU acceleration | CUDA backend implementing `ComputeBackend` with CPU fallback and parity tests. |
| 9 | Safe KiCad writing | Writer for tracks/vias, validate-by-reparse, workspace snapshots, atomic replace, undo/redo of accepted proposals, export. |
| 10 | Productisation | Installers (Windows, Linux AppImage), performance, plugin/scripting API over the command bus, user docs. |

## Parked ideas (not scheduled)

- 3D board preview.
- KiCad IPC API live connection (KiCad 9+).
- Push-and-shove interactive routing.
