# KiCad compatibility matrix

Generated from `src/pcbrouter/rules/compat.json` by `tools/compat_matrix.py`;
`tests/unit/test_compat_matrix.py` keeps it consistent with the rule engine.
Reference: KiCad source fec63a6 (rule semantics); KiCad 8.0.8 and 9.0.3 DRC (oracle).

| State | Meaning |
|---|---|
| SUPPORTED | parsed, evaluated exactly as KiCad does, respected by routing; verified against KiCad DRC |
| PARTIAL | evaluated where the needed facts exist; elsewhere treated as possibly applying (routing keeps the stricter value, never guesses) |
| UNSUPPORTED | not evaluated; a critical rule refuses or bounds affected nets, a non-critical one is reported |
| UNKNOWN | behaviour not yet established |

## Propertys

| Feature | Status | Notes | Evidence |
|---|---|---|---|
| `A.NetName` | **SUPPORTED** | case-insensitive; wildcard only in a right-hand literal; '[' literal | tests/unit/test_rule_fidelity.py |
| `A.NetClass` | **SUPPORTED** | case-insensitive string compare; Net_Class alias | tests/unit/test_rule_fidelity.py |
| `A.Type` | **SUPPORTED** | case-insensitive ('Track' == 'track') | tests/unit/test_rule_fidelity.py |
| `A.Layer` | **SUPPORTED** | layer-name wildcards | tests/unit/test_stage3_engine.py |
| `A.Net` | **SUPPORTED** | net code identity; only A.Net vs B.Net (a numeric literal is refused); unknown for generic holes | tests/unit/test_rule_fidelity.py |
| `A.Pad_Type / A.Pad_Shape` | **SUPPORTED** | pad-only (undefined, so false, elsewhere); existing pads carry their KiCad type/shape names | tests/unit/test_rule_fidelity.py |
| `A.Name` | **PARTIAL** | zone-only property: false for tracks/vias/pads (exact); zone names not tracked | tests/unit/test_partial_conditions.py |

## Functions

| Feature | Status | Notes | Evidence |
|---|---|---|---|
| `A.hasNetclass()` | **SUPPORTED** | wxString::Matches semantics (case-sensitive, * and ?) | tests/unit/test_rule_fidelity.py |
| `A.isPlated()` | **PARTIAL** | vias true, PTH pads true, other pads and tracks false (exact); a generic hole in hole-clearance queries has unknown plating and bounds conservatively | tests/unit/test_rule_fidelity.py |
| `A.existsOnLayer()` | **SUPPORTED** | layer set of tracks, graphics and existing pads; layer-name wildcards | tests/unit/test_rule_fidelity.py |
| `A.inDiffPair()` | **SUPPORTED** | MatchDpSuffix port: P/N or +/- suffix, partner net must exist, base before trailing '_' also matches | tests/unit/test_rule_fidelity.py |
| `A.insideCourtyard() / intersectsCourtyard() / insideFrontCourtyard() / intersectsFrontCourtyard() / insideBackCourtyard() / intersectsBackCourtyard()` | **PARTIAL** | KiCad: insideX = intersectsX (deprecated alias); exact for existing objects (pads, tracks, vias: copper touching the footprint's courtyard, boundary +-10 um unknown); the item being routed has no fixed position, so a rule needing its location bounds conservatively - relaxations still apply through the existing object of the pair | tests/unit/test_rule_fidelity.py |
| `A.memberOfFootprint()` | **SUPPORTED** | parent footprint by reference or lib-id selector; tracks and vias are never members | tests/unit/test_rule_fidelity.py |
| `A.insideArea() / intersectsArea() / intersectsKeepout() / enclosedByArea()` | **PARTIAL** | named zones (any kind) on a common layer, matched by name or uuid; exact for existing objects (boundary +-10 um unknown); the routed item's location is unknown and bounds conservatively | tests/unit/test_rule_fidelity.py |
| `A.fromTo()` | **UNSUPPORTED** | as above |  |

## Syntax

| Feature | Status | Notes | Evidence |
|---|---|---|---|
| `=~ (regex compare)` | **SUPPORTED** | not KiCad syntax: KiCad 8.0.8 and 9.0.3 cannot compile the condition and skip the rule; the rule is reported as ignored-by-KiCad and never applied (applying it could relax a clearance KiCad enforces) | tests/unit/test_rule_fidelity.py |

## Constraints

| Feature | Status | Notes | Evidence |
|---|---|---|---|
| `clearance` | **SUPPORTED** |  | oracle (Real100 subset, KiCad 8.0.8) |
| `track_width` | **SUPPORTED** |  |  |
| `via_diameter / hole_size / annular_width` | **SUPPORTED** |  |  |
| `hole_clearance / hole_to_hole` | **SUPPORTED** |  |  |
| `edge_clearance` | **SUPPORTED** |  |  |
| `disallow` | **SUPPORTED** |  |  |
| `diff_pair_gap / diff_pair_uncoupled` | **PARTIAL** | pair nets are routed independently, so explicit rules KiCad would flag (gap max, uncoupled max, gap min above the clearance; severity error) refuse those nets with UNSUPPORTED_RULE naming the rule; net-class diff-pair gaps (implicit, minimum only) are met by clearance. Coupled routing: not implemented. | tests/unit/test_diff_pair_refusal.py |
| `length / skew` | **UNSUPPORTED** | reported, not enforced |  |
| `connection_width (min_connection)` | **UNSUPPORTED** | KiCad flags copper necks below the minimum connection width (K035): not modelled |  |
| `silk_clearance / courtyard_clearance / text_*` | **UNSUPPORTED** | non-copper; not affected by routing |  |

## Board features

| Feature | Status | Notes | Evidence |
|---|---|---|---|
| `copper text and graphics` | **SUPPORTED** | gr_*/fp_* and visible text on copper are no-net obstacles; text is a conservative box | tests/unit/test_copper_graphics.py, tests/unit/test_rule_fidelity.py |
| `zone fills` | **PARTIAL** | foreign fills are treated as refillable (KiCad workflow: refill after routing); a refill can split a pour or starve thermals (oracle: completion disagreements, starved_thermal) |  |
| `board file formats` | **PARTIAL** | load: KiCad 5-10; export: 20211014-20261231 (KiCad 6-10); KiCad 5.99 dev formats are refused with a precise message |  |
