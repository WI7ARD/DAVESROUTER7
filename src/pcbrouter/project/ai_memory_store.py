"""Board AI-memory file store (one JSON file per board workspace).

All file access for AI memory lives here — never inside :mod:`pcbrouter.ai`
(see ``test_ai_modules_never_write_files_or_spawn_processes``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pcbrouter.ai.memory import BoardMemory

log = logging.getLogger(__name__)

FILENAME = "ai_memory.json"


def _path(directory: Path) -> Path:
    return directory / FILENAME


def load_board_memory(directory: Path | None, fingerprint: str) -> BoardMemory:
    """Entries for ``fingerprint``; empty store on any mismatch/problem."""
    memory = BoardMemory(fingerprint, directory=directory)
    if directory is None:
        return memory
    try:
        raw = json.loads(_path(directory).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.info("ai.memory.empty dir=%s reason=%s", directory, exc)
        return memory
    if not isinstance(raw, dict) or raw.get("fingerprint") != fingerprint:
        log.info("ai.memory.fingerprint_mismatch dir=%s", directory)
        return memory
    memory.entries = BoardMemory.from_dict(raw, fingerprint).entries
    return memory


def save_board_memory(directory: Path, memory: BoardMemory) -> None:
    """Write ``memory`` to ``directory`` (best effort; never raises)."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _path(directory).write_text(json.dumps(memory.to_dict(), indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("ai.memory.save_failed dir=%s error=%s", directory, exc)
