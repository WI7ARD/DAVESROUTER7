# Routing pipeline (as implemented)

This traces **Router ▸ Route Board** from the click to the saved file, using the real
functions in this repository. Paths are under `src/pcbrouter/`.

## Threads and processes

| Where | Runs |
|---|---|
| **GUI thread** (Qt main thread) | menus, canvas, panels, overlay, accepting a result, starting jobs, draining worker messages (a 33 ms `QTimer`, non-blocking) |
| **Routing worker process** (one persistent `multiprocessing` spawn child) | board snapshot → routing, rip-up, optimisation, validation, export, GPU checks; progress/heartbeat/log/result messages go back through a queue |
| **GUI-process helper threads** (`ui/workers.py` `JobRunner`) | Internal Geometry Check (`BoardEngine.run_drc`), engine refresh, setup dialogs |

The GUI never calls `join`, `wait` or `result` on the worker. Cancel is a shared
job id the worker polls; after `CANCEL_GRACE_S` (5 s) the worker is terminated.
Watchdogs are the heartbeat (20 s), the start timeout (60 s), the job timeout
(board budget + grace), and the worker's exit code.

## Stages

| # | Stage | Function (file) | Thread | In → out | Can block / fail / loop? |
|---|---|---|---|---|---|
| 1 | Button | `RoutingController.route_board` (`ui/routing_controller.py`) → `board_settings()` applies the Speed/Accuracy preset (`routing/presets.py`) | GUI | working board → `RouteBoardJob(WorkingSnapshot, settings, timeout)` | Fast. Refuses a second job while one runs (`RouteJobController.busy`); the action is disabled while busy |
| 2 | Submit | `RouteJobController.submit` → `WorkerHost.submit` (`ui/route_jobs.py`) | GUI | pickled job → worker inbox | Non-blocking. Worker start failure is retried, then falls back to `InProcessHost` with a notice |
| 3 | Overlay | `RoutingOverlay.show_starting` / `update_progress` (`ui/routing_overlay.py`) | GUI | `RouteProgress` → spinner, phase, net n/N, pass, backend, elapsed, Cancel | Spinner animates on its own timer; indeterminate when no count exists |
| 4 | Worker loop | `worker_main` (`jobs/worker.py`) → `run_job` → `_route_board` (`jobs/execute.py`) | worker | job → `JobDone(value, status, error, traceback, backend)` | Exceptions are caught and returned with the traceback, never swallowed |
| 5 | Board prep | `working_from` (`jobs/execute.py`): rebuild `WorkingBoard` + `BoardEngine` (geometry extraction, rules) | worker | snapshot → working board | Big boards: seconds. Cancel is checked between steps |
| 6 | Plan | `make_plan` / `order_tasks` (`routing/board_router.py`) | worker | incomplete nets → ordered `RouteTask`s (priority, kind, congestion, length) | Bounded (≤ 8 pads sampled per net) |
| 7 | Passes | `BoardRouter.run` → `_route_task` → `_ripup_pass` | worker | tasks → fork commits | Bounded by `max_passes`, rip-ups per net / total, and `budget_s`. Budget exhaustion → FAILED/PARTIALLY_ROUTED with per-net "not attempted" reasons (never CANCELLED); user cancel → CANCELLED |
| 8 | Net request | `Router.route_net` → `normalise` (`routing/request.py`) | worker | net + rules → width, layers, via size | Missing rules → RULE_UNKNOWN (no guessed values) |
| 9 | Grid | `compile_grid` (`routing/search/grid.py`) ← `BoardEngine.occupancy` (`routing/occupancy.py`) ← `geometry/raster.py` | worker | copper, keepouts, edge, clearances → `SearchGrid` (passable, near, via_ok per layer) | Cancel points per layer; a polygon fast path for big pours; grid cache across passes |
| 10 | Search | `Router._attempt` → `astar.search` (`routing/search/astar.py`) | worker | sources/targets/cost → path | Coarse-to-fine: coarse search, corridor fine search, wider corridor, full fallback. Bounded by `node_limit` and `time_limit_s`, and cancel is checked every 2048 nodes. The heuristic uses one box per pad group plus layer-direction bounds (admissible). Failure → structured `ROUTE_FAILED` (`RouteResult.failure_report`) |
| 11 | Geometry | `Router._geometry` (shortcut, snap, collapse) | worker | path → segments + vias | Each segment/via is checked by the exact validator; failures block cells and retry (≤ `MAX_REPAIRS`) |
| 12 | Validate | `validator.validate_route`, then `WorkingBoard.commit_proposals` on the fork | worker | proposal → committed fork copper | Illegal copper is never committed |
| 13 | Result | `BoardRoutingResult` → `JobDone` → `RouteJobController._poll` → `RoutingController._board_job_done` / `_board_done` | worker → GUI | result → Routing Jobs panel and preview overlay | The GUI only renders. A result for a board closed meanwhile is discarded |
| 14 | Accept | `accept_board` → `AcceptBoardRoutingCommand` → `WorkingBoard.commit_objects` (validated, undoable) | GUI | chosen nets → working board | ≈0.15 s for about 400 objects (measured) |
| 15 | Check | `GeometryController.run_geometry_check` → `BoardEngine.run_drc` | helper thread | working board → `DRCResult` | Result is discarded if the board changed meanwhile |
| 16 | Export | `ExportController._export` → `ExportJob` → `perform_export` (`commands/export_commands.py`) → `export_board` (`kicad/writer.py`) | worker | working board → new `.kicad_pcb` | DRC gate, source SHA check, add-only, reload self-check with identical geometry, atomic write; optional `kicad-cli` DRC. The source file is never overwritten without an explicit setting |
| 17 | Reopen | `MainWindow.open_board` → `OpenBoardCommand` → `kicad/loader.py` | **GUI** | file → board | Synchronous: measured 169 ms for the benchmark and about 1 s for a 4.4 MB board |

## Progress messages

Worker → GUI phases (`jobs/protocol.JobPhase`): STARTING, PREPARING_BOARD,
BUILDING_GRID, PLANNING, ROUTING, RIPUP_REROUTE, OPTIMIZING, VALIDATING,
RUNNING_DRC, EXPORTING, then COMPLETED / FAILED / CANCELED. Counts are real (net
n of N, pass p of P, grid layer k of K); throttled to ≤ 10 messages/s.
