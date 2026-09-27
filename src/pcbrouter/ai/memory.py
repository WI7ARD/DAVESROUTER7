"""Per-board AI memory: user notes and approved decisions that survive restarts.

Evolv-style, adapted: nothing is written without an explicit user action. User
notes are typed in directly; decisions are captured only from proposals the user
approved (already gated). The model never writes here on its own. Entries render
as bounded ``MEMORY_*`` context lines so later requests remember what matters.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FILENAME = "ai_memory.json"
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
    """Entries for one board fingerprint, optionally persisted to a directory."""

    fingerprint: str
    entries: list[MemoryEntry] = field(default_factory=list)
    directory: Path | None = None

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
        self.save()
        return entry

    def retire(self, entry_id: str) -> bool:
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.id != entry_id]
        if len(self.entries) == before:
            return False
        self.save()
        return True

    # ------------------------------------------------------------ context
    def render(self, anonymize: Any = None) -> list[str]:
        """Newest-first bounded lines for the prompt (never the whole store)."""
        lines: list[str] = []
        used = 0
        for entry in sorted(self.entries, key=lambda e: e.created_at, reverse=True):
            text = anonymize(entry.text) if anonymize is not None else entry.text
            line = f"MEMORY_{entry.kind.value.upper()}: {text}"
            if len(lines) >= RENDER_MAX_ENTRIES or used + len(line) > RENDER_MAX_CHARS:
                break
            lines.append(line)
            used += len(line) + 1
        return lines

    # ------------------------------------------------------------ persistence
    def save(self) -> None:
        if self.directory is None:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            payload = {
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
            (self.directory / FILENAME).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("ai.memory.save_failed dir=%s error=%s", self.directory, exc)

    @classmethod
    def load(cls, directory: Path | None, fingerprint: str) -> BoardMemory:
        """Load entries for ``fingerprint``; empty store on any mismatch/problem."""
        if directory is None:
            return cls(fingerprint)
        try:
            raw = json.loads((directory / FILENAME).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.info("ai.memory.empty dir=%s reason=%s", directory, exc)
            return cls(fingerprint, directory=directory)
        if not isinstance(raw, dict) or raw.get("fingerprint") != fingerprint:
            log.info("ai.memory.fingerprint_mismatch dir=%s", directory)
            return cls(fingerprint, directory=directory)
        entries: list[MemoryEntry] = []
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
        return cls(fingerprint, entries[-MAX_ENTRIES:], directory)
