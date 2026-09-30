"""Routing experience log: what was routed, how, and what happened (per net).

One JSON line per net of a finished board-routing job::

    {"schema": "pcbrouter-experience/1", "app": "1.1.1", "board": "<salted hash>",
     "board_f": {...}, "net_f": {...}, "settings": {...}, "outcome": {...}}

* **Local only.** Nothing is sent anywhere. The file lives in the user data
  folder (``experience/experience.jsonl``) and is capped at ``MAX_BYTES``; the
  previous file is kept once as ``.1``.
* **Anonymised.** Net names, references and coordinates are never stored. The
  board is identified by a salted hash of its fingerprint, so records of the same
  board group together without revealing it. The salt is per installation.
* **Features, not geometry.** Records hold the numbers a learner can use:
  layer count, board size, pads per net, airwire length, escape options,
  congestion, the search settings, and the outcome with its effort.

A learner (level 2) reads the log to choose search settings per kind of net.
It never decides legality: every route is still checked by the exact validator.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pcbrouter

log = logging.getLogger(__name__)

SCHEMA = "pcbrouter-experience/1"
MAX_BYTES = 50 * 2**20
_LOCK = threading.Lock()


def default_dir() -> Path:
    from pcbrouter.utils.paths import data_dir

    return data_dir() / "experience"


def _salt(folder: Path) -> str:
    """A per-installation random salt (created once, kept next to the log)."""
    path = folder / "salt"
    try:
        return path.read_text(encoding="ascii").strip()
    except OSError:
        value = secrets.token_hex(16)
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="ascii")
        return value


def board_id(fingerprint: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{fingerprint}".encode()).hexdigest()[:16]


def _r(x: float, nd: int = 3) -> float:
    return round(float(x), nd)


def board_features(board: Any) -> dict[str, Any]:
    s = board.statistics
    return {
        "copper_layers": s.copper_layer_count,
        "pads": s.pad_count,
        "nets": s.net_count,
        "footprints": s.footprint_count,
        "zones": s.zone_count,
        "width_mm": _r(s.width / 1e6, 1) if s.width is not None else None,
        "height_mm": _r(s.height / 1e6, 1) if s.height is not None else None,
    }


def settings_features(settings: Any) -> dict[str, Any]:
    req = settings.base_request
    return {
        "mode": "speed" if req.heuristic_weight > 1.0 else "accuracy",
        "grid_mm": _r(req.grid_resolution / 1e6),
        "heuristic_weight": _r(req.heuristic_weight),
        "coarse_factor": req.coarse_factor,
        "max_passes": settings.max_passes,
        "allow_ripup": settings.allow_ripup,
        "budget_s": _r(settings.budget_s, 1),
        "parallel_workers": settings.parallel_workers,
        "strategy": settings.strategy.value,
    }


def _bucket(task: Any, bf: dict[str, Any]) -> str:
    from pcbrouter.learning.policy import task_bucket

    return task_bucket(task, int(bf.get("copper_layers") or 2))


def records_from_result(
    result: Any, settings: Any, *, salt: str, source: str = "app"
) -> list[dict[str, Any]]:
    """One record per planned net of a finished :class:`BoardRoutingResult`."""
    plan = result.plan
    board = result.base_board
    bid = board_id(board.fingerprint, salt)
    bf = board_features(board)
    sf = settings_features(settings)
    status = result.status.value
    out: list[dict[str, Any]] = []
    for order, task in enumerate(plan.tasks):
        o = result.outcomes.get(task.net)
        if o is None:
            continue
        out.append(
            {
                "schema": SCHEMA,
                "app": pcbrouter.__version__,
                "source": source,
                "board": bid,
                "board_status": status,
                "board_f": bf,
                "net_f": {
                    "kind": task.kind.value,
                    "pads": task.pads,
                    "airwire_mm": _r(task.airwire_length / 1e6, 2),
                    "escape_options": task.escape_options,
                    "congestion": _r(task.congestion),
                    "priority": task.priority,
                    "order": order,
                    "order_frac": _r(order / max(1, len(plan.tasks) - 1)),
                    "paired": task.group is not None,
                    "bucket": _bucket(task, bf),
                },
                "settings": sf,
                "outcome": {
                    "status": o.status.value,
                    "reason": o.reason.value if o.reason else None,
                    "vias": o.vias,
                    "length_mm": _r(o.length_nm / 1e6, 2),
                    "passes": o.passes,
                    "attempts": o.attempts,
                    "expanded_nodes": o.expanded_nodes,
                    "route_s": _r(o.route_s),
                    "ripped": bool(o.removed_ids),
                    "arms": list(getattr(o, "arms", []) or []),
                    "policy": getattr(getattr(settings, "policy", None), "name", "fixed"),
                },
            }
        )
    return out


class ExperienceLog:
    """Append-only JSONL with a size cap (thread- and process-tolerant appends)."""

    def __init__(self, folder: Path | None = None, max_bytes: int = MAX_BYTES) -> None:
        self.folder = folder or default_dir()
        self.path = self.folder / "experience.jsonl"
        self.max_bytes = max_bytes

    @property
    def salt(self) -> str:
        return _salt(self.folder)

    def append(self, records: Iterable[dict[str, Any]]) -> int:
        lines = [json.dumps(r, sort_keys=True, separators=(",", ":")) for r in records]
        if not lines:
            return 0
        data = ("\n".join(lines) + "\n").encode("utf-8")
        with _LOCK:
            self.folder.mkdir(parents=True, exist_ok=True)
            try:
                if self.path.stat().st_size + len(data) > self.max_bytes:
                    os.replace(self.path, self.path.with_suffix(".jsonl.1"))
            except FileNotFoundError:
                pass
            with self.path.open("ab") as fh:
                fh.write(data)
        return len(lines)

    def read(self) -> Iterator[dict[str, Any]]:
        for p in (self.path.with_suffix(".jsonl.1"), self.path):
            if not p.exists():
                continue
            with p.open(encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            continue  # a torn last line after a crash


def record_board_job(
    result: Any,
    settings: Any,
    *,
    folder: Path | None = None,
    source: str = "app",
) -> int:
    """Append a finished job's records; never raises (logging must not break routing)."""
    try:
        exp = ExperienceLog(folder)
        return exp.append(records_from_result(result, settings, salt=exp.salt, source=source))
    except Exception:
        log.warning("experience.record_failed", exc_info=True)
        return 0
