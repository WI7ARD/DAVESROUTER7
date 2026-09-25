"""The working board: the only place routed copper is committed (Stage 4 spec 4.16).

::

    source board (file, read-only) ─▶ WorkingBoard.board ─▶ commits (accept / rip-up)
                                                          └▶ explicit export (Stage 9)

* The source :class:`Board` is never changed; each commit produces a new immutable
  Board (``dataclasses.replace``) and keeps the previous one, so undo restores the
  exact previous board (same fingerprint).
* Geometry is updated incrementally (spatial index insert/remove), not rebuilt.
  The working board owns its geometry copy; the engine for the current state
  shares it.
* Provenance per track/via: SOURCE_EXISTING (from the file), ROUTER_GENERATED,
  USER_ACCEPTED, plus user locks. Rip-up never touches source or locked copper.
* Every committed segment/via passed the exact validator when it was proposed;
  :meth:`commit_proposals` re-validates against the *current* state before applying.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from pcbrouter.board_engine import BoardEngine, EngineConfig
from pcbrouter.domain.board import Board
from pcbrouter.domain.geometry import BoundingBox
from pcbrouter.domain.track import Track
from pcbrouter.domain.via import Via, ViaType
from pcbrouter.geometry.board import BoardGeometry, CopperItem, HoleItem
from pcbrouter.geometry.extract import build_board_geometry, track_item, via_items
from pcbrouter.kicad.rule_adapter import ProjectRuleData
from pcbrouter.routing.proposal import RouteProposal
from pcbrouter.rules.overrides import RuleOverrides

log = logging.getLogger(__name__)

_ID_NAMESPACE = uuid.UUID("7b1c1e8e-3f5a-4d57-9a4e-5c0e8f1d2a90")


class Provenance(Enum):
    SOURCE_EXISTING = "source_existing"
    ROUTER_GENERATED = "router_generated"
    USER_ACCEPTED = "user_accepted"
    OPTIMIZER = "optimizer"


class CommitError(RuntimeError):
    """A commit was refused (stale proposal, invalid geometry, locked copper)."""


@dataclass(frozen=True, slots=True)
class Commit:
    commit_id: str
    label: str
    added_tracks: tuple[Track, ...]
    added_vias: tuple[Via, ...]
    removed_tracks: tuple[Track, ...]
    removed_vias: tuple[Via, ...]
    before: Board
    after: Board
    provenance: Provenance
    metadata: dict[str, Any] = field(default_factory=dict)
    removed_provenance: dict[str, Provenance] = field(default_factory=dict)

    @property
    def nets(self) -> list[str]:
        names = {o.net_name for o in _objs(self.added_tracks, self.added_vias)}
        names |= {o.net_name for o in _objs(self.removed_tracks, self.removed_vias)}
        return sorted(n for n in names if n)

    def diff_summary(self) -> str:
        parts = []
        if self.added_tracks or self.added_vias:
            parts.append(f"+{len(self.added_tracks)} segment(s), +{len(self.added_vias)} via(s)")
        if self.removed_tracks or self.removed_vias:
            parts.append(
                f"-{len(self.removed_tracks)} segment(s), -{len(self.removed_vias)} via(s)"
            )
        return "; ".join(parts) or "no change"


def _objs(tracks: Iterable[Track], vias: Iterable[Via]) -> list[Track | Via]:
    return [*tracks, *vias]


def stable_id(*parts: object) -> str:
    """Deterministic KiCad-style UUID for generated objects."""
    return str(uuid.uuid5(_ID_NAMESPACE, "/".join(str(p) for p in parts)))


Listener = Callable[["WorkingBoard", Commit | None], None]


class WorkingBoard:
    def __init__(
        self,
        source: Board,
        project_rules: ProjectRuleData | None = None,
        overrides: RuleOverrides | None = None,
        config: EngineConfig | None = None,
    ) -> None:
        self.source = source
        self.board = source
        self.project_rules = project_rules or ProjectRuleData()
        self.overrides = overrides or RuleOverrides()
        self.config = config or EngineConfig()
        self.provenance: dict[str, Provenance] = {}
        #: locks: track/via ids, "net:<name>", "comp:<reference>" (Stage 8)
        self.locks: set[str] = set()
        #: locked regions: new routes may not enter; copper inside is protected
        self.locked_regions: list[BoundingBox] = []
        #: per-net routing preferences set by the user (Stage 8 constraints panel)
        self.net_constraints: dict[str, dict[str, Any]] = {}
        #: user corridors (soft cost fields): prefer / avoid regions
        self.corridors: list[Any] = []
        self._protected_cache: tuple[Any, frozenset[str]] | None = None
        self.commits: list[Commit] = []  # applied, oldest first
        self._geometry: BoardGeometry | None = None
        self._engine: BoardEngine | None = None
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []
        self._sequence = 0
        self._rules: tuple[Any, Any] | None = None

    # ------------------------------------------------------------ engine
    @property
    def geometry(self) -> BoardGeometry:
        with self._lock:
            if self._geometry is None:
                self._geometry = build_board_geometry(self.board)
            return self._geometry

    @property
    def engine(self) -> BoardEngine:
        with self._lock:
            if self._engine is None:
                engine = BoardEngine(self.board, self.project_rules, self.overrides, self.config)
                if self._rules is not None:  # copper commits never change the rules
                    engine.__dict__["ruleset"], engine.__dict__["resolver"] = self._rules
                if self._geometry is not None:
                    engine.__dict__["geometry"] = self._geometry
                else:
                    # build lazily inside the engine, then adopt it as ours
                    self._geometry = engine.geometry
                self._engine = engine
                if self._rules is None and "ruleset" in engine.__dict__:
                    self._rules = (engine.ruleset, engine.resolver)
            return self._engine

    def rules(self) -> tuple[Any, Any]:
        """(RuleSet, RuleResolver) for the current rules, cached across commits."""
        engine = self.engine
        if self._rules is None:
            self._rules = (engine.ruleset, engine.resolver)
        return self._rules

    def fork(self) -> WorkingBoard:
        """A private copy for background board routing: same source, provenance and
        locks; its own geometry. Commits on the fork never affect this board."""
        with self._lock:
            other = WorkingBoard(self.source, self.project_rules, self.overrides, self.config)
            other.board = self.board
            other.provenance = dict(self.provenance)
            other.locks = set(self.locks)
            other.locked_regions = list(self.locked_regions)
            other.net_constraints = dict(self.net_constraints)
            other.corridors = list(self.corridors)
            other._geometry = self.geometry.copy()
            other._rules = self._rules
            return other

    def set_rules(self, overrides: RuleOverrides, config: EngineConfig) -> None:
        with self._lock:
            self.overrides = overrides
            self.config = config
            self._engine = None
            self._rules = None

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    @property
    def modified(self) -> bool:
        return self.board is not self.source

    @property
    def fingerprint(self) -> str:
        return self.board.fingerprint

    def provenance_of(self, obj_id: str) -> Provenance:
        return self.provenance.get(obj_id, Provenance.SOURCE_EXISTING)

    def is_locked(self, obj_id: str, net: str | None = None) -> bool:
        if obj_id in self.locks or (net is not None and f"net:{net}" in self.locks):
            return True
        return bool(obj_id) and obj_id in self.protected_ids()

    def protected_ids(self) -> frozenset[str]:
        """Track/via ids protected by component or region locks (cached per state)."""
        comps = sorted(lk[5:] for lk in self.locks if lk.startswith("comp:"))
        key = (self.board.fingerprint, tuple(comps), tuple(self.locked_regions))
        if self._protected_cache is not None and self._protected_cache[0] == key:
            return self._protected_cache[1]
        out: set[str] = set()
        if comps or self.locked_regions:
            geo = self.geometry
            for t in self.board.tracks:
                if any(r.intersects(t.bounds) for r in self.locked_regions):
                    out.add(t.id)
            for v in self.board.vias:
                if any(r.intersects(v.bounds) for r in self.locked_regions):
                    out.add(v.id)
            from pcbrouter.geometry.clearance import touches

            for pad in self.board.pads:
                if pad.footprint_ref not in comps:
                    continue
                item = geo.copper.get(f"pad:{pad.id}")
                if item is None:
                    continue
                for layer in item.layers:
                    for other in geo.copper_near(layer, item.bounds.expanded(1)):
                        if other.kind.value in ("track", "via") and any(
                            touches(a, b) for a in item.shapes for b in other.shapes
                        ):
                            out.add(other.source_id)
        result = frozenset(out)
        self._protected_cache = (key, result)
        return result

    def lock_state(self) -> dict[str, Any]:
        return {
            "locks": sorted(self.locks),
            "regions": [[r.min_x, r.min_y, r.max_x, r.max_y] for r in self.locked_regions],
        }

    def restore_lock_state(self, state: dict[str, Any]) -> None:
        self.locks = set(state.get("locks", []))
        self.locked_regions = [BoundingBox(*r) for r in state.get("regions", [])]
        self._protected_cache = None
        self._notify(None)

    def generated_ids(self, net: str | None = None) -> list[str]:
        idx = self.board.index
        out = []
        for obj_id, prov in self.provenance.items():
            if prov is Provenance.SOURCE_EXISTING:
                continue
            obj = idx.tracks_by_id.get(obj_id) or idx.vias_by_id.get(obj_id)
            if obj is not None and (net is None or obj.net_name == net):
                out.append(obj_id)
        return sorted(out)

    # ------------------------------------------------------------ commits
    def commit_proposals(
        self,
        proposals: Iterable[RouteProposal],
        label: str,
        provenance: Provenance = Provenance.ROUTER_GENERATED,
        remove_ids: Iterable[str] = (),
        metadata: dict[str, Any] | None = None,
        validate: bool = True,
    ) -> Commit:
        """Add proposal copper (and optionally rip up ``remove_ids``) as one commit."""
        with self._lock:
            proposals = list(proposals)
            seq = self._sequence + 1
            tracks: list[Track] = []
            vias: list[Via] = []
            for p_i, prop in enumerate(proposals):
                for s_i, seg in enumerate(prop.segments):
                    tid = stable_id(prop.proposal_id, seq, p_i, "s", s_i)
                    tracks.append(Track(tid, seg.start, seg.end, seg.width, seg.layer, prop.net))
                for v_i, via in enumerate(prop.vias):
                    vid = stable_id(prop.proposal_id, seq, p_i, "v", v_i)
                    vias.append(
                        Via(
                            vid,
                            via.position,
                            via.diameter,
                            via.drill,
                            prop.net,
                            via.start_layer,
                            via.end_layer,
                            ViaType.THROUGH,
                        )
                    )
            return self.commit_objects(
                tracks, vias, remove_ids, label, provenance, metadata, validate
            )

    def commit_objects(
        self,
        tracks: Iterable[Track],
        vias: Iterable[Via],
        remove_ids: Iterable[str],
        label: str,
        provenance: Provenance = Provenance.ROUTER_GENERATED,
        metadata: dict[str, Any] | None = None,
        validate: bool = True,
    ) -> Commit:
        """Add tracks/vias and remove generated copper as one commit. With
        ``validate`` every added object is checked against the resulting state and
        the commit is rolled back if anything is not legal."""
        with self._lock:
            tracks, vias = tuple(tracks), tuple(vias)
            remove = sorted(set(remove_ids))
            idx = self.board.index
            removed_tracks = tuple(idx.tracks_by_id[i] for i in remove if i in idx.tracks_by_id)
            removed_vias = tuple(idx.vias_by_id[i] for i in remove if i in idx.vias_by_id)
            for obj in _objs(removed_tracks, removed_vias):
                if self.provenance_of(obj.id) is Provenance.SOURCE_EXISTING:
                    raise CommitError(f"refusing to remove source copper {obj.id}")
                if self.is_locked(obj.id, obj.net_name):
                    raise CommitError(f"refusing to remove locked copper {obj.id}")
            for obj in _objs(tracks, vias):
                if self.is_locked("", obj.net_name):
                    raise CommitError(f"net {obj.net_name} is locked")
            self._sequence += 1
            before = self.board
            removed_set = set(remove)
            after = replace(
                before,
                tracks=tuple(t for t in before.tracks if t.id not in removed_set) + tracks,
                vias=tuple(v for v in before.vias if v.id not in removed_set) + vias,
            )
            commit = Commit(
                commit_id=f"commit-{self._sequence}",
                label=label,
                added_tracks=tracks,
                added_vias=vias,
                removed_tracks=removed_tracks,
                removed_vias=removed_vias,
                before=before,
                after=after,
                provenance=provenance,
                metadata=dict(metadata or {}),
                removed_provenance={
                    o.id: self.provenance_of(o.id) for o in _objs(removed_tracks, removed_vias)
                },
            )
            self._apply(commit, notify=False)
            if validate:
                problems = self._problems(commit)
                if problems:
                    self._revert(commit, notify=False)
                    raise CommitError(
                        "not committed — the copper is not valid on the working board: "
                        + "; ".join(problems[:3])
                    )
            self._notify(commit)
            return commit

    def _problems(self, commit: Commit) -> list[str]:
        validator = self.engine.validator
        out: list[str] = []
        for t in commit.added_tracks:
            r = validator.validate_segment(t.net_name, t.layer, t.start, t.end, t.width)
            if not r.legal:
                out += [f"track {t.id[:8]}: {m}" for m in r.messages()[:2]]
        for v in commit.added_vias:
            if v.drill is None or v.start_layer is None or v.end_layer is None:
                out.append(f"via {v.id[:8]}: incomplete via")
                continue
            r = validator.validate_via(
                v.net_name, v.position, v.start_layer, v.end_layer, v.diameter, v.drill
            )
            own = {f"via:{v.id}", f"hole:{v.id}"}  # the committed via itself
            others = [c for c in r.collisions if c.object_id not in own]
            if others or (not r.collisions and not r.legal):
                out += [f"via {v.id[:8]}: {c.message}" for c in others[:2]] or [
                    f"via {v.id[:8]}: {m}" for m in r.messages()[:2]
                ]
        return out

    def undo(self) -> Commit | None:
        with self._lock:
            if not self.commits:
                return None
            commit = self.commits[-1]
            self._revert(commit)
            return commit

    def redo(self, commit: Commit) -> None:
        with self._lock:
            if self.board is not commit.before:
                raise CommitError("cannot redo: the working board changed since")
            self._apply(commit)

    def reset(self) -> None:
        """Back to the source board (all generated copper removed)."""
        with self._lock:
            while self.commits:
                self._revert(self.commits[-1])

    # ------------------------------------------------------------ internals
    def _items(
        self, tracks: Iterable[Track], vias: Iterable[Via]
    ) -> tuple[list[CopperItem], list[HoleItem]]:
        copper: list[CopperItem] = []
        holes: list[HoleItem] = []
        layers = self.geometry.copper_layers
        for t in tracks:
            copper.append(track_item(t))
        for v in vias:
            item, hole = via_items(v, layers)
            copper.append(item)
            if hole is not None:
                holes.append(hole)
        return copper, holes

    @staticmethod
    def _uids(tracks: Iterable[Track], vias: Iterable[Via]) -> list[str]:
        out = [f"track:{t.id}" for t in tracks]
        for v in vias:
            out += [f"via:{v.id}", f"hole:{v.id}"]
        return out

    def _apply(self, commit: Commit, notify: bool = True) -> None:
        geo = self.geometry
        add_c, add_h = self._items(commit.added_tracks, commit.added_vias)
        geo.apply_changes(
            commit.after, add_c, add_h, self._uids(commit.removed_tracks, commit.removed_vias)
        )
        self.board = commit.after
        for obj in _objs(commit.removed_tracks, commit.removed_vias):
            self.provenance.pop(obj.id, None)
        for obj in _objs(commit.added_tracks, commit.added_vias):
            self.provenance[obj.id] = commit.provenance
        self.commits.append(commit)
        self._engine = None
        log.info(
            "working.commit %s %s: %s fingerprint=%s",
            commit.commit_id,
            commit.label,
            commit.diff_summary(),
            commit.after.fingerprint[:12],
        )
        if notify:
            self._notify(commit)

    def _revert(self, commit: Commit, notify: bool = True) -> None:
        if not self.commits or self.commits[-1] is not commit:
            raise CommitError("only the most recent commit can be reverted")
        geo = self.geometry
        add_c, add_h = self._items(commit.removed_tracks, commit.removed_vias)
        geo.apply_changes(
            commit.before, add_c, add_h, self._uids(commit.added_tracks, commit.added_vias)
        )
        self.board = commit.before
        for obj in _objs(commit.added_tracks, commit.added_vias):
            self.provenance.pop(obj.id, None)
        self.provenance.update(commit.removed_provenance)
        self.commits.pop()
        self._engine = None
        log.info("working.revert %s %s", commit.commit_id, commit.label)
        if notify:
            self._notify(None)

    def _notify(self, commit: Commit | None) -> None:
        for listener in list(self._listeners):
            listener(self, commit)
