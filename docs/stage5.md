# Stage 5 — full-board routing and optimisation

Version 0.5.0-stage5. Module `routing/board_router.py`, `routing/optimize.py`,
`routing/diffpair.py`; commands `RouteBoardCommand`, `AcceptBoardRoutingCommand`,
`OptimizeNetCommand`; UI: Router ▸ Route Board (Ctrl+Shift+R), *Routing Jobs* dock,
Router ▸ Optimize Selected Net.

## Plan and ordering
`make_plan` lists incomplete nets (UNROUTED / PARTIALLY_CONNECTED, not locked) and
their features: pad count, airwire length, escape options (Stage 3 pin escape,
first 4 pads), kind (differential pair by name, power by class width above the
default or name, group). Strategies: CRITICAL_FIRST (default: explicit priority,
then diff pairs, power, groups, signals, shortest first), SHORTEST_FIRST,
MOST_CONSTRAINED, FEWEST_ESCAPES, CONGESTION_AWARE. Ties break by net name; pair
partners and groups stay contiguous. User/AI priorities only reorder.

## Passes (all on a *fork* of the working board)
1. route every task (best validated candidate committed to the fork);
2. retry failures with congestion feedback (geometric congestion estimate as a
   per-cell cost) and 2× node / 1.5× time limits;
3. bounded rip-up: remove rippable copper (ROUTER_GENERATED / OPTIMIZER; USER_ACCEPTED
   only if enabled; never SOURCE_EXISTING or locked), route the failed net, restore
   every removed route that still validates, reroute the rest; kept only if the
   number of connected nets increases, otherwise rolled back. Limits: 2 rip-ups per
   net, 20 per job, 600 s budget;
4. optional optimisation.

Result: FULLY_ROUTED / PARTIALLY_ROUTED / FAILED / CANCELLED with per-net status,
reason (NO_ESCAPE, NO_PATH, VIA_LIMIT, LAYER_RESTRICTION, RULE_UNKNOWN, TIMEOUT,
VALIDATION), metrics (attempted, completed, %, length, new vias, runtime, nodes,
rip-ups, reroutes). Pause / resume / cancel between nets and inside searches.

Accepting: all nets, or a checked subset (rip-ups required by accepted nets are
included). The batch is re-validated when committed and refused if the working
board changed since the job started. One undoable history step.

## Optimisation
Reroute-based and conservative (see `optimize.py`): FEWER_VIAS, SHORTER,
FEWER_BENDS, MORE_CLEARANCE, MERGE_COLLINEAR. A change is kept only if the goal
metric strictly improves within guards (e.g. ≤ +25 % length), connectivity holds
and the commit validates; otherwise the net is rolled back exactly.

## Differential pairs (foundation — PARTIAL)
Detection by name; the second net routes with soft corridors along its partner and
the partner's layers preferred; `pair_metrics` reports skew, via difference, layers
and sampled gap. **Impedance is UNKNOWN** (no stack-up model). True coupled routing
is future work.

## Power nets
Net-class width and via size are used (fixture: VBUS 0.6 mm, 0.8/0.4 mm vias). No
current-capacity or thermal claims.

## Measured (this container, fixtures)
`router_dense` (40×30 mm, 11 nets incl. a pair and a 0.6 mm power net): all 11 routed,
9 vias, ≈17 s single-threaded Python. `router_ripup`: Y (0 vias allowed) routes only
after X is ripped up and rerouted under the F.Cu wall with 2 vias.
