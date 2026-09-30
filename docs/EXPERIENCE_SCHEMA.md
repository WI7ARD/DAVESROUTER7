# Experience log schema (`pcbrouter-experience/1` and `/2`)

Field-by-field reference for the routing experience log (learning level 1, see
`docs/LEARNING.md`). The writer is
`pcbrouter.learning.experience.records_from_result`; the readers are
`ExperienceLog.read`, the trainer (`pcbrouter.learning.trainer`) and
`pcbrouter.learning.policy.attempts`.

## Files

The log lives in one folder: the user data folder's `experience/`
(`learning.experience.default_dir()`), or `benchmarks/real100/work/experience/`
for Real100 runs.

| File | Contents |
|---|---|
| `experience.jsonl` | the current log. One JSON object per line (UTF-8, keys sorted, no spaces). Appended after every finished board-routing job, one line per planned net. |
| `experience.jsonl.1` | the previous log. When an append would take `experience.jsonl` past 50 MB (`MAX_BYTES`), the current file is renamed to `.1`, replacing the old `.1`. |
| `experience.keep.jsonl` | stratified retention. Before an old `.1` is replaced, its records are merged with this file, keeping the newest 100 records (`KEEP_PER_STRATUM`) per stratum. A stratum is `settings.mode` + `net_f.bucket` (or `net_f.kind` when a record has no bucket). If the result is larger than half the cap (25 MB), the per-stratum count is halved until it fits (down to 1). Without this, rotation would drop rare kinds of nets (diff pairs, large power nets) first. |
| `salt` | a random per-installation salt, 32 hex characters, created on first use. Used only to hash board fingerprints. |

The three log files together stay under about 125 MB. `ExperienceLog.read`
yields records from `keep`, then `.1`, then the current file (oldest first). It
skips blank lines and lines that are not valid JSON (for example a line torn by
a crash).

## Privacy

- Nothing is sent anywhere. The log is written only on the local disk.
- Net names, reference designators, coordinates and file paths are never
  stored. Net and board features are counts, lengths, ratios and settings.
- The board is identified by `board`, the first 16 hex characters of
  `sha256("<salt>:<board fingerprint>")`. The fingerprint is a SHA-256 of the
  board's domain state (`pcbrouter.domain.fingerprint`). Records of one board
  group together, but the id cannot be matched to a board without the salt, and
  the same board gives different ids on different installations. Any edit to
  the board gives a new id.
- `job.policy_reason` is free text written by the policy selector. It can
  contain board-profile numbers (for example `area_cm2=12`), never names.

## Record

One record per planned net of a finished job. Nets without an outcome are
skipped. **Introduced** gives the schema that first wrote a field; "1 (later)"
and "2 (later)" mean the field was added while that schema was current (see
*Compatibility*). A reader must treat every field as possibly absent.

### Top level

| Key | Type | Introduced | Meaning |
|---|---|---|---|
| `schema` | string | 1 | `"pcbrouter-experience/1"` or `"pcbrouter-experience/2"`. The current writer writes `/2`. |
| `app` | string | 1 | `pcbrouter.__version__` of the app that wrote the record, e.g. `"1.1.1"`. The trainer filters on it. |
| `router` | object | 2 (later) | `learning.policy.router_signature()`: `{"app": str, "geometry": str, "rules": str}`, the app version, `GEOMETRY_ENGINE_VERSION` and `RULE_ENGINE_VERSION` that produced the outcome. |
| `source` | string | 1 | Who routed: `"app"` (desktop app job), `"cli"` (`pcbrouter --route`), `"real100"` (benchmark harness). Test fixtures use other values (e.g. `"fixture"`). |
| `board` | string | 1 | Salted board hash, 16 hex characters (see *Privacy*). |
| `board_status` | string | 1 | Job result, a `BoardStatus` value: `FULLY_ROUTED`, `PARTIALLY_ROUTED`, `FAILED`, `CANCELLED`, `NOTHING_TO_ROUTE`. |
| `board_f` | object | 1 | Board statistics (below). |
| `board_p` | object | 2 | Board profile (below). `{}` when it could not be computed. |
| `job` | object | 2 | Board-level results and the policy decision (below). The same object in every record of a job. |
| `net_f` | object | 1 | Net features (below). |
| `settings` | object | 1 | The job's search settings (below). |
| `outcome` | object | 1 | What happened to this net (below). |

### `board_f` (board statistics)

| Key | Type | Unit | Meaning |
|---|---|---|---|
| `copper_layers` | int | | copper layer count |
| `pads` | int | | pads on the board |
| `nets` | int | | nets on the board |
| `footprints` | int | | footprints |
| `zones` | int | | zones (copper and keepout) |
| `width_mm`, `height_mm` | float or null | mm, 1 dp | board bounding box; null when unknown |

