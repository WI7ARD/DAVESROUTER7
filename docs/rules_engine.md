# Rule engine (Stage 3)

Package `pcbrouter.rules`; KiCad inputs read (read-only) by `kicad/rule_adapter.py`
from the board, `.kicad_pro` (net classes, patterns, assignments, board minimums)
and `.kicad_dru` (custom rules). SHA-256 of each file is recorded.

## Every value has a source

`ResolvedValue(value, source, possibly_stricter, notes)`. `value = None` means
**unknown** — never zero, never a guessed default.

## Precedence (what is actually implemented)

1. Custom `.kicad_dru` rules whose condition matches (later rule in the file wins;
   both A/B orders are tried for pair constraints).
2. Item-local clearance (pad/footprint override).
3. Net class (for a pair: the larger of the two classes' clearances).
4. Board minimum as a floor (board file / project).
5. Router overrides (`router_overrides.json` in the workspace, and constraints the
   user approved in the AI planner): may **tighten** or **define a missing** value,
   never weaken a known one.
6. Otherwise UNKNOWN.

Widths: the net-class width is the *preferred* width and the routing minimum unless
a custom min exists; the board/custom min is the *fabrication* minimum used by DRC.
A preferred width below the hard minimum is INVALID, never silently raised.

## Conditions supported

`A/B.NetClass`, `A/B.NetName`, `A/B.Type`, `A/B.Layer`, `==`/`!=` with wildcards,
`&&`, `||`, `!`, `hasNetclass()`. Anything else makes the rule **unsupported**.

## Unsupported rules

Recorded with name, file:line, constraint kinds and readable limits. A rule
constraining clearance/width/holes/edge/disallow is *critical*. Where it could
apply, validation reports `RULE_UNKNOWN` (or uses the rule's stated limit as a
"possibly stricter" bound). The UI shows "Unsupported deterministic rule detected".

## Conservative Rule Handling (default ON)

ON: unknown critical rules → route validation refuses (RULE_UNKNOWN is not legal).
OFF (Settings ▸ Geometry, requires confirmation): unknowns become warnings. Known
violations are INVALID in both modes.

## Snapshot

Tools ▸ Export Rules Snapshot writes `rules_snapshot.json` (board defaults, classes,
patterns, custom and unsupported rules, per-net resolved values with sources,
fingerprint, digest). No secrets.
