"""DAVESROUTER Benchmark Suite — 100 Real KiCad Boards (and other pinned corpora).

The default corpus (Real100) is a reproducible manifest of 80 KiCad PCBNew
QA/regression boards and 20 official KiCad demo designs. Third-party board bytes
are fetched on demand from a pinned KiCad source-mirror commit; DAVESROUTER does
not vendor those files.

The same harness runs any manifest given with ``--manifest``: the OpenBoards
corpus (``benchmarks/openboards/``, schema ``davesrouter-openboards/1``) pins
open-source KiCad projects from many repositories, each board (and each sidecar
``.kicad_pro``/``.kicad_dru``) by repository, commit and Git blob SHA-1.

The runner deliberately executes each board/mode in a fresh subprocess. A parser
crash, pathological router case, or timeout is recorded as one result and cannot
kill the remaining corpus run.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pcbrouter.board_engine import EngineConfig
from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.routing.board_router import BoardRouter, BoardRouterSettings
from pcbrouter.routing.presets import RouteMode, adjust_board_settings
from pcbrouter.routing.request import RouteRequest
from pcbrouter.routing.working_board import WorkingBoard

SCHEMA = "davesrouter-real100/1.1"
#: manifests this harness reads (1.1 = same boards, documents the strip policy)
REAL100_SCHEMAS = frozenset({"davesrouter-real100/1", SCHEMA})
OPENBOARDS_SCHEMA = "davesrouter-openboards/1"
SCHEMAS = REAL100_SCHEMAS | {OPENBOARDS_SCHEMA}
USER_AGENT = "DAVESROUTER-Real100/1.1 (+benchmark corpus fetcher)"
RAW_HOST = "https://raw.githubusercontent.com"
RAW_BASE = f"{RAW_HOST}/KiCad/kicad-source-mirror"
#: boards the ``smoke`` profile routes on a manifest without a curated smoke set
SMOKE_FALLBACK_COUNT = 12


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_manifest() -> Path:
    return repo_root() / "benchmarks" / "real100" / "manifest.json"


def default_workdir() -> Path:
    return repo_root() / "benchmarks" / "real100" / "work"


@dataclass(frozen=True, slots=True)
class BoardSpec:
    id: str
    name: str
    family: str
    source_path: str
    source_size_bytes: int
    git_blob_sha1: str
    difficulty: str
    tags: tuple[str, ...]
    route_policy: str
    #: per-board source; None = the manifest's ``source_repository``/``source_ref``
    repository: str | None = None
    ref: str | None = None
    #: pinned sidecars as (repository path, git blob sha1); empty = best effort
    sidecars: tuple[tuple[str, str], ...] = ()
    license: str | None = None
    #: whether the original (unstripped) board was fully connected; None = unknown
    reference_complete: bool | None = None


@dataclass(frozen=True, slots=True)
class Manifest:
    suite_name: str
    suite_version: str
    source_repository: str
    source_ref: str
    source_ref_date: str
    boards: tuple[BoardSpec, ...]
    schema: str = SCHEMA

    @property
    def is_real100(self) -> bool:
        return self.schema in REAL100_SCHEMAS

    @property
    def slug(self) -> str:
        """Short corpus name used in result file names (``real100``, ``openboards``)."""
        return self.schema.split("/", 1)[0].removeprefix("davesrouter-")

    def board_repository(self, spec: BoardSpec) -> str:
        return spec.repository or self.source_repository

    def board_ref(self, spec: BoardSpec) -> str:
        return spec.ref or self.source_ref


def load_manifest(path: Path | None = None) -> Manifest:
    p = path or default_manifest()
    data = json.loads(p.read_text(encoding="utf-8"))
    schema = data.get("schema")
    if schema not in SCHEMAS:
        raise ValueError(f"unsupported benchmark manifest schema: {schema!r}")
    source_repository = str(data.get("source_repository") or "")
    source_ref = str(data.get("source_ref") or "")
    boards = tuple(_board_spec(x, source_repository, source_ref) for x in data["boards"])
    ids = {b.id for b in boards}
    if schema in REAL100_SCHEMAS:
        if len(boards) != 100 or len(ids) != 100:
            raise ValueError("Real100 manifest must contain exactly 100 unique board IDs")
    elif not boards or len(ids) != len(boards):
        raise ValueError("benchmark manifest needs at least one board and unique board IDs")
    return Manifest(
        suite_name=str(data["suite_name"]),
        suite_version=str(data["suite_version"]),
        source_repository=source_repository,
        source_ref=source_ref,
        source_ref_date=str(data.get("source_ref_date") or ""),
        boards=boards,
        schema=str(schema),
    )


def _board_spec(x: dict[str, Any], source_repository: str, source_ref: str) -> BoardSpec:
    source_path = str(x["source_path"])
    repository = x.get("repository") or source_repository
    ref = x.get("commit") or x.get("ref") or source_ref
    if not repository or not ref:
        raise ValueError(f"board {x.get('id')!r}: no repository/commit (and no manifest default)")
    sidecars = tuple((str(sc["path"]), str(sc["git_blob_sha1"])) for sc in x.get("sidecars") or ())
    complete = x.get("reference_complete")
    return BoardSpec(
        id=str(x["id"]),
        name=str(x.get("name") or source_path.rsplit("/", 1)[-1]),
        family=str(x.get("family", "")),
        source_path=source_path,
        source_size_bytes=int(x["source_size_bytes"]),
        git_blob_sha1=str(x["git_blob_sha1"]),
        difficulty=str(x.get("difficulty", "")),
        tags=tuple(str(t) for t in x.get("tags", ())),
        route_policy=str(x.get("route_policy", "route")),
        repository=str(repository),
        ref=str(ref),
        sidecars=sidecars,
        license=str(x["license"]) if x.get("license") else None,
        reference_complete=None if complete is None else bool(complete),
    )


def git_blob_sha1(data: bytes) -> str:
    prefix = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(prefix + data).hexdigest()


def _download(url: str, *, timeout: float = 120.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data: bytes = response.read()
        return data


def repo_slug(repository: str) -> str:
    """``owner/name`` from ``owner/name``, ``https://github.com/owner/name(.git)``."""
    slug = repository.strip().rstrip("/")
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if slug.startswith(prefix):
            slug = slug[len(prefix) :]
    slug = slug.removesuffix(".git")
    if slug.count("/") != 1:
        raise ValueError(f"not a GitHub repository: {repository!r}")
    return slug