### `board_p` (board profile, schema 2)

`learning.features.board_profile(board, plan.tasks)`, every value a float
rounded to 4 decimals. "Nets" here means the nets the job planned to route.
The definitions belong to `learning.features.PROFILE_VERSION` (currently 1).

| Key | Unit | Meaning |
|---|---|---|
| `copper_layers` | | copper layer count |
| `signal_layers` | | routable layers: copper layers minus plane layers, at least 1 |
| `plane_layers` | | copper layers more than 60 % covered by non-keepout zones (by area, against the bounding box) |
| `area_cm2` | cm² | bounding-box area |
| `pads` | | pads on the board |
| `nets_to_route` | | planned nets |
| `pads_per_net` | | mean pads per planned net |
| `multi_pad_frac` | 0–1 | share of planned nets with 4 or more pads |
| `pad_density_cm2` | 1/cm² | pads per cm² |
| `pitch_p10_mm`, `pitch_p50_mm` | mm | 10th and 50th percentile of the nearest-neighbour distance between pad centres (fine pitch) |
| `airwire_total_mm`, `airwire_mean_mm` | mm | total and mean airwire length of the planned nets |
| `long_net_frac` | 0–1 | share of planned nets whose airwire is longer than a quarter of the board diagonal |
| `demand` | 1/mm | routing demand: total airwire length / (board area in mm² × signal layers) |
| `low_escape_frac` | 0–1 | share of planned nets with `escape_options` ≤ 2 |
| `congestion_mean` | 0–1 | mean `net_f.congestion` |
| `existing_tracks`, `existing_vias` | | tracks and vias already on the board |
| `zones` | | zone count |
| `diff_pair_frac` | 0–1 | share of planned nets that are diff-pair tasks |

### `job` (board-level results, schema 2)

| Key | Type | Meaning |
|---|---|---|
| `completed` | int | nets completed |
| `attempted` | int | nets attempted (planned) |
| `ripups` | int | rip-ups in the job |
| `runtime_s` | float, s, 2 dp | job runtime |
| `new_vias` | int | vias the job added |
| `policy_decision` | string | `NONE` (no policy configured: the fixed router), `POLICY` (a non-selective policy ran, e.g. `random:SEED`), `LEARNED` (the selector let a learned policy run), `FALLBACK_FIXED` (a policy was configured but the fixed router ran) |
| `policy_reason` | string or null | why, for `FALLBACK_FIXED` (out of distribution, no A/B evidence, trained on another router, no policy for this mode, selection error) |
| `policy_id` | string or null | content hash of the policy (`pol-…`), when known |

### `net_f` (net features)

| Key | Type | Unit | Introduced | Meaning |
|---|---|---|---|---|
| `kind` | string | | 1 | `signal`, `power`, `diff_pair` or `group` |
| `pads` | int | | 1 | pads on the net |
| `airwire_mm` | float | mm, 2 dp | 1 | sum of the net's airwire lengths |
| `escape_options` | int | | 1 | fewest free escape directions (summed over layers) among the net's first 4 pads; 99 = unknown |
| `congestion` | float | 0–1, 3 dp | 1 | highest copper density near the net's first 8 pads (advisory) |
| `priority` | int | | 1 | explicit user/AI priority, higher first |
| `order` | int | | 1 | position in the routing plan (0 = first) |
| `order_frac` | float | 0–1, 3 dp | 1 | `order / (planned nets − 1)` |
| `paired` | bool | | 1 | the task belongs to a group or diff pair |
| `bucket` | string | | 1 (later) | the context label at write time, for diagnostics and retention strata only. Its format has changed: `kind\|pN\|eN\|lN` with `l` from copper layers (early `/1`), `l` from routable layers (`/2`), then with a density band `\|dN` added. **Learners never read it**; contexts are re-derived from raw fields (see *Compatibility*). |

### `settings` (the job's search settings)

| Key | Type | Unit | Meaning |
|---|---|---|---|
| `mode` | string | | `"speed"` when the base heuristic weight is above 1.0, else `"accuracy"` |
| `grid_mm` | float | mm, 3 dp | base grid pitch |
| `heuristic_weight` | float | 3 dp | base A* heuristic weight (1.0 = optimal, Speed uses 1.5) |
| `coarse_factor` | int | | coarse-to-fine factor (0 = off) |
| `max_passes` | int | | routing passes |
| `allow_ripup` | bool | | rip-up allowed |
| `budget_s` | float | s, 1 dp | job time budget |
| `parallel_workers` | int | | worker processes (0 = in process) |
| `strategy` | string | | net ordering: `most_constrained`, `shortest_first`, `critical_first`, `fewest_escapes`, `congestion_aware` |

