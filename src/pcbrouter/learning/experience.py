"""Routing experience log: what was routed, how, and what happened (per net).

One JSON line per net of a finished board-routing job::

    {"schema": "pcbrouter-experience/1", "app": "1.1.1", "board": "<salted hash>",
     "board_f": {...}, "net_f": {...}, "settings": {...}, "outcome": {...}}

* **Local only.** Nothing is sent anywhere. The file lives in the user data
  folder (``experience/experience.jsonl``) and is capped at ``MAX_BYTES``; the
  previous file is kept once as ``.1``. Nothing leaves the computer unless the
  user exports it (:func:`export_bundle`) and sends the file themselves.
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

import contextlib
import hashlib
import json
import logging
import os
import secrets
import threading
import zipfile
import zlib
from collections import Counter
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pcbrouter

log = logging.getLogger(__name__)

#: 2 adds the board profile (``board_p``), a per-attempt ``outcome.trace``, the
#: policy decision and board-level job results; readers accept 1 and 2
SCHEMA = "pcbrouter-experience/2"
#: record schemas readers (and the export) accept
KNOWN_SCHEMAS = ("pcbrouter-experience/1", "pcbrouter-experience/2")
#: the user-initiated export bundle (a zip: ``manifest.json`` + ``records.jsonl``)
EXPORT_SCHEMA = "pcbrouter-experience-export/1"
EXPORT_CONTENTS = (
    "anonymised per-net routing records; no net names, references, coordinates, "
    "file paths; board ids are salted hashes; the salt is not included"
)
MAX_BYTES = 50 * 2**20
#: stratified retention: records kept per stratum (mode + context bucket) when
#: the oldest file is rotated out, so rare kinds of nets are not starved first
KEEP_PER_STRATUM = 100
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


def _bucket(task: Any, layers: int, demand: float | None) -> str:
    from pcbrouter.learning.policy import task_bucket

    return task_bucket(task, layers, demand)


def _profile(result: Any) -> dict[str, float]:
    from pcbrouter.learning.features import board_profile

    try:
        return {k: _r(v, 4) for k, v in board_profile(result.base_board, result.plan.tasks).items()}
    except Exception:  # a profile is an extra: never lose the record over it
        log.debug("board profile unavailable", exc_info=True)
        return {}


def _trace(o: Any) -> list[dict[str, Any]]:
    out = []
    for t in getattr(o, "trace", None) or []:
        out.append({k: (_r(v, 4) if isinstance(v, float) else v) for k, v in t.items()})
    return out


def records_from_result(
    result: Any, settings: Any, *, salt: str, source: str = "app"
) -> list[dict[str, Any]]:
    """One record per planned net of a finished :class:`BoardRoutingResult`."""
    plan = result.plan
    board = result.base_board
    bid = board_id(board.fingerprint, salt)
    from pcbrouter.learning.policy import router_signature

    router = router_signature()
    bf = board_features(board)
    bp = _profile(result)
    layers = int(bp.get("signal_layers") or bf.get("copper_layers") or 2)
    sf = settings_features(settings)
    status = result.status.value
    decision = getattr(result, "policy_decision", None) or {}
    active = decision.get("decision") in ("POLICY", "LEARNED")
    policy_name = (
        decision.get("policy") or getattr(getattr(settings, "policy", None), "name", "fixed")
        if active
        else "fixed"
    )
    m = result.metrics
    job = {
        "completed": m.nets_completed,
        "attempted": m.nets_attempted,
        "fully_routed": m.nets_attempted > 0 and m.nets_completed == m.nets_attempted,
        #: the router's end-game summary (a dict) once it reports one, else None
        "endgame": getattr(result, "endgame", None),
        "ripups": m.ripups,
        "runtime_s": _r(m.runtime_s, 2),
        "new_vias": m.new_vias,
        "policy_decision": decision.get("decision", "NONE"),
        "policy_reason": decision.get("reason"),
        "policy_id": decision.get("policy_id"),
    }
    out: list[dict[str, Any]] = []
    for order, task in enumerate(plan.tasks):
        o = result.outcomes.get(task.net)
        if o is None:
            continue
        out.append(
            {
                "schema": SCHEMA,
                "app": pcbrouter.__version__,
                "router": router,
                "source": source,
                "board": bid,
                "board_status": status,
                "board_f": bf,
                "board_p": bp,
                "job": job,
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
                    "bucket": _bucket(task, layers, bp.get("demand")),
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
                    "policy": policy_name,
                    "trace": _trace(o),
                },
            }
        )
    return out


class ExperienceLog:
    """Append-only JSONL with a size cap (thread- and process-tolerant appends)."""

    def __init__(
        self,
        folder: Path | None = None,
        max_bytes: int = MAX_BYTES,
        keep_per_stratum: int = KEEP_PER_STRATUM,
    ) -> None:
        self.folder = folder or default_dir()
        self.path = self.folder / "experience.jsonl"
        #: rotated-out records kept per stratum (see :meth:`_retain`)
        self.keep_path = self.folder / "experience.keep.jsonl"
        self.max_bytes = max_bytes
        self.keep_per_stratum = keep_per_stratum

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
                    old = self.path.with_suffix(".jsonl.1")
                    if old.exists():
                        self._retain(old)  # before its records are dropped
                    os.replace(self.path, old)
            except FileNotFoundError:
                pass
            with self.path.open("ab") as fh:
                fh.write(data)
        return len(lines)

    @staticmethod
    def stratum(r: dict[str, Any]) -> str:
        nf = r.get("net_f") or {}
        mode = (r.get("settings") or {}).get("mode", "?")
        return f"{mode}|{nf.get('bucket') or nf.get('kind', '?')}"

    def _retain(self, dropped: Path) -> None:
        """Stratified retention: before *dropped* is deleted by rotation, keep the
        newest ``keep_per_stratum`` records of every stratum (mode + context
        bucket) from it and the existing keep file. Pure FIFO would lose rare
        strata (diff pairs, big power nets) first. The keep file is bounded to
        half the log cap by lowering the per-stratum count until it fits."""
        per: dict[str, list[str]] = {}
        for p in (self.keep_path, dropped):  # oldest first, so newest end last
            if not p.exists():
                continue
            with p.open(encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    per.setdefault(self.stratum(r), []).append(line.rstrip("\n"))
        n = self.keep_per_stratum
        while True:
            kept = [line for lines in per.values() for line in lines[-n:]]
            size = sum(len(x.encode()) + 1 for x in kept)
            if size <= self.max_bytes // 2 or n <= 1:
                break
            n = max(1, n // 2)
        tmp = self.keep_path.with_suffix(".tmp")
        tmp.write_text("".join(x + "\n" for x in kept), encoding="utf-8")
        os.replace(tmp, self.keep_path)

    def files(self) -> tuple[Path, ...]:
        """The log files in read order: keep file, rotated file, current file."""
        return (self.keep_path, self.path.with_suffix(".jsonl.1"), self.path)

    def read(self) -> Iterator[dict[str, Any]]:
        for p in self.files():
            yield from _read_jsonl(p)


def _read_jsonl(path: Path) -> Iterator[Any]:
    """Parsed lines of *path* (absent file: nothing). Blank lines and lines that
    are not valid UTF-8 JSON (a line torn by a crash) are skipped."""
    if not path.exists():
        return
    with path.open("rb") as fh:
        for raw in fh:
            if raw.strip():
                try:
                    yield json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue  # a torn last line after a crash


def _label(value: Any) -> str:
    return "unknown" if value is None else str(value)


def _readable(paths: Iterable[Path], warnings: list[str]) -> Iterator[Any]:
    """Lines of every file in *paths*; a file that cannot be read (permissions,
    I/O error) is reported in *warnings* and the rest are still read."""
    for path in paths:
        try:
            yield from _read_jsonl(path)
        except (OSError, ValueError) as exc:
            warnings.append(f"{path.name} could not be read completely: {exc}")
            log.warning("experience.export_read_failed file=%s", path.name)


def export_bundle(
    dest: Path, folder: Path | None = None, *, overwrite: bool = False
) -> dict[str, Any]:
    """Write the experience log in *folder* to the zip *dest*, for the user to
    share if they choose. Nothing is sent anywhere.

    The zip holds ``records.jsonl`` (every readable record of a known schema,
    compact JSON with sorted keys) and ``manifest.json`` (``EXPORT_SCHEMA``,
    counts, the app and router versions). The ``salt`` file is never included,
    so board ids cannot be matched to boards. A partial or corrupt log never
    raises: unreadable lines and files are skipped. Raises ``OSError`` only when
    *dest* cannot be written, including ``FileExistsError`` when it exists and
    *overwrite* is false. Returns the manifest plus ``path``, ``bytes``,
    ``empty`` and ``warnings``.
    """
    from pcbrouter.learning.policy import router_signature

    dest = Path(dest)
    if dest.exists() and not overwrite:
        raise FileExistsError(f"{dest} already exists")
    log_ = ExperienceLog(folder)
    counts: dict[str, Counter[str]] = {k: Counter() for k in ("schema", "app", "mode", "source")}
    boards: set[str] = set()
    total = skipped = 0
    warnings: list[str] = []
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            with zf.open("records.jsonl", "w", force_zip64=True) as out:
                for r in _readable(log_.files(), warnings):
                    if not isinstance(r, dict) or r.get("schema") not in KNOWN_SCHEMAS:
                        skipped += 1
                        continue
                    line = json.dumps(r, sort_keys=True, separators=(",", ":"))
                    out.write(line.encode("utf-8") + b"\n")
                    total += 1
                    settings = r.get("settings")
                    mode = settings.get("mode") if isinstance(settings, dict) else None
                    counts["schema"][_label(r.get("schema"))] += 1
                    counts["app"][_label(r.get("app"))] += 1
                    counts["mode"][_label(mode)] += 1
                    counts["source"][_label(r.get("source"))] += 1
                    if r.get("board") is not None:
                        boards.add(str(r["board"]))
            manifest: dict[str, Any] = {
                "schema": EXPORT_SCHEMA,
                "created": datetime.now(UTC).isoformat(timespec="seconds"),
                "app": pcbrouter.__version__,
                "router": router_signature(),
                "records": total,
                "skipped": skipped,
                "by_schema": dict(sorted(counts["schema"].items())),
                "by_app": dict(sorted(counts["app"].items())),
                "by_mode": dict(sorted(counts["mode"].items())),
                "by_source": dict(sorted(counts["source"].items())),
                "boards": len(boards),
                "contents": EXPORT_CONTENTS,
            }
            zf.writestr("manifest.json", json.dumps(manifest, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, dest)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
    if total == 0:
        warnings.append("the experience log has no records yet: the file holds 0 records")
    return {
        **manifest,
        "path": str(dest),
        "bytes": dest.stat().st_size,
        "empty": total == 0,
        "warnings": warnings,
    }


def read_bundle(path: Path) -> Iterator[dict[str, Any]]:
    """Records of an export bundle written by :func:`export_bundle`. Raises
    ``ValueError`` when *path* is not a bundle (bad zip, missing or foreign
    manifest); torn or non-object lines are skipped."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{path} is not an experience export (not a zip file)") from exc
    with zf:
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except (KeyError, ValueError, zipfile.BadZipFile, zlib.error) as exc:
            raise ValueError(f"{path} is not an experience export (no manifest)") from exc
        schema = manifest.get("schema") if isinstance(manifest, dict) else None
        if schema != EXPORT_SCHEMA:
            raise ValueError(f"{path}: unsupported export schema {schema!r}")
        try:
            fh = zf.open("records.jsonl")
        except KeyError as exc:
            raise ValueError(f"{path} is not an experience export (no records)") from exc
        try:
            with fh:
                for raw in fh:
                    if not raw.strip():
                        continue
                    try:
                        r = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(r, dict):
                        yield r
        except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
            raise ValueError(f"{path}: damaged export ({exc})") from exc


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