def _raw_url(repository: str, ref: str, source_path: str) -> str:
    quoted = urllib.parse.quote(source_path, safe="/")
    return f"{RAW_HOST}/{repo_slug(repository)}/{ref}/{quoted}"


def _entry_dir(workdir: Path, stage: str, spec: BoardSpec) -> Path:
    return workdir / stage / spec.id


def fetch_board(
    spec: BoardSpec, manifest: Manifest, workdir: Path, *, force: bool = False
) -> dict[str, Any]:
    dest_dir = _entry_dir(workdir, "downloads", spec)
    dest_dir.mkdir(parents=True, exist_ok=True)
    board_dest = dest_dir / spec.name
    result: dict[str, Any] = {"id": spec.id, "source_path": spec.source_path, "status": "ok"}

    if board_dest.exists() and not force:
        data = board_dest.read_bytes()
        observed = git_blob_sha1(data)
        if observed == spec.git_blob_sha1:
            result.update({"cached": True, "bytes": len(data), "git_blob_sha1": observed})
        else:
            board_dest.unlink()
    repository, ref = manifest.board_repository(spec), manifest.board_ref(spec)
    if not board_dest.exists():
        url = _raw_url(repository, ref, spec.source_path)
        data = _download(url, timeout=300.0 if spec.source_size_bytes > 20_000_000 else 120.0)
        observed = git_blob_sha1(data)
        if observed != spec.git_blob_sha1:
            raise RuntimeError(
                f"{spec.id}: Git blob SHA mismatch: expected {spec.git_blob_sha1}, got {observed}"
            )
        if len(data) != spec.source_size_bytes:
            # Size is advisory metadata; blob identity is the authoritative integrity check.
            result["size_note"] = f"manifest {spec.source_size_bytes}, fetched {len(data)}"
        board_dest.write_bytes(data)
        result.update({"cached": False, "bytes": len(data), "git_blob_sha1": observed})

    if spec.sidecars:
        result["sidecars"] = _fetch_pinned_sidecars(spec, repository, ref, dest_dir, force)
        return result
    # KiCad 6+ rules usually live beside the board. Fetch same-stem sidecars when present.
    sidecars: list[str] = []
    src = Path(spec.source_path)
    for suffix in (".kicad_pro", ".kicad_dru"):
        side_source = str(src.with_suffix(suffix)).replace("\\", "/")
        side_dest = dest_dir / Path(side_source).name
        if side_dest.exists() and not force:
            sidecars.append(side_dest.name)
            continue
        try:
            side_data = _download(_raw_url(repository, ref, side_source), timeout=60.0)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        side_dest.write_bytes(side_data)
        sidecars.append(side_dest.name)
    result["sidecars"] = sidecars
    return result


def _fetch_pinned_sidecars(
    spec: BoardSpec, repository: str, ref: str, dest_dir: Path, force: bool
) -> list[str]:
    """Fetch exactly the manifest's sidecars, each verified against its blob SHA."""
    names: list[str] = []
    for side_source, expected in spec.sidecars:
        side_dest = dest_dir / Path(side_source).name
        if side_dest.exists() and not force:
            if git_blob_sha1(side_dest.read_bytes()) == expected:
                names.append(side_dest.name)
                continue
            side_dest.unlink()
        side_data = _download(_raw_url(repository, ref, side_source), timeout=60.0)
        observed = git_blob_sha1(side_data)
        if observed != expected:
            raise RuntimeError(
                f"{spec.id}: sidecar {side_source} Git blob SHA mismatch: "
                f"expected {expected}, got {observed}"
            )
        side_dest.write_bytes(side_data)
        names.append(side_dest.name)
    return names