### `outcome` (what happened to this net)

| Key | Type | Unit | Introduced | Meaning |
|---|---|---|---|---|
| `status` | string | | 1 | final `RouteStatus`: `SUCCESS`, `PARTIAL`, `NO_ROUTE`, `TIMEOUT`, `CANCELLED`, `INVALID_REQUEST`, `RULE_UNKNOWN`, `ALREADY_CONNECTED`, `INTERNAL_ERROR` |
| `reason` | string or null | | 1 | final `FailureReason`: `NO_ESCAPE`, `NO_PATH`, `VIA_LIMIT`, `LAYER_RESTRICTION`, `CONGESTION`, `RULE_UNKNOWN`, `TIMEOUT`, `VALIDATION`; null on success |
| `vias` | int | | 1 | vias in the net's route |
| `length_mm` | float | mm, 2 dp | 1 | routed length (0 when not routed) |
| `passes` | int | | 1 | last pass the net was tried in |
| `attempts` | int | | 1 | searches run for the net over the whole job |
| `expanded_nodes` | int | | 1 | search states expanded over all attempts |
| `route_s` | float | s, 3 dp | 1 | search seconds over all attempts |
| `ripped` | bool | | 1 | the net's final route required removing other router-generated copper |
| `arms` | list of string | | 1 (later) | the search variant of each attempt, in order: `preset`, `no_coarse`, `finer_grid`, `greedier`, `fewer_vias`. Empty when no policy ran. Since learning v3 an arm that did not change the request (e.g. `greedier` on Speed) is recorded as `preset`. |
| `policy` | string | | 1 (later) | the policy that chose the arms: `fixed` (also after `FALLBACK_FIXED`), `random`, `thompson` |
| `trace` | list of object | | 2 | one entry per attempt (below) |

#### `outcome.trace[]` (one entry per attempt, schema 2)

Written by `BoardRouter._task_request` when the search is dispatched (the
settings actually used, after the arm and any diff-pair changes) and completed
by `_apply_result` when its result comes back. Floats are rounded to 4 decimals.

| Key | Type | Unit | Meaning |
|---|---|---|---|
| `pass` | int | | routing pass |
| `arm` | string or null | | the arm of this attempt; null when no policy ran |
| `grid_mm` | float | mm | grid pitch |
| `heuristic_weight` | float | | A* heuristic weight. Records made before the v3 cap show 1.875 for `greedier` on Speed; since v3 it is at most 1.5. |
| `coarse_factor` | int | | coarse-to-fine factor |
| `minimize_vias` | bool | | minimise vias |
| `via_cost_mm` | float | mm | via cost in path length |
| `wrong_way_factor` | float | | cost factor for off-preferred-direction segments |
| `node_limit` | int or null | | search state limit |
| `slice_s` | float or null | s | time limit for this search |
| `status` | string | | result `RouteStatus` |
| `reason` | string or null | | result `FailureReason` |
| `expanded_nodes` | int | | states expanded by this attempt |
| `route_s` | float | s | seconds of this attempt (the arm's cost in training) |

The last four keys are absent when no result was recorded for the attempt.

## Compatibility rules

1. **Add only.** A field is never removed or renamed, and its type does not
   change. New fields may be added while a schema is current; that is what
   "(later)" marks above.
2. **A meaning change bumps the schema.** Changing what an existing field
   measures (unit, definition, the `board_p` feature definitions behind
   `PROFILE_VERSION`) requires a new `pcbrouter-experience/N`. Adding a new
   `board_p` key is an addition.
3. **Readers accept 1 and 2.** They use `.get` with defaults and treat any
   field as possibly missing. A schema-1 record has no `board_p`, `job`,
   `router` or `trace`.
4. **Contexts are derived at train time.** `learning.policy.attempts` builds
   each attempt's context from raw fields (`net_f.kind`, `pads`,
   `escape_options`, routable layers from `board_p.signal_layers` or else
   `board_f.copper_layers`, `board_p.demand`, and the previous trace entry's
   `reason`), never from the stored `bucket`. Old logs stay trainable when the
   context definition changes. A record without the needed field falls into an
   "unknown" band: `d?` without `board_p`, `retry:unknown` without `trace`.
   The router always knows the board's demand, so `d?` contexts never match a
   live decision; `retry:unknown` matches only retries whose previous failure
   had no reason.

**Golden fixtures.** `tests/fixtures/experience/v1_sample.jsonl` and
`v2_sample.jsonl` are real-format records of each schema. A unit test pins
them: the current readers and trainer must still accept both files. A change
that breaks that test breaks users' existing logs. Do not edit the fixtures to
make it pass.
