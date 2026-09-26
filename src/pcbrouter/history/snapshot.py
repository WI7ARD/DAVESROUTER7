"""Snapshot and route-proposal groundwork.

Future strategy (documented in docs/architecture.md):

* A **snapshot** is a named restore point. The on-disk form (Stage 9) will be a
  copy of the *working* board inside the project workspace, written before every
  save — never inside the user's KiCad project folder, never over the original.
* A **route proposal** is a set of new tracks/vias produced by the router, shown as
  a before/after diff, and applied only when the user accepts it.

Stage 1 stores metadata only: no board copies are serialised and nothing is written
to disk.
"""

from __future__ import annotations

import itertools
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    id: str
    created_at: float
    source_path: Path
    board_sha256: str
    description: str
    storage_path: Path | None = None  # None: metadata-only (Stage 1)


class SnapshotStore(ABC):
    @abstractmethod
    def create(self, source_path: Path, board_sha256: str, description: str) -> SnapshotRef: ...

    @abstractmethod
    def list_snapshots(self) -> list[SnapshotRef]: ...

    def get(self, snapshot_id: str) -> SnapshotRef | None:
        return next((s for s in self.list_snapshots() if s.id == snapshot_id), None)


class MetadataSnapshotStore(SnapshotStore):
    """In-memory, metadata-only store. Writes nothing to disk."""

    def __init__(self) -> None:
        self._items: list[SnapshotRef] = []
        self._ids = itertools.count(1)

    def create(self, source_path: Path, board_sha256: str, description: str) -> SnapshotRef:
        ref = SnapshotRef(
            id=f"snap-{next(self._ids):04d}",
            created_at=time.time(),
            source_path=source_path,
            board_sha256=board_sha256,
            description=description,
        )
        self._items.append(ref)
        return ref

    def list_snapshots(self) -> list[SnapshotRef]:
        return list(self._items)


class ProposalStatus(Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ProposalStateError(RuntimeError):
    """A proposal was accepted/rejected after it had already been decided."""


@dataclass(frozen=True, slots=True)
class RouteProposal:
    """Metadata of a future router result awaiting user review."""

    id: str
    description: str
    base_board_sha256: str
    added_tracks: int = 0
    added_vias: int = 0
    removed_tracks: int = 0
    status: ProposalStatus = ProposalStatus.PENDING

    def accept(self) -> RouteProposal:
        return self._transition(ProposalStatus.ACCEPTED)

    def reject(self) -> RouteProposal:
        return self._transition(ProposalStatus.REJECTED)

    def _transition(self, status: ProposalStatus) -> RouteProposal:
        if self.status is not ProposalStatus.PENDING:
            raise ProposalStateError(f"proposal {self.id} is already {self.status.value}")
        return replace(self, status=status)
