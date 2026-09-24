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
        self.locks: set[str] = set()  # track/via ids and net names ("net:<name>")
        self.commits: list[Commit] = []  # applied, oldest first
        self._geometry: BoardGeometry | None = None
        self._engine: BoardEngine | None = None
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []
        self._sequence = 0

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
                if self._geometry is not None:
                    engine.__dict__["geometry"] = self._geometry
                else:
                    # build lazily inside the engine, then adopt it as ours
                    self._geometry = engine.geometry
                self._engine = engine
            return self._engine

    def set_rules(self, overrides: RuleOverrides, config: EngineConfig) -> None:
        with self._lock:
            self.overrides = overrides
            self.config = config
            self._engine = None

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
        return obj_id in self.locks or (net is not None and f"net:{net}" in self.locks)

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
            remove = sorted(set(remove_ids))
            idx = self.board.index
            removed_tracks = tuple(idx.tracks_by_id[i] for i in remove if i in idx.tracks_by_id)
            removed_vias = tuple(idx.vias_by_id[i] for i in remove if i in idx.vias_by_id)
            for obj in _objs(removed_tracks, removed_vias):
                if self.provenance_of(obj.id) is Provenance.SOURCE_EXISTING:
                    raise CommitError(f"refusing to remove source copper {obj.id}")
                if self.is_locked(obj.id, obj.net_name):
                    raise CommitError(f"refusing to remove locked copper {obj.id}")
            self._sequence += 1
            seq = self._sequence
            tracks: list[Track] = []
            vias: list[Via] = []
            for p_i, prop in enumerate(proposals):
                if self.is_locked("", prop.net):
                    raise CommitError(f"net {prop.net} is locked")
                for s_i, seg in enumerate(prop.segments):
                    tracks.append(
                        Track(
                            stable_id(prop.proposal_id, seq, p_i, "s", s_i),
                            seg.start,
                            seg.end,
                            seg.width,
                            seg.layer,
                            prop.net,
                        )
                    )
                for v_i, via in enumerate(prop.vias):
                    vtype = ViaType.THROUGH
                    vias.append(
                        Via(
                            stable_id(prop.proposal_id, seq, p_i, "v", v_i),
                            via.position,
                            via.diameter,
                            via.drill,
                            prop.net,
                            via.start_layer,
                            via.end_layer,
                            vtype,
                        )
                    )
            if validate:
                self._validate_against_current(proposals, set(remove))
            before = self.board
            removed_set = set(remove)
            after = replace(
                before,
                tracks=tuple(t for t in before.tracks if t.id not in removed_set) + tuple(tracks),
                vias=tuple(v for v in before.vias if v.id not in removed_set) + tuple(vias),
            )
            commit = Commit(
                commit_id=f"commit-{seq}",
                label=label,
                added_tracks=tuple(tracks),
                added_vias=tuple(vias),
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
            self._apply(commit)
            return commit

    def _validate_against_current(self, proposals: list[RouteProposal], removing: set[str]) -> None:
        if removing:
            return  # validated by the caller against the ripped-up state (Stage 5)
        validator = self.engine.validator
        for prop in proposals:
            res = validator.validate_route(prop)
            if not res.legal:
                raise CommitError(
                    f"proposal {prop.proposal_id} is no longer valid on the working board: "
                    + "; ".join(res.messages[:3])
                )

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

    def _apply(self, commit: Commit) -> None:
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
        self._notify(commit)

    def _revert(self, commit: Commit) -> None:
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
        self._notify(None)

    def _notify(self, commit: Commit | None) -> None:
        for listener in list(self._listeners):
            listener(self, commit)
