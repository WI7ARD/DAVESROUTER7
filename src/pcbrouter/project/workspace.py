"""Per-board workspace locations (computed, not created, in Stage 1).

Each opened board gets a workspace under the application data directory, keyed by
a hash of its absolute path. Future stages store snapshots, route proposals and
the working copy there — **never** inside the user's KiCad project directory and
never over the original file.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from pcbrouter.utils.paths import data_dir

_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True, slots=True)
class Workspace:
    root: Path
    source_path: Path

    @property
    def snapshots_dir(self) -> Path:
        return self.root / "snapshots"

    @property
    def proposals_dir(self) -> Path:
        return self.root / "proposals"

    @property
    def metadata_file(self) -> Path:
        return self.root / "workspace.json"

    @property
    def exists(self) -> bool:
        return self.root.is_dir()


def workspace_key(source_path: Path) -> str:
    # normcase lower-cases on Windows (case-insensitive paths: C:\A and c:\a are the
    # same file) and is a no-op on Linux (case-sensitive paths).
    normalized = os.path.normcase(str(source_path.resolve()))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    stem = _SAFE_RE.sub("_", Path(normalized).stem)[:40] or "board"
    return f"{stem}-{digest}"


def workspace_for(source_path: Path, base_dir: Path | None = None) -> Workspace:
    base = base_dir if base_dir is not None else data_dir() / "workspaces"
    return Workspace(root=base / workspace_key(source_path), source_path=source_path.resolve())
