# Stage 4 — CPU autorouter v1

Version 0.4.0-stage4.

```
RouteRequest ─normalise (rules)─▶ SearchGrid ─A*─▶ grid path ─simplify─▶ segments/vias
   ─exact per-element validation (repair loop)─▶ RouteProposal ─validate_route─▶ candidate
   ─preview─▶ user Accept ─▶ WorkingBoard commit (undoable) ─▶ export (Stage 9)
```

| module | role |
|---|---|
| `routing/request.py` | `RouteRequest` (net, groups, width, layers, max vias, grid, style, limits, candidates, soft regions) and `normalise()` through the rule engine. A width below the rule minimum is INVALID; an unknown width is RULE_UNKNOWN (never guessed). |
| `routing/cost/model.py` | `CostModel`: every component explicit, in equivalent nm of track (via 2.5 mm, bend 45° 0.15 mm, bend 90° 0.5 mm, clearance proximity +30 %, non-preferred layer ×1.25, congestion, corridors, alternative diversity). |
| `routing/search/grid.py` | compiled search grid from the Stage 3 occupancy maps: passable cells for the track centreline (obstacles inflated by half width + resolved clearance), via mask (via-mode occupancy on every layer the through via spans, keepouts forbidding vias), proximity mask. |
| `routing/search/astar.py` | A* over (layer, cell, incoming direction[, vias used]); 8 directions (4 in orthogonal style); no turns sharper than 90°; no corner cutting; octile heuristic + via cost for other-layer targets (admissible); node/time limits and cancellation; deterministic tie-breaking. |
| `routing/path/simplify.py` | per-layer runs, collinear merge, octilinear shortcutting where the exact validator accepts every new segment, pad-centre snapping when legal. |
| `routing/router.py` | multi-group nets (N groups → N−1 connections, targets are any copper of the other groups), repair loop (block the offending object's region, re-search, ≤ 6), up to 5 diverse candidates, failure reasons (NO_ESCAPE, NO_PATH, VIA_LIMIT, LAYER_RESTRICTION, RULE_UNKNOWN, TIMEOUT) with blocking statistics. |
| `routing/working_board.py` | immutable-board commits with incremental geometry/index updates, provenance, locks, undo/redo, reset. |
| `commands/route_commands.py` | `RouteNetCommand`, `AcceptRouteCommand` (+ history entry), `ResetWorkingBoardCommand`. |
| `ui/routing_controller.py`, `ui/route_panel.py` | Router ▸ Route Selected Net (R), background job with Cancel, Route Review dock: candidates, metrics, Accept / Reject / Try Alternative, overlay preview; undo/redo through Edit. |

Guarantees: every committed segment and via passed `validate_segment` /
`validate_via` and the whole proposal passed `validate_route`; commits are
re-validated against the current working board (stale proposals are refused). The
source `Board` object and file are never modified.

Measured on the fixture `router_basic` (0.1 mm grid, this container): net A (two
vias under an F.Cu keepout) ≈ 1.9 s for 3 candidates; simple nets 20–120 ms.

Limitations: through vias only (no blind/buried/micro generation); widths are
constant per net (the max of per-layer rule widths); arc tracks are not generated;
the search grid samples cell centres (the exact validator is the authority, and
the repair loop handles misses).
