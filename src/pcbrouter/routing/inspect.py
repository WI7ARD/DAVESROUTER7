"""Route inspection for the workbench (Stage 8): facts, sections, diffs, explanations.

All functions are deterministic reads (or computations on a *fork*); none commits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pcbrouter.domain.geometry import BoundingBox, Point
from pcbrouter.domain.track import Track
from pcbrouter.domain.via import Via
from pcbrouter.routing.optimize import net_metrics
from pcbrouter.routing.request import RouteRequest
from pcbrouter.routing.result import RouteStatus
from pcbrouter.routing.router import Router
from pcbrouter.routing.working_board import CommitError, WorkingBoard

EXPLAIN_NODE_LIMIT = 200_000


def route_info(wb: WorkingBoard, obj_id: str) -> dict[str, Any] | None:
    """Route Inspector facts for a selected track or via (all deterministic)."""
    idx = wb.board.index
    obj: Track | Via | None = idx.tracks_by_id.get(obj_id) or idx.vias_by_id.get(obj_id)
    if obj is None or not obj.net_name:
        return None
    net = obj.net_name
    m = net_metrics(wb, net)
    s = wb.engine.resolver.summary(net)
    layers = sorted({t.layer for t in wb.board.tracks if t.net_name == net})
    info: dict[str, Any] = {
        "net": net,
        "object": "track" if isinstance(obj, Track) else "via",
        "provenance": wb.provenance_of(obj_id).value,
        "locked": wb.is_locked(obj_id, net),
        "net_layers": layers,
        "net_length_mm": round(m.length_nm / 1e6, 3),
        "net_vias": m.vias,
        "net_bends": m.bends,
        "net_segments": m.segments,
        "min_clearance_margin_mm": (
            None if m.min_margin_nm is None else round(m.min_margin_nm / 1e6, 4)
        ),
        "rule_width": s.width.preferred.describe(),
        "rule_clearance": s.clearance.describe(),
    }
    if isinstance(obj, Track):
        info.update(width_mm=obj.width / 1e6, layer=obj.layer, length_mm=round(obj.length / 1e6, 3))
    else:
        info.update(diameter_mm=obj.diameter / 1e6, drill_mm=(obj.drill or 0) / 1e6)
    commit = next(
        (
            c
            for c in reversed(wb.commits)
            if any(t.id == obj_id for t in c.added_tracks)
            or any(v.id == obj_id for v in c.added_vias)
        ),
        None,
    )
    if commit is not None and "score" in commit.metadata:
        info["router_score"] = commit.metadata["score"]
    return info


def section_ids(wb: WorkingBoard, track_id: str) -> list[str]:
    """The run of generated tracks through ``track_id`` up to the nearest junction
    (pad, via, branch or non-generated copper): what "Reroute this section" removes."""
    board = wb.board
    idx = board.index
    start = idx.tracks_by_id.get(track_id)
    if start is None or wb.is_locked(track_id, start.net_name):
        return []
    from pcbrouter.routing.working_board import Provenance

    if wb.provenance_of(track_id) is Provenance.SOURCE_EXISTING:
        return []
    net_tracks = [
        t for t in board.tracks if t.net_name == start.net_name and t.layer == start.layer
    ]
    ends: dict[tuple[int, int], list[Track]] = {}
    for t in net_tracks:
        for p in (t.start, t.end):
            ends.setdefault((p.x, p.y), []).append(t)
    vias = {(v.position.x, v.position.y) for v in board.vias if v.net_name == start.net_name}
    geo = wb.engine.geometry
    pads_at: set[tuple[int, int]] = set()
    for key in ends:
        p = Point(*key)
        for item in geo.copper_near(start.layer, BoundingBox(p.x, p.y, p.x, p.y)):
            if item.kind.value == "pad" and any(s.contains(p) for s in item.shapes):
                pads_at.add(key)
    section = {start.id}
    frontier = [start]
    while frontier:
        t = frontier.pop()
        for p in (t.start, t.end):
            key = (p.x, p.y)
            joined = ends.get(key, [])
            if key in vias or key in pads_at or len(joined) != 2:
                continue  # junction: the section stops here
            for other in joined:
                if other.id in section:
                    continue
                if wb.provenance_of(other.id) is Provenance.SOURCE_EXISTING or wb.is_locked(
                    other.id, other.net_name
                ):
                    continue
                section.add(other.id)
                frontier.append(other)
    return sorted(section)


@dataclass
class RouteDiff:
    added_segments: int
    added_vias: int
    removed_segments: int
    removed_vias: int
    net_length_before_mm: dict[str, float] = field(default_factory=dict)
    net_length_after_mm: dict[str, float] = field(default_factory=dict)
    vias_before: int = 0
    vias_after: int = 0
    drc_errors_before: int | None = None
    drc_errors_after: int | None = None
    note: str = ""

    def lines(self) -> list[str]:
        out = [f"+ {self.added_segments} segment(s), + {self.added_vias} via(s)"]
        if self.removed_segments or self.removed_vias:
            out.append(
                f"- {self.removed_segments} generated segment(s), "
                f"- {self.removed_vias} generated via(s)"
            )
        for net, after in sorted(self.net_length_after_mm.items()):
            before = self.net_length_before_mm.get(net, 0.0)
            out.append(f"{net} length: {before:.2f} → {after:.2f} mm")
        out.append(f"board vias: {self.vias_before} → {self.vias_after}")
        if self.drc_errors_before is not None and self.drc_errors_after is not None:
            out.append(f"Internal DRC errors: {self.drc_errors_before} → {self.drc_errors_after}")
        if self.note:
            out.append(self.note)
        return out


def route_diff(
    wb: WorkingBoard,
    tracks: list[Track],
    vias: list[Via],
    remove_ids: list[str],
    with_drc: bool = True,
) -> RouteDiff:
    """Before/after for a proposed change, computed on a fork (nothing committed)."""
    idx = wb.board.index
    nets = sorted(
        {t.net_name for t in tracks if t.net_name} | {v.net_name for v in vias if v.net_name}
    )
    removed_t = [i for i in remove_ids if i in idx.tracks_by_id]
    removed_v = [i for i in remove_ids if i in idx.vias_by_id]
    diff = RouteDiff(len(tracks), len(vias), len(removed_t), len(removed_v))
    for net in nets:
        diff.net_length_before_mm[net] = round(net_metrics(wb, net).length_nm / 1e6, 3)
    diff.vias_before = len(wb.board.vias)
    if with_drc:
        last = wb.engine.last_drc
        diff.drc_errors_before = len((last or wb.engine.run_drc()).errors)
    fork = wb.fork()
    try:
        fork.commit_objects(tracks, vias, remove_ids, "diff preview", validate=False)
    except CommitError as exc:
        diff.note = f"cannot preview: {exc}"
        return diff
    for net in nets:
        diff.net_length_after_mm[net] = round(net_metrics(fork, net).length_nm / 1e6, 3)
    diff.vias_after = len(fork.board.vias)
    if with_drc:
        diff.drc_errors_after = len(fork.engine.run_drc().errors)
    return diff


def candidate_objects(wb: WorkingBoard, proposal: Any) -> tuple[list[Track], list[Via]]:
    """Domain objects a proposal would add (preview ids only)."""
    from pcbrouter.domain.via import ViaType
    from pcbrouter.routing.working_board import stable_id

    tracks = [
        Track(
            stable_id("preview", proposal.proposal_id, i),
            s.start,
            s.end,
            s.width,
            s.layer,
            proposal.net,
        )
        for i, s in enumerate(proposal.segments)
    ]
    vias = [
        Via(
            stable_id("preview-v", proposal.proposal_id, i),
            v.position,
            v.diameter,
            v.drill,
            proposal.net,
            v.start_layer,
            v.end_layer,
            ViaType.THROUGH,
        )
        for i, v in enumerate(proposal.vias)
    ]
    return tracks, vias


def explain_vias(wb: WorkingBoard, net: str) -> dict[str, Any]:
    """Deterministic facts for "why does this route use a via?" — never a guess:
    it asks the router whether a zero-via route exists on the current board (with
    this net's generated copper removed)."""
    m = net_metrics(wb, net)
    facts: dict[str, Any] = {"net": net, "vias": m.vias}
    if m.vias == 0:
        facts["answer"] = "the route uses no vias"
        return facts
    fork = wb.fork()
    gen = [i for i in fork.generated_ids(net) if not fork.is_locked(i, net)]
    if not gen:
        facts["answer"] = "the vias are source copper from the board file (not router-made)"
        return facts
    fork.commit_objects((), (), gen, "explain", validate=False)
    req = RouteRequest(
        net, request_id=f"explain-{net}", candidates=1, max_vias=0, node_limit=EXPLAIN_NODE_LIMIT
    )
    res = Router(fork.engine).route_net(req)
    if res.status is RouteStatus.SUCCESS:
        facts["zero_via_route"] = "exists"
        facts["answer"] = (
            "a zero-via route exists now; the chosen route used vias because of the "
            "cost model at the time (via cost vs. detour length/congestion) or board "
            "changes since — try Optimize ▸ Reduce Vias"
        )
    else:
        facts["zero_via_route"] = (
            f"none found ({res.reason.value if res.reason else res.status.value})"
        )
        facts["answer"] = "no single-layer route was found: the vias are needed on this board"
        if res.blockers:
            facts["blocking_cells"] = dict(sorted(res.blockers.items())[:6])
    return facts


__all__ = [
    "RouteDiff",
    "candidate_objects",
    "explain_vias",
    "route_diff",
    "route_info",
    "section_ids",
]