def fetch_all(manifest: Manifest, workdir: Path, *, force: bool = False) -> list[dict[str, Any]]:
    workdir.mkdir(parents=True, exist_ok=True)
    out: list[dict[str, Any]] = []
    for i, spec in enumerate(manifest.boards, 1):
        print(f"[{i:03d}/{len(manifest.boards)}] fetch {spec.id} {spec.name}", flush=True)
        try:
            out.append(fetch_board(spec, manifest, workdir, force=force))
        except Exception as exc:  # one unavailable board must not hide the rest
            out.append(
                {
                    "id": spec.id,
                    "source_path": spec.source_path,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    path = workdir / "fetch_status.json"
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return out


ROUTE_SYMBOLS = frozenset({"segment", "via", "arc"})
#: KiCad 7+ teardrop zones: generated from tracks, so they go with them
_TEARDROP_RE = re.compile(r"\(\s*attr\s*\(\s*teardrop\b")


def strip_routing_copper(text: str) -> tuple[str, dict[str, int]]:
    """Remove only top-level routed copper: ``(segment …)``, ``(via …)`` and
    copper-track ``(arc …)`` children of the outer ``(kicad_pcb …)`` form, plus
    teardrop zones (``(zone … (attr (teardrop …)))``): KiCad generates those from
    the tracks, and left behind they are orphan copper blobs on pads that new
    routes join at odd angles (Real100 K035: 339 of them; KiCad then flags
    connection_width necks). Pack builder 1.2.

    Footprints, pads, zones, keepouts, graphics (``gr_arc`` is a different
    symbol), board outline, rules, net table, setup, groups and every unknown
    construct stay byte-for-byte unchanged. The scanner respects strings, escapes
    and ``;`` comments, and consumes the removed object's trailing blanks and one
    line break, so no blank lines are left behind.
    """
    if "(kicad_pcb" not in text:
        raise ValueError("not a KiCad board")
    counts = {"segment": 0, "via": 0, "arc": 0, "teardrop": 0}
    out: list[str] = []
    last = depth = 0
    in_string = escaped = in_comment = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_comment:
            in_comment = ch not in "\r\n"
            i += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == ";":
            in_comment = True
        elif ch == '"':
            in_string = True
        elif ch == "(":
            sym = _head_symbol(text, i) if depth == 1 else ""
            end = -1
            if sym == "zone":
                end = _matching_paren(text, i)
                if _TEARDROP_RE.search(text, i, end):
                    sym = "teardrop"
            if sym in ROUTE_SYMBOLS or sym == "teardrop":
                if end < 0:
                    end = _matching_paren(text, i)
                out.append(text[last:i])
                j = end + 1
                while j < n and text[j] in " \t":
                    j += 1
                if text.startswith("\r\n", j):
                    j += 2
                elif j < n and text[j] in "\r\n":
                    j += 1
                last = i = j
                counts[sym] += 1
                continue
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("malformed KiCad board: unexpected ')'")
        i += 1
    if depth != 0 or in_string:
        raise ValueError("malformed KiCad board: unbalanced parentheses or string")
    out.append(text[last:])
    return "".join(out), counts


def _head_symbol(text: str, open_paren: int) -> str:
    i = open_paren + 1
    n = len(text)
    while i < n and text[i].isspace():
        i += 1
    j = i
    while j < n and not text[j].isspace() and text[j] not in "()":
        j += 1
    return text[i:j]


def _matching_paren(text: str, start: int) -> int:
    """Index of the ``)`` closing ``text[start] == "("`` (strings/comments aware)."""
    depth = 0
    in_string = escaped = in_comment = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_comment:
            in_comment = ch not in "\r\n"
        elif in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == ";":
            in_comment = True
        elif ch == '"':
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError(f"unbalanced S-expression starting at character {start}")


def prepare_board(spec: BoardSpec, workdir: Path, *, force: bool = False) -> dict[str, Any]:
    src_dir = _entry_dir(workdir, "downloads", spec)
    src = src_dir / spec.name
    if not src.is_file():
        return {"id": spec.id, "status": "missing", "error": "run fetch first"}
    dest_dir = _entry_dir(workdir, "prepared", spec)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / spec.name
    if dest.exists() and (dest_dir / "SOURCE.json").exists() and not force:
        return {"id": spec.id, "status": "ok", "cached": True, "path": str(dest)}
    raw = src.read_bytes()
    if git_blob_sha1(raw) != spec.git_blob_sha1:  # never derive from an unverified source
        raise RuntimeError(f"{spec.id}: downloaded board does not match its pinned Git blob")
    stripped, counts = strip_routing_copper(raw.decode("utf-8-sig"))
    again, leftovers = strip_routing_copper(stripped)
    if any(leftovers.values()) or again != stripped:
        raise RuntimeError(f"{spec.id}: route stripping is not idempotent: {leftovers}")
    data = stripped.encode("utf-8")
    dest.write_bytes(data)
    sidecars = []
    if spec.sidecars:
        side_names = [Path(p).name for p, _sha in spec.sidecars]
    else:
        side_names = [src.with_suffix(suffix).name for suffix in (".kicad_pro", ".kicad_dru")]
    for name in side_names:
        side = src_dir / name
        if side.is_file():
            shutil.copy2(side, dest_dir / side.name)
            sidecars.append(side.name)
    meta = {
        "id": spec.id,
        "source_path": spec.source_path,
        "git_blob_sha1": spec.git_blob_sha1,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "unrouted_sha256": hashlib.sha256(data).hexdigest(),
        "transform": {"name": "strip_routing_copper", "removed": counts, "zones_preserved": True},
        "sidecars": sidecars,
    }
    (dest_dir / "SOURCE.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return {
        "id": spec.id,
        "status": "ok",
        "cached": False,
        "path": str(dest),
        "removed_segments": counts["segment"],
        "removed_vias": counts["via"],
        "removed_track_arcs": counts["arc"],
        "removed_teardrops": counts["teardrop"],
        "sidecars": sidecars,
        "sha256": meta["unrouted_sha256"],
    }


def _write_rules_inventory(rows: list[dict[str, Any]], path: Path) -> None:
    keys = ["id", "status", "removed_segments", "removed_vias", "removed_track_arcs",
            "removed_teardrops", "sidecars", "sha256", "error"]  # fmt: skip
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({**r, "sidecars": ";".join(r.get("sidecars") or [])})


def prepare_all(manifest: Manifest, workdir: Path, *, force: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, spec in enumerate(manifest.boards, 1):
        print(f"[{i:03d}/{len(manifest.boards)}] prepare {spec.id} {spec.name}", flush=True)
        try:
            out.append(prepare_board(spec, workdir, force=force))
        except Exception as exc:
            out.append({"id": spec.id, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
    (workdir / "prepare_status.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    _write_rules_inventory(out, workdir / "rules_inventory.csv")
    return out


def _stats_dict(board: Any) -> dict[str, Any]:
    s = board.statistics
    return {
        "footprints": s.footprint_count,
        "pads": s.pad_count,
        "nets": s.net_count,
        "tracks": s.track_count,
        "arc_tracks": s.arc_track_count,
        "vias": s.via_count,
        "copper_layers": s.copper_layer_count,
        "zones": s.zone_count,
        "keepouts": s.keepout_count,
        "net_classes": s.net_class_count,
        "width_mm": round(s.width / 1e6, 3) if s.width is not None else None,
        "height_mm": round(s.height / 1e6, 3) if s.height is not None else None,
    }


def inventory_board(spec: BoardSpec, workdir: Path) -> dict[str, Any]:
    path = _entry_dir(workdir, "prepared", spec) / spec.name
    if not path.is_file():
        return {"id": spec.id, "status": "missing", "error": "run fetch + prepare first"}
    t0 = time.perf_counter()
    loaded = load_board(path)
    rules = load_project_rules(path)
    elapsed = time.perf_counter() - t0
    return {
        "id": spec.id,
        "status": "ok",
        "name": spec.name,
        "family": spec.family,
        "difficulty": spec.difficulty,
        "tags": list(spec.tags),
        "source_size_bytes": spec.source_size_bytes,
        "prepared_size_bytes": path.stat().st_size,
        "load_s": round(elapsed, 6),
        "board_sha256": loaded.stats.sha256,
        "format_version": loaded.board.metadata.format_version,
        "generator": loaded.board.metadata.generator,
        "generator_version": loaded.board.metadata.generator_version,
        "load_warnings": [getattr(w, "message", str(w)) for w in loaded.warnings],
        "project_rules_found": rules.found_any,
        "rule_warnings": list(rules.warnings),
        **_stats_dict(loaded.board),
    }


def inventory_all(manifest: Manifest, workdir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, spec in enumerate(manifest.boards, 1):
        print(f"[{i:03d}/{len(manifest.boards)}] inventory {spec.id} {spec.name}", flush=True)
        try:
            out.append(inventory_board(spec, workdir))
        except Exception as exc:
            out.append({"id": spec.id, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
    path = workdir / "inventory.json"
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    _write_inventory_csv(out, workdir / "inventory.csv")
    return out


def _write_inventory_csv(rows: list[dict[str, Any]], path: Path) -> None:
    keys = [
        "id",
        "status",
        "name",
        "family",
        "difficulty",
        "source_size_bytes",
        "prepared_size_bytes",
        "load_s",
        "format_version",
        "footprints",
        "pads",
        "nets",
        "tracks",
        "vias",
        "copper_layers",
        "zones",
        "keepouts",
        "project_rules_found",
        "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def route_one_board(
    board_path: Path,
    mode: RouteMode,
    *,
    budget_s: float,
    conservative: bool = True,
    experience_dir: Path | None = None,
    policy: str | None = None,
    check_validity: bool = False,
    save_routed: Path | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    loaded = load_board(board_path)
    rules = load_project_rules(board_path)
    wb = WorkingBoard(loaded.board, rules, config=EngineConfig(conservative=conservative))
    # the hard timeout covers loading too: route only in what is left, keeping a
    # reserve for the post-route connectivity check (big boards load for 20 s)
    load_s = time.perf_counter() - started
    budget_s = max(0.1, (budget_s - load_s) * 0.9)
    base = RouteRequest(
        "",
        candidates=1,
        time_limit_s=min(30.0, max(1.0, budget_s)),
        total_time_limit_s=None,
    )
    settings = BoardRouterSettings(budget_s=max(0.1, budget_s), base_request=base)
    settings = adjust_board_settings(settings, base, mode)
    if policy:
        from dataclasses import replace as _replace

        from pcbrouter.learning.policy import policy_from_spec

        settings = _replace(settings, policy=policy_from_spec(policy))
    pre = wb.engine.connectivity.metrics()
    t_route = time.perf_counter()
    result = BoardRouter(wb, settings).run()
    routing_s = time.perf_counter() - t_route
    if experience_dir is not None:
        from pcbrouter.learning.experience import record_board_job

        record_board_job(result, settings, folder=experience_dir, source="real100")
    post_wb = WorkingBoard(
        result.final_board, rules, config=EngineConfig(conservative=conservative)
    )
    post = post_wb.engine.connectivity.metrics()
    validity: dict[str, Any] = {}
    if check_validity:
        # belt and braces: every commit was already checked by the exact
        # validator; this re-checks the finished board's new copper with DRC
        t_drc = time.perf_counter()
        added = {f"track:{t.id}" for t in result.added_tracks}
        added |= {f"via:{v.id}" for v in result.added_vias} | {
            f"hole:{v.id}" for v in result.added_vias
        }
        errors = post_wb.engine.run_drc().errors
        bad = [v for v in errors if v.object_a in added or v.object_b in added]
        validity = {
            "new_copper_drc_errors": len(bad),
            "new_copper_drc_examples": [v.message[:160] for v in bad[:5]],
            "drc_s": round(time.perf_counter() - t_drc, 2),
        }
    saved: dict[str, Any] = {}
    if save_routed is not None:
        saved = _save_routed(board_path, loaded.board, result, save_routed)
    return {
        **validity,
        **saved,
        "endgame": result.endgame,
        "routing_s": round(routing_s, 3),
        **_peak_rss(),
        "policy_decision": result.policy_decision,
        "status": "ok",
        "route_status": result.status.value,
        "mode": mode.value,
        "runtime_s": round(time.perf_counter() - started, 6),
        "router_summary": result.summary(),
        "load_s": round(load_s, 3),
        "route_budget_s": round(budget_s, 3),
        "failure_reasons": dict(
            Counter(
                (o.reason.value if o.reason else o.status.value)
                for o in result.outcomes.values()
                if o.status.value != "SUCCESS"
            )
        ),
        "failed_examples": [
            f"{o.net}: {(o.message or '')[:240]}"
            for o in result.outcomes.values()
            if o.status.value != "SUCCESS"
        ][:8],
        "metrics": result.metrics.to_dict(),
        "pre_connectivity": pre,
        "post_connectivity": post,
        "board": _stats_dict(loaded.board),
        "load_warning_count": len(loaded.warnings),
        "rule_warning_count": len(rules.warnings),
        "project_rules_found": rules.found_any,
        "conservative_rules": conservative,
    }


def _peak_rss() -> dict[str, Any]:
    """Peak resident memory of this worker and of its finished helper processes
    (MB; ``ru_maxrss`` is KiB on Linux, bytes on macOS; absent on Windows)."""
    try:
        import resource
    except ImportError:  # Windows
        return {}
    scale = 1024 * 1024 if sys.platform == "darwin" else 1024
    own = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    kids = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return {
        "peak_rss_mb": round(own * 1024 / scale / 1024, 1),
        "helpers_peak_rss_mb": round(kids * 1024 / scale / 1024, 1),
    }


def _save_routed(board_path: Path, source: Any, result: Any, prefix: Path) -> dict[str, Any]:
    """Export the routed board as ``<prefix>.kicad_pcb`` (the source is never
    touched) plus ``<prefix>.generated.json``: the UUIDs of every object the
    router added and the nets it reports finished, for the KiCad DRC oracle."""
    import hashlib

    from pcbrouter.kicad.writer import export_board

    t0 = time.perf_counter()
    prefix.parent.mkdir(parents=True, exist_ok=True)
    out = prefix.with_name(prefix.name + ".kicad_pcb")
    sha = hashlib.sha256(board_path.read_bytes()).hexdigest()
    report = export_board(board_path, sha, source, result.final_board, out)
    generated = sorted({t.id for t in result.added_tracks} | {v.id for v in result.added_vias})
    finished = sorted(n for n, o in result.outcomes.items() if o.status.value == "SUCCESS")
    meta = {
        "source": str(board_path),
        "source_sha256": sha,
        "export_status": report.status.value,
        "generated": generated,
        "finished_nets": finished,
    }
    prefix.with_name(prefix.name + ".generated.json").write_text(
        json.dumps(meta, indent=1) + "\n", encoding="utf-8"
    )
    return {
        "routed_board": str(out) if report.ok else None,
        "export_status": report.status.value,
        "export_s": round(time.perf_counter() - t0, 2),
    }


def board_hash(board_path: Path, salt: str) -> str:
    """The salted board id the experience log writes for *board_path*
    (:func:`pcbrouter.learning.experience.board_id` of the loaded board's
    fingerprint), so benchmark boards can be matched to experience records."""
    from pcbrouter.learning.experience import board_id

    return board_id(load_board(board_path).board.fingerprint, salt)


SMOKE_IDS = (
    "K001",
    "K002",
    "K003",
    "K009",
    "K022",
    "K035",
    "K067",
    "K081",
    "K084",
    "K090",
    "K096",
    "K099",
)


def select_specs(manifest: Manifest, profile: str, ids: set[str] | None = None) -> list[BoardSpec]:
    specs = list(manifest.boards)
    if ids:
        specs = [s for s in specs if s.id in ids]
        missing = ids - {s.id for s in specs}
        if missing:
            raise ValueError(f"unknown board IDs: {', '.join(sorted(missing))}")
        return specs
    if profile == "smoke":
        if not manifest.is_real100:  # SMOKE_IDS name Real100 boards only
            return specs[:SMOKE_FALLBACK_COUNT]
        wanted = set(SMOKE_IDS)
        return [s for s in specs if s.id in wanted]
    if profile == "standard":
        return [s for s in specs if s.route_policy != "stress"]
    if profile == "full":
        return specs
    raise ValueError(f"unknown profile {profile!r}")


ROUTABLE_SCHEMA = "davesrouter-routable/1"


def routable_path(manifest_path: Path | None) -> Path:
    """The pruned board list lives beside its manifest (``routable.json``)."""
    return (manifest_path or default_manifest()).with_name("routable.json")


def classify_boards(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Which boards are worth routing, from baseline results (any modes/runs).

    A board is **excluded** only for reasons the router is right to stop at:

    * ``nothing_to_route`` - no net has two or more unconnected pads;
    * ``rules_refused`` - not one net routed and every failure is RULE_UNKNOWN
      (no project rules, or a rule the conservative engine cannot assume, such
      as a missing copper-to-edge clearance).

    Everything else stays, including crashes and timeouts (those are bugs to
    investigate, never pruned)."""
    by: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(str(r["id"]), []).append(r)
    out: dict[str, dict[str, Any]] = {}
    for bid, rs in sorted(by.items()):
        ok = [r for r in rs if r.get("status") == "ok"]
        modes = {str(r.get("mode")): r.get("route_status") for r in rs}
        reason = None
        if ok and len(ok) == len(rs):
            if all(r.get("route_status") == "NOTHING_TO_ROUTE" for r in ok):
                reason = "nothing_to_route"
            else:
                done = sum(int((r.get("metrics") or {}).get("nets_completed", 0)) for r in ok)
                fails = set().union(*(set(r.get("failure_reasons") or {}) for r in ok))
                if done == 0 and fails and fails <= {"RULE_UNKNOWN"}:
                    rules = all(r.get("project_rules_found") for r in ok)
                    reason = "rules_refused" + ("" if rules else " (no project rules)")
        out[bid] = {"routable": reason is None, "reason": reason, "modes": modes}
    return out


def write_routable(
    rows: list[dict[str, Any]], manifest: Manifest, manifest_path: Path | None, sources: list[str]
) -> tuple[Path, dict[str, dict[str, Any]]]:
    boards = classify_boards(rows)
    known = {b.id for b in manifest.boards}
    unknown = sorted(set(boards) - known)
    if unknown:
        raise ValueError(f"results name boards not in this manifest: {', '.join(unknown)}")
    path = routable_path(manifest_path)
    data = {
        "schema": ROUTABLE_SCHEMA,
        "suite_name": manifest.suite_name,
        "sources": sources,
        "routable": sorted(b for b, v in boards.items() if v["routable"]),
        "excluded": {b: v["reason"] for b, v in sorted(boards.items()) if not v["routable"]},
        "not_measured": sorted(known - set(boards)),
    }
    path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    return path, boards


def load_routable(manifest_path: Path | None) -> set[str]:
    path = routable_path(manifest_path)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found: run 'prune' on baseline results first")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != ROUTABLE_SCHEMA:
        raise ValueError(f"{path}: unsupported schema {data.get('schema')!r}")
    return set(data["routable"])


def profile_defaults(profile: str) -> tuple[tuple[RouteMode, ...], float]:
    if profile == "smoke":
        return (RouteMode.SPEED,), 45.0
    if profile in ("standard", "routable"):
        return (RouteMode.SPEED, RouteMode.ACCURACY), 180.0
    if profile == "full":
        return (RouteMode.SPEED, RouteMode.ACCURACY), 900.0
    raise ValueError(profile)


def _worker_command(
    board: Path,
    mode: RouteMode,
    budget_s: float,
    conservative: bool,
    experience_dir: Path | None = None,
    policy: str | None = None,
    check_validity: bool = False,
    save_routed: Path | None = None,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pcbrouter.benchmark.real100",
        "worker",
        "--board",
        str(board),
        "--mode",
        mode.value,
        "--budget",
        str(budget_s),
        "--conservative",
        "yes" if conservative else "no",
        *(["--experience", str(experience_dir)] if experience_dir is not None else []),
        *(["--policy", policy] if policy else []),
        *(["--check-validity"] if check_validity else []),
        *(["--save-routed", str(save_routed)] if save_routed is not None else []),
    ]


EXPORT_GRACE_S = 120.0


def run_corpus(
    manifest: Manifest,
    workdir: Path,
    *,
    profile: str,
    modes: tuple[RouteMode, ...] | None = None,
    timeout_s: float | None = None,
    ids: set[str] | None = None,
    max_boards: int | None = None,
    conservative: bool = True,
    experience_dir: Path | None = None,
    policy: str | None = None,
    check_validity: bool = False,
    out_path: Path | None = None,
    save_routed_dir: Path | None = None,
) -> Path:
    default_modes, default_timeout = profile_defaults(profile)
    modes = modes or default_modes
    timeout_s = float(timeout_s or default_timeout)
    specs = select_specs(manifest, profile, ids)
    if max_boards is not None:
        specs = specs[: max(0, max_boards)]
    runs_dir = workdir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    # the pid keeps runs started in the same second (side-by-side A/B) apart
    result_path = out_path or runs_dir / f"{manifest.slug}-{profile}-{stamp}-p{os.getpid()}.jsonl"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    if result_path.exists():
        raise FileExistsError(f"{result_path} exists; results are never appended to old runs")

    env = os.environ.copy()
    src = str(repo_root() / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    total = len(specs) * len(modes)
    index = 0
    with result_path.open("w", encoding="utf-8") as out:
        for spec in specs:
            board = _entry_dir(workdir, "prepared", spec) / spec.name
            for mode in modes:
                index += 1
                base = {
                    "schema": "davesrouter-real100-result/1",
                    "suite_version": manifest.suite_version,
                    "source_ref": manifest.board_ref(spec),
                    "profile": profile,
                    "id": spec.id,
                    "name": spec.name,
                    "family": spec.family,
                    "difficulty": spec.difficulty,
                    "tags": list(spec.tags),
                    "mode": mode.value,
                    "timeout_s": timeout_s,
                    "policy": policy or "fixed",
                    "timestamp_utc": datetime.now(UTC).isoformat(),
                }
                print(
                    f"[{index:03d}/{total:03d}] route {spec.id} {mode.value:<8} {spec.name}",
                    flush=True,
                )
                if not board.is_file():
                    record = {
                        **base,
                        "status": "missing",
                        "error": "prepared board missing; run fetch + prepare",
                    }
                else:
                    cmd = _worker_command(
                        board,
                        mode,
                        max(1.0, timeout_s - 5.0),
                        conservative,
                        experience_dir,
                        policy,
                        check_validity,
                        save_routed_dir / f"{spec.id}_{mode.value}" if save_routed_dir else None,
                    )
                    # exporting reloads the board: extra time outside the
                    # routing budget, so routing results stay comparable
                    hard = timeout_s + (EXPORT_GRACE_S if save_routed_dir else 0.0)
                    t0 = time.perf_counter()
                    try:
                        proc = subprocess.run(
                            cmd,
                            cwd=repo_root(),
                            env=env,
                            text=True,
                            capture_output=True,
                            timeout=hard,
                            check=False,
                        )
                        wall = time.perf_counter() - t0
                        if proc.returncode != 0:
                            record = {
                                **base,
                                "status": "worker_error",
                                "returncode": proc.returncode,
                                "wall_s": round(wall, 6),
                                "error": proc.stderr[-4000:] or proc.stdout[-4000:],
                            }
                        else:
                            try:
                                payload = json.loads(proc.stdout.strip().splitlines()[-1])
                            except Exception as exc:
                                record = {
                                    **base,
                                    "status": "worker_protocol_error",
                                    "wall_s": round(wall, 6),
                                    "error": f"{type(exc).__name__}: {exc}",
                                    "stdout_tail": proc.stdout[-2000:],
                                    "stderr_tail": proc.stderr[-2000:],
                                }
                            else:
                                record = {**base, **payload, "wall_s": round(wall, 6)}
                                if proc.stderr.strip():
                                    record["stderr_tail"] = proc.stderr[-2000:]
                    except subprocess.TimeoutExpired as exc:
                        wall = time.perf_counter() - t0
                        record = {
                            **base,
                            "status": "timeout",
                            "wall_s": round(wall, 6),
                            "error": f"hard subprocess timeout after {timeout_s:.1f}s",
                            "stdout_tail": (
                                (exc.stdout or "")[-1000:] if isinstance(exc.stdout, str) else ""
                            ),
                            "stderr_tail": (
                                (exc.stderr or "")[-1000:] if isinstance(exc.stderr, str) else ""
                            ),
                        }
                out.write(json.dumps(record, sort_keys=True) + "\n")
                out.flush()
    return result_path


def read_results(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def oracle_rows(
    rows: list[dict[str, Any]],
    tool: Any,
    variants: tuple[str, ...],
    workdir: Path,
) -> list[dict[str, Any]]:
    """KiCad-DRC every routed board the results saved (``run --save-routed``).
    A row that saved nothing is reported with the reason, never skipped."""
    from pcbrouter.kicad.oracle import oracle_check

    cache: dict[tuple[str, ...], Any] = {}
    out: list[dict[str, Any]] = []
    for r in rows:
        base = {k: r.get(k) for k in ("id", "name", "mode", "status", "route_status")}
        base["nets"] = [
            (r.get("metrics") or {}).get("nets_completed"),
            (r.get("metrics") or {}).get("nets_attempted"),
        ]
        routed = r.get("routed_board")
        if not routed or not Path(routed).is_file():
            why = r.get("export_status") or r.get("status") or "no routed board saved"
            out.append({**base, "oracle": {"status": "NOT_RUN", "reason": f"not saved: {why}"}})
            continue
        meta_path = Path(str(routed)[: -len(".kicad_pcb")] + ".generated.json")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        result = oracle_check(
            tool,
            Path(meta["source"]),
            Path(routed),
            set(meta["generated"]),
            workdir / f"{r.get('id')}_{r.get('mode')}",
            variants=variants,
            claimed_complete_nets=set(meta["finished_nets"]),
            cache=cache,
        )
        out.append({**base, "oracle": result})
        v = result.get("verdict_variant")
        res = result["variants"].get(v, {}) if v else {}
        print(
            f"oracle {r.get('id')} {r.get('mode'):<8} {result['status']:<24} "
            f"gen_err={res.get('new_generated_errors', '-')} "
            f"unc={res.get('unconnected_after', '-')} "
            f"disagree={len(res.get('completion_disagreements', []))}",
            flush=True,
        )
    return out


def run_oracle(args: argparse.Namespace) -> int:
    from pcbrouter.kicad.oracle import find_oracle

    tool = find_oracle(args.kicad_cli, args.kicad_python)
    if tool is None:
        print("KiCad DRC oracle unavailable: no kicad-cli with 'pcb drc' (KiCad 8+)")
        return 3
    print(f"KiCad {tool.version} ({tool.cli}); zone refill: {tool.refill}")
    if args.out.exists():
        raise FileExistsError(f"{args.out} exists; oracle results are never appended")
    rows = [r for path in args.results for r in read_results(path)]
    variants = tuple(v.strip() for v in args.variants.split(",") if v.strip())
    stage = args.out.with_suffix(".work")
    judged = oracle_rows(rows, tool, variants, stage)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for j in judged:
            fh.write(json.dumps(j, sort_keys=True) + "\n")
    counts = Counter(j["oracle"]["status"] for j in judged)
    print("oracle:", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    print(args.out)
    return 0


def _pct(n: int, d: int) -> str:
    return f"{(100.0*n/d):.1f}%" if d else "n/a"


def generate_report(results_path: Path, *, output_dir: Path | None = None) -> tuple[Path, Path]:
    rows = read_results(results_path)
    output_dir = output_dir or results_path.parent
    stem = results_path.stem
    csv_path = output_dir / f"{stem}.csv"
    md_path = output_dir / f"{stem}.md"

    csv_fields = [
        "id",
        "name",
        "family",
        "difficulty",
        "profile",
        "mode",
        "status",
        "route_status",
        "wall_s",
        "runtime_s",
        "nets_attempted",
        "nets_completed",
        "nets_failed",
        "completion_pct",
        "new_vias",
        "total_routed_length_mm",
        "expanded_nodes",
        "ripups",
        "reroutes",
        "passes",
        "clean_rate_pct",
        "project_rules_found",
        "conservative_rules",
        "error",
    ]
    flat_rows = []
    for r in rows:
        metrics = r.get("metrics") or {}
        flat_rows.append({**r, **metrics})
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=csv_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(flat_rows)

    ok = [r for r in rows if r.get("status") == "ok"]
    timeouts = [r for r in rows if r.get("status") == "timeout"]
    failures = [r for r in rows if r.get("status") not in {"ok", "timeout"}]
    routed = [r for r in ok if r.get("route_status") == "FULLY_ROUTED"]
    partial = [r for r in ok if r.get("route_status") == "PARTIALLY_ROUTED"]
    total_nets_attempted = sum(int((r.get("metrics") or {}).get("nets_attempted", 0)) for r in ok)
    total_nets_completed = sum(int((r.get("metrics") or {}).get("nets_completed", 0)) for r in ok)
    walls = [float(r["wall_s"]) for r in rows if isinstance(r.get("wall_s"), (int, float))]

    by_mode: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_mode.setdefault(str(r.get("mode", "?")), []).append(r)

    lines = [
        "# DAVESROUTER Real100 Benchmark Report",
        "",
        f"**Result file:** `{results_path.name}`  ",
        f"**Generated:** {datetime.now(UTC).isoformat()}  ",
        f"**Host:** {platform.platform()} · Python {platform.python_version()}  ",
        "",
        "## Aggregate",
        "",
        f"- Runs: **{len(rows)}**",
        f"- Worker-complete runs: **{len(ok)}** ({_pct(len(ok), len(rows))})",
        f"- Fully routed: **{len(routed)}**",
        f"- Partially routed: **{len(partial)}**",
        f"- Hard timeouts: **{len(timeouts)}**",
        f"- Worker/load/protocol failures: **{len(failures)}**",
        f"- Nets completed: **{total_nets_completed}/{total_nets_attempted}** "
        f"({_pct(total_nets_completed, total_nets_attempted)})",
    ]
    if walls:
        lines += [
            f"- Median wall time: **{statistics.median(walls):.2f}s**",
            f"- P95-ish wall time: "
            f"**{sorted(walls)[min(len(walls) - 1, int(0.95 * (len(walls) - 1)))]:.2f}s**",
        ]
    lines += [
        "",
        "## By mode",
        "",
        "| Mode | Runs | Complete workers | Fully routed | Timeouts | Net completion |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for mode, group in sorted(by_mode.items()):
        gok = [r for r in group if r.get("status") == "ok"]
        gfull = [r for r in gok if r.get("route_status") == "FULLY_ROUTED"]
        gto = [r for r in group if r.get("status") == "timeout"]
        a = sum(int((r.get("metrics") or {}).get("nets_attempted", 0)) for r in gok)
        c = sum(int((r.get("metrics") or {}).get("nets_completed", 0)) for r in gok)
        lines.append(
            f"| {mode} | {len(group)} | {len(gok)} | {len(gfull)} | {len(gto)} | "
            f"{c}/{a} ({_pct(c, a)}) |"
        )

    reasons: Counter[str] = Counter()
    for r in ok:
        reasons.update(r.get("failure_reasons") or {})
    if reasons:
        lines += ["", "## Unrouted nets by reason", "", "| Reason | Nets |", "|---|---:|"]
        lines += [f"| {k} | {v} |" for k, v in reasons.most_common()]
    lines += [
        "",
        "## Per-board results",
        "",
        "| ID | Mode | Difficulty | Status | Router status | Nets | Wall s |",
        "|---|---|---|---|---|---:|---:|",
    ]
    for r in rows:
        m = r.get("metrics") or {}
        nets = (
            f"{m.get('nets_completed', 0)}/{m.get('nets_attempted', 0)}"
            if r.get("status") == "ok"
            else "—"
        )
        wall = f"{float(r['wall_s']):.2f}" if isinstance(r.get("wall_s"), (int, float)) else "—"
        lines.append(
            f"| {r.get('id', '?')} | {r.get('mode', '?')} | {r.get('difficulty', '?')} | "
            f"{r.get('status', '?')} | {r.get('route_status', '—')} | {nets} | {wall} |"
        )

    if failures or timeouts:
        lines += ["", "## Failures and timeouts", ""]
        for r in [*timeouts, *failures]:
            err = str(r.get("error", ""))[:500].replace("\n", " ")
            lines.append(f"- **{r.get('id')} / {r.get('mode')}** — `{r.get('status')}` — {err}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return csv_path, md_path


def print_manifest_summary(manifest: Manifest) -> None:
    counts: dict[str, int] = {}
    for b in manifest.boards:
        counts[b.difficulty] = counts.get(b.difficulty, 0) + 1
    print(manifest.suite_name)
    print(f"suite version: {manifest.suite_version}")
    if manifest.is_real100:
        print(f"boards: {len(manifest.boards)} (QA 80 + demos 20)")
        print(f"pinned KiCad ref: {manifest.source_ref}")
    else:
        repos = {manifest.board_repository(b) for b in manifest.boards}
        print(f"boards: {len(manifest.boards)} from {len(repos)} repositories")
    print("difficulty: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


def _parse_modes(value: str) -> tuple[RouteMode, ...]:
    vals = [v.strip().lower() for v in value.split(",") if v.strip()]
    if not vals:
        raise argparse.ArgumentTypeError("at least one mode is required")
    try:
        return tuple(RouteMode(v) for v in vals)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("modes must be speed,accuracy") from exc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="DAVESROUTER Real100 KiCad benchmark suite")
    p.add_argument(
        "--manifest",
        type=Path,
        default=default_manifest(),
        help="corpus manifest (default: Real100; e.g. benchmarks/openboards/manifest.json)",
    )
    p.add_argument("--workdir", type=Path, default=default_workdir())
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show suite composition")
    f = sub.add_parser("fetch", help="download the pinned board corpus")
    f.add_argument("--force", action="store_true")
    pr = sub.add_parser("prepare", help="make local unrouted benchmark copies")
    pr.add_argument("--force", action="store_true")
    sub.add_parser("inventory", help="parse all prepared boards and write inventory CSV/JSON")
    r = sub.add_parser("run", help="run isolated routing benchmarks")
    r.add_argument("--profile", choices=("smoke", "standard", "full", "routable"), default="smoke")
    r.add_argument("--modes", type=_parse_modes, default=None, help="speed,accuracy")
    r.add_argument("--timeout", type=float, default=None, help="hard timeout per board/mode")
    r.add_argument("--ids", default=None, help="comma-separated IDs such as K001,K081")
    r.add_argument("--max-boards", type=int, default=None)
    r.add_argument("--conservative", choices=("yes", "no"), default="yes")
    r.add_argument(
        "--policy",
        default=None,
        help="search-variant policy: fixed (default), random:SEED or a policy.json",
    )
    r.add_argument("--out", type=Path, default=None, help="results .jsonl (default: runs/)")
    r.add_argument(
        "--check-validity",
        action="store_true",
        help="DRC the finished board and count errors on router-added copper",
    )
    r.add_argument(
        "--no-experience",
        action="store_true",
        help="do not write routing experience records (work/experience/)",
    )
    r.add_argument(
        "--save-routed",
        type=Path,
        default=None,
        help="export every routed board into this folder (for the 'oracle' command)",
    )
    pr2 = sub.add_parser(
        "prune",
        help="classify boards from baseline results; write routable.json beside the manifest",
    )
    pr2.add_argument("results", nargs="+", type=Path)
    o = sub.add_parser(
        "oracle", help="judge saved routed boards with real KiCad DRC (run --save-routed)"
    )
    o.add_argument("results", nargs="+", type=Path)
    o.add_argument("--out", type=Path, required=True, help="oracle results .jsonl")
    o.add_argument("--kicad-cli", default=None, help="kicad-cli to use (default: search)")
    o.add_argument("--kicad-python", default=None, help="Python that imports KiCad pcbnew")
    o.add_argument("--variants", default="refilled,as_exported")
    rep = sub.add_parser("report", help="turn a JSONL run into CSV + Markdown")
    rep.add_argument("results", type=Path)
    a = sub.add_parser("all", help="fetch, prepare, inventory, run, report")
    a.add_argument("--profile", choices=("smoke", "standard", "full"), default="smoke")
    a.add_argument("--modes", type=_parse_modes, default=None)
    a.add_argument("--timeout", type=float, default=None)
    a.add_argument("--max-boards", type=int, default=None)
    a.add_argument("--force", action="store_true")
    a.add_argument("--conservative", choices=("yes", "no"), default="yes")
    a.add_argument("--no-experience", action="store_true")
    a.add_argument("--policy", default=None)

    w = sub.add_parser("worker", help=argparse.SUPPRESS)
    w.add_argument("--board", type=Path, required=True)
    w.add_argument("--mode", choices=("speed", "accuracy"), required=True)
    w.add_argument("--budget", type=float, required=True)
    w.add_argument("--conservative", choices=("yes", "no"), default="yes")
    w.add_argument("--experience", type=Path, default=None)
    w.add_argument("--policy", default=None)
    w.add_argument("--check-validity", action="store_true")
    w.add_argument("--save-routed", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "worker":
        try:
            payload = route_one_board(
                args.board,
                RouteMode(args.mode),
                budget_s=args.budget,
                conservative=args.conservative == "yes",
                experience_dir=args.experience,
                policy=args.policy,
                check_validity=args.check_validity,
                save_routed=args.save_routed,
            )
            print(json.dumps(payload, sort_keys=True))
            return 0
        except BaseException as exc:  # worker boundary: serialize any failure cleanly
            traceback.print_exc(file=sys.stderr)
            print(
                json.dumps(
                    {"status": "error", "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True
                )
            )
            return 0

    manifest = load_manifest(args.manifest)
    # absolute: worker subprocesses resolve board paths from their own cwd
    workdir: Path = args.workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    if args.command == "list":
        print_manifest_summary(manifest)
        return 0
    if args.command == "fetch":
        rows = fetch_all(manifest, workdir, force=args.force)
        print(f"fetch: {sum(r.get('status')=='ok' for r in rows)}/{len(rows)} ok")
        return 0 if all(r.get("status") == "ok" for r in rows) else 2
    if args.command == "prepare":
        rows = prepare_all(manifest, workdir, force=args.force)
        print(f"prepare: {sum(r.get('status')=='ok' for r in rows)}/{len(rows)} ok")
        return 0 if all(r.get("status") == "ok" for r in rows) else 2
    if args.command == "inventory":
        rows = inventory_all(manifest, workdir)
        print(f"inventory: {sum(r.get('status')=='ok' for r in rows)}/{len(rows)} loaded")
        return 0 if all(r.get("status") == "ok" for r in rows) else 2
    if args.command == "run":
        ids = {x.strip() for x in args.ids.split(",") if x.strip()} if args.ids else None
        profile = args.profile
        if profile == "routable":  # the pruned list: boards worth routing
            pruned = load_routable(args.manifest)
            ids = (ids & pruned) if ids else pruned
            profile = "standard"
        result = run_corpus(
            manifest,
            workdir,
            profile=profile,
            modes=args.modes,
            timeout_s=args.timeout,
            ids=ids,
            max_boards=args.max_boards,
            conservative=args.conservative == "yes",
            experience_dir=None if args.no_experience else workdir / "experience",
            policy=args.policy,
            check_validity=args.check_validity,
            out_path=args.out,
            save_routed_dir=args.save_routed.resolve() if args.save_routed else None,
        )
        print(result)
        return 0
    if args.command == "oracle":
        return run_oracle(args)
    if args.command == "prune":
        rows = [
            json.loads(line)
            for path in args.results
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        out, boards = write_routable(rows, manifest, args.manifest, [str(p) for p in args.results])
        excluded = {b: v["reason"] for b, v in boards.items() if not v["routable"]}
        print(f"routable: {len(boards) - len(excluded)} of {len(boards)} measured boards")
        for reason in sorted(set(excluded.values())):
            names = sorted(b for b, r in excluded.items() if r == reason)
            print(f"  excluded ({reason}): {len(names)}: {', '.join(names)}")
        print(out)
        return 0
    if args.command == "report":
        csv_path, md_path = generate_report(args.results)
        print(csv_path)
        print(md_path)
        return 0
    if args.command == "all":
        fetched = fetch_all(manifest, workdir, force=args.force)
        if not all(r.get("status") == "ok" for r in fetched):
            print(
                "fetch had failures; continuing so successful boards remain benchmarkable",
                file=sys.stderr,
            )
        prepare_all(manifest, workdir, force=args.force)
        inventory_all(manifest, workdir)
        result = run_corpus(
            manifest,
            workdir,
            profile=args.profile,
            modes=args.modes,
            timeout_s=args.timeout,
            max_boards=args.max_boards,
            conservative=args.conservative == "yes",
            experience_dir=None if args.no_experience else workdir / "experience",
            policy=args.policy,
        )
        csv_path, md_path = generate_report(result)
        print(result)
        print(csv_path)
        print(md_path)
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
