# Fixes applied (stabilization pass, 2026-09-29)

Every number below was measured in this container (CPU only, 4 cores, Xvfb for the
GUI). "Before" means `origin/main` 4c73e31.

---
**ISSUE:** Routing takes minutes or fails (NODE_LIMIT / NO_PATH) on nets spread over a real board.
**ROOT CAUSE:**
- The A* heuristic measured distance to one bounding box around *all* unconnected pads per layer.
- On a spread-out net that box covers most of the board, so the heuristic was ≈0 almost everywhere and A* expanded like Dijkstra.
- On `pic_programmer` a single net expanded 1.9M nodes (42 s) and gave up, although a flood fill reached every target.

**FILES:** `routing/search/astar.py`, `routing/router.py`
**FIX:**
- One heuristic box per unconnected pad group (`SearchProblem.target_boxes`), merged to ≤16 (`merge_boxes`).
- Merged boxes contain their inputs, so the heuristic stays admissible.

**VERIFICATION:**
- `test_per_group_boxes_cut_expansions_and_keep_the_optimum`: more than 20× fewer expansions, same optimal cost.
- `pic_programmer` full board: 167.6 s → 46.8 s with identical copper (default settings, before the presets).

---
**ISSUE:** 2-layer boards with many crossings fail. On `Router_Benchmark_RevA`: Speed 21/32 nets, Accuracy 15/32 at the 600 s budget.
**ROOT CAUSE:** No preferred routing direction per layer, so early nets wall off both layers.
**FILES:** `routing/cost/model.py`, `routing/search/astar.py`, `routing/router.py`, `routing/presets.py`
**FIX:**
- New `cost.wrong_way_factor`: layers alternate H/V in stack order.
- A step against the layer's direction costs the factor; a diagonal costs half the extra.
- With factor ≥ 2 the heuristic becomes Manhattan, plus the cheaper of the wrong-way extra or two vias for same-layer targets. Both are lower bounds.
- Presets: Accuracy uses 3, Speed uses 4.

**VERIFICATION:**
- `test_direction_costs_and_group_boxes_keep_search_optimal`: every real search matches an independent pruning-free Dijkstra.
- Benchmark results are in `TEST_RESULTS.md`.

---
**ISSUE:** Search time dominated whole-board routing (91% of runtime).
**FIX:** Coarse-to-fine search (`request.coarse_factor`, ×4 in both presets):
1. A coarse search, where a coarse cell counts as free if at least half its fine cells are.
2. If that finds nothing, a retry on an optimistic coarse grid.
3. A fine search inside a corridor around the coarse route (radius 2, then radius 5).
4. The full fine search as fallback. No route can be lost.

An optimistic coarse flood fill (8-neighbour, no turn or via limits) proves "no path" without the full search.
**FILES:** `routing/search/astar.py`, `routing/request.py`, `routing/router.py`
**VERIFICATION:**
- `test_coarse_to_fine_finds_routes_and_falls_back`: the corridor route stays within 5% of optimal; a one-cell gap is still found; a walled-in source is proved impossible quickly.
- Benchmark Speed: 64 s → 19 s at the same completion.

---
**ISSUE:** A spent board budget was reported as **CANCELLED**, and nets never reached showed no reason.
**FILES:** `routing/board_router.py`
**FIX:**
- Budget exhaustion is tracked separately from a user cancel. The status becomes FAILED or PARTIALLY_ROUTED.
- Untouched nets get `TIMEOUT: not attempted: the board time budget (N s) ran out`.
- A real cancel still gives CANCELLED with "cancelled by the user".

**VERIFICATION:** `test_budget_exhaustion_is_not_a_user_cancel`.

---
**ISSUE:** Failed routes gave no usable diagnosis. Partial routes always said NO_PATH with an empty message.
**FILES:** `routing/result.py`, `routing/router.py`, `routing/board_router.py`
**FIX:**
- `RouteResult.failed_at` and `failure_report()` produce a `ROUTE_FAILED` block: net, status, reason, connections, expanded_nodes, elapsed_ms, start, goal, layers.
- It is logged as `[SEARCH] …` and summarised in the Routing Jobs text, e.g. `… between (93.81, 43.65) and (21.27, 43.65) mm [1,500,000 nodes, 11.0 s]`.
- Partial routes keep the real reason (node limit, time limit, no path).

**VERIFICATION:** `test_failed_route_reports_structured_failure`.

---
**ISSUE:** The GUI froze for 0.3–0.6 s after opening a board and after accepting routes.
**ROOT CAUSE:** The AI panel's context label rebuilt the whole AI context on the GUI thread, including board-wide congestion rasterisation, even when AI was not used (stall tracer: `board_engine.congestion ← context_builder._congestion_near ← ai_panel._update_context_label`).
**FILES:** `ai/context_builder.py`, `ai/session.py`, `ui/ai_panel.py`
**FIX:**
- Preview contexts (the label) skip congestion facts. A context that is actually sent still includes them.
- The label isn't computed while the panel is hidden; it is recomputed when the panel is shown.

**VERIFICATION:** `tools/e2e_gui_check.py`: accept stall 508 → 154 ms on the benchmark.

---
**ISSUE:** The GUI froze for 1.8 s after the Internal Geometry Check on a board with a copper pour.
**ROOT CAUSE:** Violation overlays rebuilt the outline of every involved object once per violation. That included zone fills with thousands of vertices.
**FILES:** `ui/overlays.py`
**FIX:**
- Each object is outlined once, in the colour of its worst violation.
- Outlines over 2 000 points are drawn as their bounding box.

**VERIFICATION:** e2e on `pic_programmer`: DRC-phase stall 1779 → 287 ms.

---
**ISSUE:** Live routing preview repaints were slow (dashed pen over hundreds of tracks).
**FILES:** `ui/overlays.py` (`preview_items`), `ui/routing_controller.py`
**FIX:** The live preview uses a solid pen. Final candidates keep the dashed style.

---
**ISSUE:** Cancel during "Preparing the board" took 2–3 s.
**FILES:** `jobs/execute.py`
**FIX:** Cancel points after the board build, after geometry extraction and after planning.

---
**NEW TOOL:** `tools/e2e_gui_check.py` drives the real main window:
- **Flow:** open → Route Board → cancel → reroute → accept → Internal DRC → export → reopen → close.
- **Measurements:** a 10 ms event-loop gap probe and a GUI-thread stall tracer (which Python stack or thread blocked the loop), plus progress-text sampling and screenshots.
