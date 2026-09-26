# Internal Geometry Check (not KiCad DRC)

Tools ▸ Run Internal Geometry Check (Ctrl+Shift+D) runs `drc.run_geometry_check`
on a background thread. It is the router's own check of the copper it understands.
**It is not KiCad DRC and does not cover every KiCad rule** — run KiCad DRC before
manufacturing.

Checks: copper-to-copper clearance and shorts (pad pairs inside one footprint are
skipped, as KiCad does), zone-fill clearance (WARNING — fills are refillable),
track width min/max, via diameter/drill/annular ring, board edge crossing and edge
clearance, cutouts, keepouts (per permission), hole clearance and hole-to-hole,
custom `disallow` rules, unconnected items (INFO), unsupported rules (INFO/WARNING).

Checks skipped because a rule is unknown are reported as WARNING `RULE_UNKNOWN`, so a
board with unknown rules never shows PASS.

Result: `PASS` / `WARNINGS` / `FAIL`, error/warning/info counts, number of checks,
duration. Each violation: stable id, kind, severity, objects, layer, location,
actual and required value, rule source, geometry accuracy.

UI: the *Internal Geometry Check* dock (filters: errors / warnings / info, layer,
net; clicking a row zooms to and highlights the objects), markers on the canvas, and
the status field `Internal DRC: Pass / Warnings / Fail`.
