# Connectivity (Stage 3)

`routing.connectivity.analyse_connectivity` builds a union-find over copper items of
each net that physically touch (gap ≤ 1 nm, same layer or through a via/THT pad).
Per net: pad groups, islands, contacts, routed length, via count, and airwires
(Prim MST between groups, shortest pad-pair distance).

Statuses: `FULLY_CONNECTED`, `PARTIALLY_CONNECTED` (some copper attached to pads but
more than one group), `UNROUTED`, `NOT_APPLICABLE` (fewer than two pads).
Zone fills count as connecting copper only as stored in the file (no refill).
Board metrics: fully/partially connected, unrouted, remaining connections.
A net is never reported connected without physical copper.
