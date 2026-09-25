"""Working-session files: explicit Save/Load Session and crash recovery (Stage 9).

A session file records what the user did on top of the source board — never the
board itself and never credentials:

* source identity (file name, SHA-256, board fingerprint),
* router-added tracks/vias (with ids and provenance), locks, locked regions,
  per-net constraints, corridors,
* history labels, routing settings, AI command metadata (ids, states, operations —
  no prompts' secrets, no keys, no headers).

Crash recovery: after every working-board change the session is written atomically
to ``<workspace>/recovery/working_session.json``; a clean close deletes it. If it
exists when the same board (same SHA-256) is opened again, the user is offered
recovery. Restoring is a normal validated commit ("Recovered session") and can be
undone. A file for a different source is never applied.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pcbrouter import __version__
from pcbrouter.domain.geometry import BoundingBox, Point
from pcbrouter.domain.track import Track
from pcbrouter.domain.via import Via, ViaType
from pcbrouter.kicad.writer import atomic_write

if TYPE_CHECKING:
    from pcbrouter.routing.working_board import Commit, WorkingBoard

log = logging.getLogger(__name__)

SESSION_FORMAT = "pcbrouter-working-session"
SESSION_VERSION = 1
RECOVERY_DIR = "recovery"
RECOVERY_FILE = "working_session.json"
MAX_SESSION_BYTES = 64 * 1024 * 1024


class SessionError(ValueError):
    """The session file cannot be applied (other board, corrupt, invalid copper)."""


def recovery_path(workspace_root: Path) -> Path:
    return workspace_root / RECOVERY_DIR / RECOVERY_FILE


def session_data(
    wb: WorkingBoard,
    source_name: str,
    source_sha256: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    src_t = {t.id for t in wb.source.tracks}
    src_v = {v.id for v in wb.source.vias}
    tracks = [t for t in wb.board.tracks if t.id not in src_t]
    vias = [v for v in wb.board.vias if v.id not in src_v]
    return {
        "format": SESSION_FORMAT,
        "format_version": SESSION_VERSION,
        "app_version": __version__,
        "saved_at": time.time(),
        "source": {
            "name": source_name,
            "sha256": source_sha256,
            "fingerprint": wb.source.fingerprint,
        },
        "working_fingerprint": wb.board.fingerprint,
        "added_tracks": [
            {
                "id": t.id,
                "start": [t.start.x, t.start.y],
                "end": [t.end.x, t.end.y],
                "width": t.width,
                "layer": t.layer,
                "net": t.net_name,
            }
            for t in tracks
        ],
        "added_vias": [
            {
                "id": v.id,
                "at": [v.position.x, v.position.y],
                "diameter": v.diameter,
                "drill": v.drill,
                "layers": [v.start_layer, v.end_layer],
                "net": v.net_name,
            }
            for v in vias
        ],
        "provenance": {k: p.value for k, p in wb.provenance.items()},
        "locks": wb.lock_state(),
        "net_constraints": wb.net_constraints,
        "corridors": [
            {
                "kind": c.kind.value,
                "box": [c.box.min_x, c.box.min_y, c.box.max_x, c.box.max_y],
                "layers": list(c.layers),
            }
            for c in wb.corridors
        ],
        "history": [c.label for c in wb.commits],
        **(extra or {}),
    }


def save_session(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=1)
    if "api_key" in text.lower() or "authorization" in text.lower():
        # defence in depth: sessions never hold credentials
        raise SessionError("refusing to write a session that mentions credentials")
    atomic_write(path, text.encode("utf-8"))
    return path


def load_session(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_SESSION_BYTES:
        raise SessionError("session file is too large")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SessionError(f"cannot read session file: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != SESSION_FORMAT:
        raise SessionError("not a working-session file")
    if int(data.get("format_version", 0)) > SESSION_VERSION:
        raise SessionError("session file is from a newer version of the application")
    return data


def restore_session(wb: WorkingBoard, data: dict[str, Any], source_sha256: str) -> Commit | None:
    """Apply a session to a working board of the *same* source. Validated commit."""
    from pcbrouter.routing.request import SoftRegion, SoftRegionKind
    from pcbrouter.routing.working_board import Provenance

    src = data.get("source") or {}
    if src.get("sha256") != source_sha256:
        raise SessionError("the session belongs to a different version of this board file")
    tracks = [
        Track(
            t["id"],
            Point(*t["start"]),
            Point(*t["end"]),
            int(t["width"]),
            str(t["layer"]),
            t.get("net"),
        )
        for t in data.get("added_tracks", [])
    ]
    vias = [
        Via(
            v["id"],
            Point(*v["at"]),
            int(v["diameter"]),
            v.get("drill"),
            v.get("net"),
            v["layers"][0],
            v["layers"][1],
            ViaType.THROUGH,
        )
        for v in data.get("added_vias", [])
    ]
    commit = None
    if tracks or vias:
        commit = wb.commit_objects(tracks, vias, (), "Recovered session", Provenance.USER_ACCEPTED)
        for obj_id, prov in (data.get("provenance") or {}).items():
            try:
                wb.provenance[obj_id] = Provenance(prov)
            except ValueError:
                continue
    wb.restore_lock_state(data.get("locks") or {})
    wb.net_constraints = dict(data.get("net_constraints") or {})
    wb.corridors = [
        SoftRegion(SoftRegionKind(c["kind"]), BoundingBox(*c["box"]), tuple(c.get("layers", ())))
        for c in data.get("corridors", [])
    ]
    log.info("session.restored tracks=%d vias=%d", len(tracks), len(vias))
    return commit
