"""Per-board AI memory: user notes and approved decisions that survive restarts.

Evolv-style, adapted: nothing is written without an explicit user action. User
notes are typed in directly; decisions are captured only from proposals the user
approved (already gated). The model never writes here on its own. Entries render
as bounded ``MEMORY_*`` context lines so later requests remember what matters.

Architecture rule: this module is pure logic — no file or process access (see
``test_ai_modules_never_write_files_or_spawn_processes``). Persistence lives in
:mod:`pcbrouter.project.ai_memory_store`; this module only serialises, and calls
the ``on_change`` hook (bound by the UI layer) after every mutation.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FORMAT_VERSION = 1
MAX_TEXT_CHARS = 500
MAX_ENTRIES = 50
RENDER_MAX_ENTRIES = 10
RENDER_MAX_CHARS = 2000


class MemoryKind(StrEnum):
    NOTE = "note"  # typed by the user
    DECISION = "decision"  # captured from an approved proposal
    PREFERENCE = "preference"  # promoted by the user (e.g. "always minimize vias")


@dataclass
class MemoryEntry:
    id: str
    kind: MemoryKind
    text: str
    source: str  # "user" | "approval:<proposal_id>"
    created_at: float = field(default_factory=time.time)

    def render(self) -> str:
        return f"MEMORY_{self.kind.value.upper()}: {self.text}"


@dataclass
class BoardMemory:
    """Entries for one board fingerprint.

    Pure logic: persistence is done by the caller through :meth:`to_dict` /
    :meth:`from_dict` (see :mod:`pcbrouter.project.ai_memory_store`). After every
    mutation the ``on_change`` hook runs (bound by the UI layer to save).
    """

    fingerprint: str
    entries: list[MemoryEntry] = field(default_factory=list)
    directory: Path | None = None
    on_change: Callable[[BoardMemory], None] | None = field(default=None, repr=False)

    # ------------------------------------------------------------ mutation
    def add(self, kind: MemoryKind, text: str, source: str = "user") -> MemoryEntry:
        cleaned = " ".join(text.split())
        if not cleaned:
            raise ValueError("memory text is empty")
        if len(cleaned) > MAX_TEXT_CHARS:
            raise ValueError(f"memory text over the {MAX_TEXT_CHARS} character limit")
        entry = MemoryEntry(uuid.uuid4().hex[:8], kind, cleaned, source)
        self.entries.append(entry)
        while len(self.entries) > MAX_ENTRIES:
            self.entries.pop(0)
        self._notify()
        return entry

    def retire(self, entry_id: str) -> bool:
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.id != entry_id]
        if len(self.entries) == before:
            return False
        self._notify()
        return True

    def _notify(self) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change(self)
        except Exception:  # a save failure must never break the session
            log.debug("ai.memory.notify_failed", exc_info=True)

    # ------------------------------------------------------------ context
    def render(self, anonymize: Any = None) -> list[str]:
        """Newest-first bounded lines for the prompt (never the whole store)."""
        lines: list[str] = []
        used = 0
        # Insertion order is chronological (loading preserves it), so plain
        # reversal is newest-first even when timestamps tie on coarse clocks.
        for entry in reversed(self.entries):
            text = anonymize(entry.text) if anonymize is not None else entry.text
            line = f"MEMORY_{entry.kind.value.upper()}: {text}"
            if len(lines) >= RENDER_MAX_ENTRIES or used + len(line) > RENDER_MAX_CHARS:
                break
            lines.append(line)
            used += len(line) + 1
        return lines

    # ------------------------------------------------------------ serialisation
    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": FORMAT_VERSION,
            "fingerprint": self.fingerprint,
            "entries": [
                {
                    "id": e.id,
                    "kind": e.kind.value,
                    "text": e.text,
                    "source": e.source,
                    "created_at": e.created_at,
                }
                for e in self.entries
            ],
        }

    @classmethod
    def from_dict(cls, raw: Any, fingerprint: str) -> BoardMemory:
        """Validated entries for ``fingerprint``; empty store on any mismatch."""
        entries: list[MemoryEntry] = []
        if isinstance(raw, dict) and raw.get("fingerprint") == fingerprint:
            for item in raw.get("entries", []):
                try:
                    kind = MemoryKind(item["kind"])
                    text = str(item["text"])
                    if not text or len(text) > MAX_TEXT_CHARS:
                        continue
                    entries.append(
                        MemoryEntry(
                            str(item.get("id", uuid.uuid4().hex[:8])),
                            kind,
                            text,
                            str(item.get("source", "user")),
                            float(item.get("created_at", time.time())),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
        return cls(fingerprint, entries[-MAX_ENTRIES:])
