"""Electrical connectivity from actual copper geometry.

Per net, every copper item (pad, track, via, zone fill) is a node; two same-net
items are joined when their copper *touches* on a layer they share (gap <= 1 nm,
:func:`~pcbrouter.geometry.clearance.touches`). That covers T-junctions, crossings,
track-into-pad, overlapping traces and zone fills; a via joins every layer it spans.
Two tracks crossing on different layers without a via are *not* connected.

A net is "fully connected" when all of its pads end up in one group. "Having
tracks" is never taken to mean "routed".

Airwires (unrouted connection requirements) are a minimum spanning tree between
pad groups, using the closest pad pair of each two groups (deterministic
tie-breaking by id).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from pcbrouter.domain.geometry import Point
from pcbrouter.domain.units import Nm
from pcbrouter.geometry.board import BoardGeometry, CopperItem, ItemKind
from pcbrouter.geometry.clearance import GEOMETRY_TOLERANCE_NM, touches
from pcbrouter.geometry.distance import PointCore, PolygonCore, SegmentCore


class NetStatus(Enum):
    FULLY_CONNECTED = "fully connected"
    #: Not complete, but some routing copper already touches its pads.
    PARTIALLY_CONNECTED = "partially connected"
    #: No routing copper touches any of its pads.
    UNROUTED = "unrouted"
    NOT_APPLICABLE = "n/a (fewer than 2 pads)"


@dataclass(frozen=True, slots=True)
class Airwire:
    net: str
    from_uid: str
    to_uid: str
    start: Point
    end: Point

    @property
    def length(self) -> float:
        return self.start.distance_to(self.end)

    @property
    def midpoint(self) -> Point:
        return Point((self.start.x + self.end.x) // 2, (self.start.y + self.end.y) // 2)


@dataclass
class NetConnectivity:
    net: str
    pad_uids: list[str]
    #: Pad groups: each is the list of pad uids electrically joined together.
    groups: list[list[str]]
    #: Copper islands that contain no pad (dangling tracks, floating vias).
    islands: list[list[str]]
    edges: list[tuple[str, str, str]]  # (uid_a, uid_b, layer) physical contacts
    airwires: list[Airwire] = field(default_factory=list)
    routed_length: Nm = 0
    via_count: int = 0
    #: Some track/via/zone copper touches at least one pad group.
    attached_copper: bool = False

    @property
    def pad_count(self) -> int:
        return len(self.pad_uids)

    @property
    def status(self) -> NetStatus:
        if self.pad_count < 2:
            return NetStatus.NOT_APPLICABLE
        if len(self.groups) <= 1:
            return NetStatus.FULLY_CONNECTED
        if len(self.groups) == self.pad_count and not self.attached_copper:
            return NetStatus.UNROUTED
        return NetStatus.PARTIALLY_CONNECTED

    @property
    def is_fully_connected(self) -> bool:
        return self.status in (NetStatus.FULLY_CONNECTED, NetStatus.NOT_APPLICABLE)

    @property
    def remaining_connections(self) -> int:
        return max(0, len(self.groups) - 1)


@dataclass
class BoardConnectivity:
    nets: dict[str, NetConnectivity]
    elapsed_s: float = 0.0

    def net(self, name: str) -> NetConnectivity | None:
        return self.nets.get(name)

    def is_net_fully_connected(self, net: str) -> bool:
        info = self.nets.get(net)
        return info is None or info.is_fully_connected

    def unconnected_components(self, net: str) -> list[list[str]]:
        info = self.nets.get(net)
        return [] if info is None or info.is_fully_connected else info.groups

    def estimated_remaining_connections(self, net: str) -> int:
        info = self.nets.get(net)
        return 0 if info is None else info.remaining_connections

    @property
    def airwires(self) -> list[Airwire]:
        return [a for info in self.nets.values() for a in info.airwires]

    def metrics(self) -> dict[str, int]:
        counts = {s: 0 for s in NetStatus}
        for info in self.nets.values():
            counts[info.status] += 1
        return {
            "nets_with_2plus_pads": sum(
                counts[s] for s in NetStatus if s is not NetStatus.NOT_APPLICABLE
            ),
            "fully_connected": counts[NetStatus.FULLY_CONNECTED],
            "partially_connected": counts[NetStatus.PARTIALLY_CONNECTED],
            "unrouted": counts[NetStatus.UNROUTED],
            "remaining_connections": sum(i.remaining_connections for i in self.nets.values()),
        }


class _UnionFind:
    def __init__(self, items: list[str]) -> None:
        self.parent = {i: i for i in items}

    def find(self, x: str) -> str:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Deterministic: the lexicographically smaller root wins.
            if rb < ra:
                ra, rb = rb, ra
            self.parent[rb] = ra


def _center(item: CopperItem) -> Point:
    core = item.shapes[0].core
    if isinstance(core, PointCore):
        return core.p
    if isinstance(core, SegmentCore):
        return Point((core.a.x + core.b.x) // 2, (core.a.y + core.b.y) // 2)
    assert isinstance(core, PolygonCore)
    return core.polygon.bounds.center


def _net_connectivity(geo: BoardGeometry, net: str, uids: list[str]) -> NetConnectivity:
    items = [geo.copper[u] for u in uids]
    uf = _UnionFind(uids)
    edges: list[tuple[str, str, str]] = []
    member = set(uids)
    for item in items:
        for layer in sorted(item.layers):
            probe = item.bounds.expanded(GEOMETRY_TOLERANCE_NM)
            for other in geo.copper_near(layer, probe):
                if other.uid <= item.uid or other.uid not in member:
                    continue
                if uf.find(item.uid) == uf.find(other.uid):
                    continue
                if any(touches(a, b) for a in item.shapes for b in other.shapes):
                    uf.union(item.uid, other.uid)
                    edges.append((item.uid, other.uid, layer))
    pads = sorted(i.uid for i in items if i.kind is ItemKind.PAD)
    comps: dict[str, list[str]] = {}
    for uid in uids:
        comps.setdefault(uf.find(uid), []).append(uid)
    groups: list[list[str]] = []
    islands: list[list[str]] = []
    attached = False
    for members in comps.values():
        pad_members = sorted(u for u in members if geo.copper[u].kind is ItemKind.PAD)
        (groups if pad_members else islands).append(pad_members or sorted(members))
        if pad_members and len(pad_members) < len(members):
            attached = True
    groups.sort()
    islands.sort()
    info = NetConnectivity(
        net=net,
        pad_uids=pads,
        groups=groups,
        islands=islands,
        edges=edges,
        routed_length=sum(
            geo.board.index.tracks_by_id[i.source_id].length
            for i in items
            if i.kind is ItemKind.TRACK
        ),
        via_count=sum(1 for i in items if i.kind is ItemKind.VIA),
        attached_copper=attached,
    )
    info.airwires = _airwires(geo, net, groups)
    return info


def _airwires(geo: BoardGeometry, net: str, groups: list[list[str]]) -> list[Airwire]:
    """Prim's MST over pad groups (closest pad pair per group pair)."""
    if len(groups) < 2:
        return []
    centers = [[(uid, _center(geo.copper[uid])) for uid in g] for g in groups]

    def closest(a: int, b: int) -> tuple[float, str, str, int, Point, Point]:
        best: tuple[float, str, str, int, Point, Point] | None = None
        for ua, pa in centers[a]:
            for ub, pb in centers[b]:
                cand = (pa.distance_to(pb), ua, ub, b, pa, pb)
                if best is None or cand[:3] < best[:3]:
                    best = cand
        assert best is not None
        return best

    in_tree = {0}
    wires: list[Airwire] = []
    while len(in_tree) < len(groups):
        options = [
            closest(a, b) for a in sorted(in_tree) for b in range(len(groups)) if b not in in_tree
        ]
        _d, ua, ub, b_index, pa, pb = min(options, key=lambda o: o[:3])
        in_tree.add(b_index)
        wires.append(Airwire(net, ua, ub, pa, pb))
    return wires


def analyse_connectivity(geo: BoardGeometry) -> BoardConnectivity:
    t0 = time.perf_counter()
    nets: dict[str, NetConnectivity] = {}
    for net, uids in sorted(geo.by_net.items(), key=lambda kv: kv[0] or ""):
        if not net:
            continue  # no-net copper is not a connection requirement
        nets[net] = _net_connectivity(geo, net, sorted(uids))
    # Nets declared with pads but no copper at all still appear (with their pads).
    return BoardConnectivity(nets, time.perf_counter() - t0)
