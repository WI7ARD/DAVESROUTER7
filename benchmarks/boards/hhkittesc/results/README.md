# hhkittesc ESC: final pass with a Power net class (v1.1.1 RC)

Davi's decision: **Power = 3.0 mm track / 0.6 mm clearance** for:

- `DC_BUS+`
- `/PHASE_A`, `/PHASE_B`, `/PHASE_C`
- `/POWER_INPUT/BAT_K1_OUT`, `/POWER_INPUT/BAT-`
- `+12V`

Vias stay at the Default 0.6 / 0.3 mm. GND stays Default because it is poured
on F.Cu, In1.Cu and B.Cu.

Files:

- `hhkittesc_power_routed.kicad_pro`: Davi's project with the added Power class
  and its 7 net patterns. Nothing else changed. Use it so KiCad's DRC checks the
  same rules.
- `hhkittesc_power_routed.kicad_pcb`: the routed board (open it with the `.kicad_pro` above).
- `hhkittesc_power_routed.png`: render. Red = F.Cu, blue = B.Cu, orange / green = inner layers.
  The circles mark the failure areas.
- `hhkittesc_power_routed.report.json`: the CLI report.

Command:

```
pcbrouter hhkittesc.kicad_pcb --route --mode accuracy --workers -1 --budget 1200
```

It ran with 4 helpers in a container on a 4-core Xeon.

## Result

| | Default class only (earlier run) | **Power 3.0 / 0.6 mm** |
|---|---|---|
| Nets routed | 93/94 | **79/94** |
| New vias | 278 | 234 |
| Route time | 548 s | 959 s |
| Rip-ups | 0 | 1 |
| New internal-check errors | 0 | **0** |

The internal check reports 31 errors on the routed board. They are the same 31
already on the unrouted board:

- 28 hole clearance;
- 3 edge clearance.

They come from footprint placement, not from routing, and the router adds none.
The export is marked "UNVERIFIED" because of them.

## Why 15 nets fail with the Power class

Nine of the 15 failures start at one pin column: **U3**, x = 96.862 mm,
y = 52.7–57.9 mm. The column has a 0.65 mm pin pitch and 0.4 mm-wide pads.

- `/PHASE_A`, `/PHASE_B`, `/PHASE_C` end on U3's phase-sense pins. A net class
  applies its width to the whole net. A 3.0 mm track with 0.6 mm clearance
  cannot enter a 0.4 mm pad whose neighbours are 0.65 mm away. The pad would
  be buried under 3 mm of copper touching the next pins. The router has no
  "neck-down" (narrow near the pin, wide elsewhere), so these connections are
  impossible by the rules, not just hard.
- `/GATE_DRIVER/BST_B`, `BST_C`, `GHA_O`, `GHB_O`, `GHC_O`, `GLA_O` and `INLC` are
  U3's neighbouring pins. They fail with only 1–60 k nodes searched: their
  escapes are boxed in by the wide phase copper and its 0.6 mm clearance.
- `GND` fails one connection next to U3 (91.1, 56.6) for the same reason.

The other failures:

- `DC_BUS+` and `/AUX_POWER/UVLO` fail at the aux-power IC (167.1, 109–110 mm),
  also fine pitch: the same width-vs-pad problem.
- `+12V` fails between (185.1, 118.0) and (188.5, 122.0), a tight spot at 3 mm.
- `/POWER_INPUT/BAT-` fails a 70 mm connection from (148, 31) to (191.6, 87.6):
  no 3 mm path exists across the board with 0.6 mm clearance (1.1 M nodes searched).

## What this means (engineering)

- **3 mm on a whole net only works when every pad on that net can take 3 mm.** The
  high-current path (MOSFETs → phase terminals, battery → bus) can; the gate
  driver's sense pins cannot. In KiCad this is normally solved by:
  - **copper pours (zones)** for the phase and bus nodes, which is the usual
    choice for ESC current paths anyway (lower resistance and inductance, and
    better heat spreading than any trace); or
  - a short, narrow **sense trace** (e.g. 0.3 mm) from the pour to the driver pin.
    Only the current-carrying part is 3 mm.
- **Practical next step:**
  1. keep the Power class for `BAT-`, `BAT_K1_OUT` and `DC_BUS+`, but draw them
     as zones;
  2. move the three phase nets back to Default, so the router can reach U3, and
     give the MOSFET → motor-terminal phase path a zone;
  3. route the rest with the router.

  The earlier Default-class run shows what the router does on the signal side:
  93/94.
- **Current capacity, for scale:** a 3 mm, 1 oz (35 µm) outer trace carries about
  6–7 A for a 10 °C rise (IPC-2221 approximation); inner layers carry about half.
  If this ESC is meant for tens of amps, even 3 mm traces are the wrong tool:
  that is a pour or a bus bar.

## Not verified here

- KiCad's own DRC (`kicad-cli` is not installed in this container). Open the
  board with the included `.kicad_pro` and run DRC in KiCad 10.
- Current/thermal numbers above are a rule-of-thumb approximation, not a
  simulation.
