"""Undo/redo infrastructure.

Design for later stages: board edits will be represented as small, reversible
*change sets* (tracks/vias added/removed relative to an immutable base Board),
not as full board copies. Each change set is wrapped in an :class:`UndoableAction`
and pushed here. Stage 1 has no board-modifying actions; the manager is exercised
with :class:`MetadataAction` to prove the mechanics.
"""

from __future__ import annotations

import itertools
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_MAX_DEPTH = 200


class UndoableAction(ABC):
    """A reversible operation. ``apply`` must be repeatable after ``revert``."""

    @property
    @abstractmethod
    def label(self) -> str: ...

    @property
    def kind(self) -> str:
        return type(self).__name__

    @property
    def metadata(self) -> Mapping[str, Any]:
        return MappingProxyType({})

    @abstractmethod
    def apply(self) -> None: ...

    @abstractmethod
    def revert(self) -> None: ...


class MetadataAction(UndoableAction):
    """Records metadata only; it changes no board state.

    Used by tests and diagnostics to exercise the history mechanics before real
    board-editing actions exist. ``applied`` tracks state so tests can observe it.
    """

    def __init__(self, label: str, metadata: Mapping[str, Any] | None = None) -> None:
        self._label = label
        self._metadata = MappingProxyType(dict(metadata or {}))
        self.applied = False

    @property
    def label(self) -> str:
        return self._label

    @property
    def metadata(self) -> Mapping[str, Any]:
        return self._metadata

    def apply(self) -> None:
        self.applied = True

    def revert(self) -> None:
        self.applied = False


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    sequence: int
    label: str
    kind: str
    timestamp: float
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


class HistoryManager:
    def __init__(self, max_depth: int = DEFAULT_MAX_DEPTH) -> None:
        if max_depth < 1:
            raise ValueError("max_depth must be >= 1")
        self._max_depth = max_depth
        self._undo: list[tuple[HistoryEntry, UndoableAction]] = []
        self._redo: list[tuple[HistoryEntry, UndoableAction]] = []
        self._seq = itertools.count(1)
        self._listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------ queries
    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_label(self) -> str | None:
        return self._undo[-1][0].label if self._undo else None

    @property
    def redo_label(self) -> str | None:
        return self._redo[-1][0].label if self._redo else None

    def entries(self) -> list[HistoryEntry]:
        """Undo stack, oldest first."""
        return [entry for entry, _ in self._undo]

    # ------------------------------------------------------------ mutation
    def push(self, action: UndoableAction, *, already_applied: bool = False) -> HistoryEntry:
        """Apply (unless ``already_applied``) and record ``action``; clears redo."""
        if not already_applied:
            action.apply()
        entry = HistoryEntry(
            sequence=next(self._seq),
            label=action.label,
            kind=action.kind,
            timestamp=time.time(),
            metadata=MappingProxyType(dict(action.metadata)),
        )
        self._undo.append((entry, action))
        if len(self._undo) > self._max_depth:
            del self._undo[0]
        self._redo.clear()
        log.debug("history.push seq=%d label=%r", entry.sequence, entry.label)
        self._notify()
        return entry

    def undo(self) -> HistoryEntry | None:
        if not self._undo:
            return None
        entry, action = self._undo.pop()
        action.revert()
        self._redo.append((entry, action))
        log.debug("history.undo seq=%d label=%r", entry.sequence, entry.label)
        self._notify()
        return entry

    def redo(self) -> HistoryEntry | None:
        if not self._redo:
            return None
        entry, action = self._redo.pop()
        action.apply()
        self._undo.append((entry, action))
        log.debug("history.redo seq=%d label=%r", entry.sequence, entry.label)
        self._notify()
        return entry

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
        self._notify()

    def subscribe(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()
