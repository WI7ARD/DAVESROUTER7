# AI constraint capabilities (v1.1.1)

An AI command can only ask for what the deterministic router or rule engine
actually delivers. Each field of `RoutingConstraints` belongs to one of four
classes, and the class can depend on the operation:

| Class | Meaning |
|---|---|
| **ENFORCED** | Guaranteed: a hard limit or an exact value, checked by the rule engine and the exact validator. |
| **SOFT** | A preference. It changes search costs or the routing order. Nothing is guaranteed. |
| **ANALYSIS ONLY** | Measured after routing and reported next to the result. Routing ignores it. |
| **UNSUPPORTED** | Not implemented. The validator marks the command INVALID and it cannot run. |

The approval panel labels every constraint with its class and shows **What will
run**: plain-language lines built from the same `BoardPolicy` → `BoardRouterSettings`
that execution uses. The source of truth is `src/pcbrouter/ai/capabilities.py`;
`tests/unit/test_ai_policy.py` fails if this page or the matrix misses a schema
field.

Operations:

- **RN** = route_net
- **RG** = route_group, plus the batch of several approved commands
- **RB** = route_board
- **OPT** = optimize_net / reduce_vias
- **SC** = set_net_constraint
- **SP** = set_routing_priority

A constraint on an analysis, explanation or lock command has no effect. Such a
command gets a warning, not an error.

| Field | RN | RG | RB | OPT | SC | SP | What happens |
|---|---|---|---|---|---|---|---|
| `preferred_layers` | SOFT | SOFT | SOFT | – | – | – | Cheaper on these layers; other allowed layers stay usable |
| `forbidden_layers` | ENFORCED | ENFORCED | ENFORCED | – | ENFORCED | – | No new copper on them (SC: stored rule override) |
| `min_trace_width_mm` | ENFORCED | ENFORCED | – | – | ENFORCED | – | Exact width when no preferred width is given. Below the rule minimum: rejected. On RB it would override every net class, so it is rejected |
| `preferred_trace_width_mm` | ENFORCED | ENFORCED | – | – | ENFORCED | – | Exact routed width (same limits) |
| `max_trace_width_mm` | ENFORCED* | ENFORCED* | – | – | ENFORCED* | – | *Only together with a min/preferred width: the requested width must not exceed it. Alone it is unsupported |
| `min_clearance_mm` | – | – | – | SOFT | ENFORCED | – | Routing uses the board's clearance rules. SC stores a tighten-only override. OPT uses it as the "more clearance" goal |
| `max_vias` | ENFORCED | ENFORCED | ENFORCED | – | ENFORCED | – | Hard via limit per net |
| `minimize_vias` | SOFT | SOFT | SOFT | SOFT | – | – | Vias cost 4× in the search; OPT: fewer-vias goal |
| `preferred_via_type` | ENFORCED (through) | ENFORCED (through) | ENFORCED (through) | – | ENFORCED (through) | – | Through vias only. `blind_buried` / `micro`: unsupported |
| `preserve_existing_routes` | ENFORCED | ENFORCED | ENFORCED | ENFORCED (false) | ENFORCED | ENFORCED | Default **true**: routes on the board before the job are never moved. RN only adds copper. OPT replaces only the target nets' own routes, so `true` is unsupported there |
| `allow_ripup` | ENFORCED (false) | ENFORCED | ENFORCED | ENFORCED (false) | ENFORCED (false) | ENFORCED (false) | Default **false**. RG/RB: rip-up on for the job. With `preserve_existing_routes` true, it may only move copper this job created. Speed mode never rips up |
| `allow_component_movement` | ENFORCED (false) | ENFORCED (false) | ENFORCED (false) | ENFORCED (false) | ENFORCED (false) | ENFORCED (false) | The router never moves parts. `true`: unsupported |
| `priority` | ANALYSIS ONLY | SOFT | SOFT | – | – | SOFT | Routing order: high/critical nets first. SP orders later AI board jobs |
| `criticality` | ANALYSIS ONLY | SOFT | SOFT | – | – | SOFT | As priority |
| `max_length_mm` | ANALYSIS ONLY | ANALYSIS ONLY | ANALYSIS ONLY | ANALYSIS ONLY | – | – | Routed length reported as within / EXCEEDS; no length tuning |
| `target_length_mm` | ANALYSIS ONLY | ANALYSIS ONLY | ANALYSIS ONLY | ANALYSIS ONLY | – | – | Reported as met / NOT met with the tolerance |
| `length_tolerance_mm` | ANALYSIS ONLY | ANALYSIS ONLY | ANALYSIS ONLY | ANALYSIS ONLY | – | – | Used by the target-length report |
| `avoid_nets` | – | – | – | – | – | – | Unsupported: no net-to-net avoidance |
| `avoid_net_classes` | – | – | – | – | – | – | Unsupported |
| `keep_near` | – | – | – | – | – | – | Unsupported: no attraction term |
| `keep_away_from` | – | – | – | – | – | – | Unsupported: no repulsion term |
| `differential_pair` | – | SOFT | SOFT | – | – | – | The nets are routed one after the other. The second follows a soft corridor along the first. Gap, skew and impedance are **not** controlled. On RG both nets must be targets |
| `pair_gap_mm` | – | ANALYSIS ONLY | ANALYSIS ONLY | – | – | – | Measured gap (min–mean) reported next to the target |
| `pair_skew_tolerance_mm` | – | ANALYSIS ONLY | ANALYSIS ONLY | – | – | – | Measured length skew reported as within / OUTSIDE |
| `impedance_target_ohm` | – | – | – | – | – | – | Unsupported: needs stack-up data and a field solver. It is never computed |
| `shielding_preference` | ENFORCED (none) | ENFORCED (none) | ENFORCED (none) | ENFORCED (none) | ENFORCED (none) | ENFORCED (none) | No guard traces or ground references are added. Any other value: unsupported |
| `additional_notes` | ANALYSIS ONLY | ANALYSIS ONLY | ANALYSIS ONLY | ANALYSIS ONLY | ANALYSIS ONLY | ANALYSIS ONLY | Shown to you, never interpreted |

"–" means unsupported for that operation. The validator names the field and the
reason, and the command is INVALID.

## Board-routing policy (route_group, route_board, batches)

`plan_from_command` builds one `BoardPolicy` from the approved command(s):

- rip-up is allowed only if every command in the plan allows it;
- `preserve_existing_routes` defaults to true;
- `ripup_user_accepted` is always false (AI plans never move copper you accepted);
- the Speed/Accuracy mode of the toolbar at run time;
- priorities and differential pairs;
- board-wide layer and via constraints (route_board).

`BoardPolicy.settings()` is the only place AI board jobs get their
`BoardRouterSettings`. The approval text, the "running" message and the
`ROUTER_RESULT` facts all come from `describe_settings()` of that object.

In every mode:

- copper loaded from the file and locked copper are never ripped up;
- a rip-up that would leave a displaced net disconnected is rolled back.
