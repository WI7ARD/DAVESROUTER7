# Stage 8 — interactive routing workbench

Version 0.8.0-stage8. All panels are dockable (View menu). Default layout: left —
Project, Layers; centre — PCB canvas; right — Inspector/Nets, AI Engineering /
Route Review; bottom — Log, AI History, Internal Geometry Check, Routing Rules,
Routing Jobs.

| feature | where |
|---|---|
| visual states: file copper (solid), working copper (dashed), proposed route (dashed overlay coloured by validation status, tooltip "PROPOSED"), selection, DRC markers, locked copper (dashed white outline + tooltip "LOCKED"), AI target (proposal's net highlighted) | canvas |
| before/after: Working, Original, Overlay, Difference | View ▸ Board View |
| route diff: +segments/+vias/−generated, per-net length before→after, board vias before→after, Internal DRC errors before→after (computed on a fork, in the background) | Route Review "Change preview" |
| granular review: accept/reject route, accept checked nets / all / reject batch | Route Review, Routing Jobs |
| locks: track, via, net, component (protects copper attached to its pads), region (new routes may not enter; copper inside protected); undoable | Edit ▸ Lock/Unlock Selected (L), Lock Region…, Unlock All |
| per-net constraints: width (checked against the hard minimum), allowed layers, max vias, priority, preserve existing, preferred corridor, avoid region | Router ▸ Net Routing Constraints… |
| corridors: global prefer/avoid soft cost fields (not keepouts) | Router ▸ Add Routing Corridor… |
| tweak: shorten, reduce vias/bends, increase clearance, merge collinear, try alternative | Router ▸ Optimize Selected Net; Route Review |
| local reroute of a generated section between junctions (pads, vias, branches) — preview, then one validated, undoable commit | Router ▸ Reroute Selected Section (Shift+R) |
| heat maps: congestion, occupancy grid, inflated obstacles, clearance envelope, failed-search explored cells | View ▸ Debug Overlays; Router ▸ Search Debug |
| search playback (off by default) | Router ▸ Search Debug ▸ Play Search Exploration |
| job panel: net, pass, index/total, nodes, rip-ups, Pause/Resume/Cancel | Routing Jobs |
| route inspector: net, width, layer, lengths, vias, bends, clearance margin, provenance, lock state, rule sources, router score | Inspector ("Route" section) |
| "why did this route use a via?": the router is asked for a zero-via route on a fork; the answer is a deterministic fact (also handed to the AI as a FACT line) | Router ▸ Explain Route Vias |

Shortcuts: R route selected net · Shift+R reroute section · Ctrl+Shift+R route
board · L lock/unlock · F fit · G grid · Ctrl+Z / Ctrl+Shift+Z undo/redo · Ctrl+Shift+D
Internal Geometry Check.
