"""Undo/redo, snapshots and route-proposal groundwork (no board edits in Stage 1)."""

from __future__ import annotations

from pcbrouter.history.history import (
    HistoryEntry,
    HistoryManager,
    MetadataAction,
    UndoableAction,
)
from pcbrouter.history.snapshot import (
    MetadataSnapshotStore,
    ProposalStateError,
    ProposalStatus,
    RouteProposal,
    SnapshotRef,
    SnapshotStore,
)

__all__ = [
    "HistoryEntry",
    "HistoryManager",
    "MetadataAction",
    "MetadataSnapshotStore",
    "ProposalStateError",
    "ProposalStatus",
    "RouteProposal",
    "SnapshotRef",
    "SnapshotStore",
    "UndoableAction",
]
