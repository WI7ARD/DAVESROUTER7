# Known limitations

What DAVESROUTER does not (yet) do, or does only conservatively. Each entry
names the mechanism and the evidence. Feature-by-feature status:
[KICAD_COMPATIBILITY.md](KICAD_COMPATIBILITY.md).

"Conservative" always means the router may give up routes it could have made;
it never means accepting geometry KiCad would reject.

## Legality: KiCad DRC errors the router can still cause

Measured with the KiCad 8.0.8 DRC oracle (`benchmark_real100.py oracle`):
real KiCad DRC on the board before and after routing, with zones refilled.

| KiCad check | When it happens | Status |
|---|---|---|
| `connection_width` (board `min_connection`) | The board's minimum connection width equals the track width (Real100 K035: both 0.2 mm). Necks of 0.17–0.198 mm appear where a track meets a teardrop zone left behind by removed tracks, or where a one-cell (0.14 mm) diagonal stub joins another track. | Not modelled. K035: 7–36 errors per run. Fix planned: remove micro-stubs in the optimizer, and model `min_connection` on junctions. |
| `starved_thermal` | A refill leaves a pad with fewer thermal spokes than the zone requires. | Not modelled (the refill model counts a single spoke as a connection, as KiCad's connectivity does). |
| `solder_mask_bridge` | Board-level mask openings (gr_* on F.Mask/B.Mask) are keepouts for new copper of other nets than the one already exposed there. Pad mask apertures (`pad_to_mask_clearance`, footprint mask shapes) are not modelled. | Partly modelled. Real100 K024/K034/K074: 0 bridges after the change (were 2–5). |

## Connectivity and zones

- **Zone refill is estimated, not computed.** A stored fill crossed by new foreign
  copper is treated as stale. Its refill is estimated conservatively from the
  stored polygon (`routing/refill.py`): copper is only ever removed, necks below
  `min_thickness` are cut, and copper KiCad might *add* is not counted.
  - A pour can therefore look split when KiCad would keep it whole. The router
    then adds a track, or reports the net failed.
  - It never looks joined when KiCad would split it (0 completion disagreements
    on the measured boards).
- **Nets the job disconnects.** A route can split a pour that another net,
  connected before the job, relies on. The job re-verifies those nets and
  routes them again. When no budget is left, they are reported failed and
  counted as attempted, never silently broken.
  - Prevention (route around foreign pours) is under evaluation.
- **Stale teardrops.** Teardrop zones whose tracks were removed stay on the
  board. The router may connect to them at a new angle (see `connection_width`).

## Rules

- **Diff pairs** are routed as independent nets. A net with an explicit
  `diff_pair_gap` max or `diff_pair_uncoupled` rule that KiCad would enforce is
  refused with `UNSUPPORTED_RULE` (named rule) instead of being routed out of
  spec.
- **`length` / `skew`** constraints are reported, not enforced.
- **Location rule functions** (`insideCourtyard`, `intersectsArea`,
  `enclosedByArea`, …) are exact for existing copper. The routed track has no
  fixed location yet, so a rule that depends on its location bounds the route
  with the stricter value.
- **Routing clearances** use the stricter of KiCad's candidates. KiCad gives a
  pad or footprint local clearance precedence over everything except the board
  minimum; the router maxes it with the net class instead. This is safe, but can
  be tighter than KiCad needs. Zone-refill judgements use KiCad's exact
  precedence.
- **`=~`** is not KiCad syntax: KiCad 8 and 9 skip such rules, and so do we.

## Formats and tooling

- KiCad 10 boards (format > 20241229) load and route, but cannot be checked by the
  oracle locally: no KiCad 10 build is reachable from the test environment.
- Export is refused for a few constructs it cannot write safely (Real100 K007,
  K026, K070). The source board is never modified.

## Performance

- Large dense boards are budget-limited: Real100's biggest boards end with
  `TIMEOUT` nets at the standard budget.
- The refill model adds work on boards with many pours that routing crosses
  (Real100 K003 Speed: 9 s → 16 s). The cost is under measurement.
