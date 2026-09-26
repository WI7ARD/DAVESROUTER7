# Geometry engine (Stage 3)

Package: `pcbrouter.geometry` (version `GEOMETRY_ENGINE_VERSION`), spatial index in
`pcbrouter.spatial`.

## Representation

* Units: integer nanometres everywhere (`domain.units`). Floats only for display.
* Every copper object is **core ⊕ radius**: a point (vias, round pads), a segment
  (tracks, ovals) or a polygon (rect/roundrect/chamfered/custom pads, zone fills),
  grown by a radius. Tracks are capsules of their full width — never zero-width lines.
* Inflation by a clearance is exact: add it to the radius (Minkowski sum).
* Arcs (tracks, outlines, custom-pad primitives) become chords plus the chord
  sagitta (≤ `ARC_MAX_SAGITTA_NM` = 1 µm) added to the radius: the shape always
  covers the real copper → **CONSERVATIVE_APPROXIMATION**, labelled as such.
* Accuracy labels on every shape: `EXACT`, `CONSERVATIVE_APPROXIMATION`, `UNKNOWN`.

## Exactness and boundaries

Distances are compared as exact integer/rational squares (`geometry.exact`), never
with float epsilons. `GEOMETRY_TOLERANCE_NM = 1`:

| predicate | meaning |
|---|---|
| meets clearance | gap ≥ required − 1 nm (a gap exactly equal to the rule is legal) |
| touches | gap ≤ 1 nm (same-net connection) |
| overlaps | gap < −1 nm |

## Board model

`BoardGeometry` (built by `geometry.extract.build_board_geometry`):
copper items per layer (pads, tracks, vias, **stored** zone fills — never re-filled),
holes (plated/NPTH), keepouts (with their own track/via/pad/pour permissions),
board-edge chords, courtyards (**metadata only, not keepouts**), and the board region
(Edge.Cuts loops with even-odd nesting → outline, cutouts, islands).
`contains_routable_point` returns `None` when no closed outline exists: callers treat
that as unknown, not as inside.

## Spatial index

`SpatialIndex` interface; `GridIndex` (uniform hash, cell chosen from object sizes)
is used; `LinearIndex` is the O(N) reference used by equivalence tests.
`LayeredIndex` keeps one index per copper layer. Collision queries never scan the
whole board. Measured (tools/bench_geometry.py, 11k copper objects): local segment
check ≈ 0.08 ms, index build ≈ 33 ms.

## Deviations from the spec suggestion

Extraction lives in `geometry/extract.py` (not `kicad/geometry_adapter.py`); there is
no quadtree (grid + linear reference instead). Shapely was evaluated and not used:
float-only coordinates would break the exact-integer boundary semantics, and it
adds a GEOS binary to packaging.
