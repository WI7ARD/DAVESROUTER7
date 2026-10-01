"""OpenBoards curation tool: turn ``candidates.json`` into a pinned benchmark manifest.

A developer tool (like ``benchmarks/real100/pack_builder``), not part of the
router. For every candidate ``{repo, ref, board, domain?, tags?, license_path?,
license_override?, license_note?, keep_incomplete?}``
it

1. resolves ``ref`` to a full commit SHA (``git ls-remote``),
2. downloads the board and its same-stem ``.kicad_pro``/``.kicad_dru`` from
   ``raw.githubusercontent.com`` at that commit and records their Git blob SHA-1s,
3. finds the licence file closest to the board (``LICENSE``/``LICENCE``/``COPYING``…)
   and maps it to an SPDX id by text matching; an unknown licence rejects the
   candidate unless it carries ``license_override`` (+ ``license_note``),
4. requires a ``.kicad_pro`` and a KiCad 6+ board that the project loader and
   ``load_project_rules`` accept,
5. measures whether the ORIGINAL routing is complete (connectivity engine); an
   incomplete board is rejected unless the candidate sets ``keep_incomplete``
   (it then carries ``reference_complete: false`` and a ``reference_incomplete`` tag),
6. strips routing copper exactly like the benchmark harness and requires nets to
   route (``make_plan`` with default settings), and
7. computes ``learning.features.board_profile`` on the stripped board.

Outputs (the only files kept in git): ``manifest.json``, ``CORPUS.md`` and
``ATTRIBUTION.md`` next to this script. Downloads and intermediate boards go to
``work/curate/`` (gitignored)::

    python benchmarks/openboards/curate.py              # evaluate + write outputs
    python benchmarks/openboards/curate.py --only antmicro/  # evaluate a subset, no write

Network access is confined to :func:`resolve_commit` and :func:`fetch_file`, so
tests can replace both.
"""

# ruff: noqa: T201  (a terminal dev tool, like tools/)
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from pcbrouter.benchmark.real100 import (  # noqa: E402
    OPENBOARDS_SCHEMA,
    _download,
    _raw_url,
    git_blob_sha1,
    strip_routing_copper,
)

SUITE_NAME = "DAVESROUTER OpenBoards — pinned open-source KiCad projects"
SUITE_VERSION = "1.0.0"
#: bump when the acceptance checks change: cached verdicts are keyed by it
CURATE_VERSION = 2
#: first KiCad 6.0 board file format; older files (KiCad 5 and 5.99 nightlies) are rejected
KICAD6_MIN_VERSION = 20211014
LICENSE_NAMES = (
    "LICENSE", "LICENSE.md", "LICENSE.txt", "LICENSE.TXT", "LICENCE", "LICENCE.md",
    "LICENCE.txt", "COPYING", "COPYING.md", "COPYING.txt", "License.txt", "license.md",
)  # fmt: skip
#: profile numbers stored in the manifest (and shown in CORPUS.md)
STAT_KEYS = (
    "copper_layers", "signal_layers", "plane_layers", "area_cm2", "pads", "nets_to_route",
    "pad_density_cm2", "long_net_frac", "demand", "pitch_p10_mm", "multi_pad_frac",
)  # fmt: skip


class CandidateRejectedError(Exception):
    """A candidate that does not make it into the corpus (message = reason)."""


# ----------------------------------------------------------------- network
def resolve_commit(repo: str, ref: str) -> str:
    """Full commit SHA of *ref* (tag, branch or SHA) in GitHub repo ``owner/name``."""
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return ref
    proc = subprocess.run(
        ["git", "ls-remote", f"https://github.com/{repo}", ref, f"{ref}^{{}}"],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        check=False,
    )
    if proc.returncode != 0:
        raise CandidateRejectedError(f"git ls-remote failed: {proc.stderr.strip()[:200]}")
    refs: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        sha, _, name = line.partition("\t")
        refs[name.strip()] = sha.strip()
    for name in (f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}", f"refs/heads/{ref}", ref):
        if name in refs:
            return refs[name]
    raise CandidateRejectedError(f"ref {ref!r} not found")


def fetch_file(repo: str, commit: str, path: str) -> bytes | None:
    """Raw file bytes at *commit*, or None when the file does not exist."""
    try:
        return _download(_raw_url(repo, commit, path), timeout=300.0)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


# ------------------------------------------------------------------ helpers
def _cached_fetch(cache: Path, repo: str, commit: str, path: str) -> bytes | None:
    """:func:`fetch_file` with an on-disk cache (commits are immutable)."""
    target = cache / "files" / repo.replace("/", "__") / commit / path
    missing = target.with_name(target.name + ".404")
    if target.is_file():
        return target.read_bytes()
    if missing.is_file():
        return None
    data = fetch_file(repo, commit, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if data is None:
        missing.write_text("", encoding="utf-8")
    else:
        target.write_bytes(data)
    return data


def classify_license(text: str) -> tuple[str | None, str]:
    """``(SPDX id, note)`` by simple text matching; ``(None, why)`` when unknown."""
    t = re.sub(r"\s+", " ", text).lower()
    head = t[:600]
    if "noncommercial" in t or "non-commercial" in t:
        return None, "non-commercial licence"
    # CERN OHL v2: the variant is in the title (each text mentions the others)
    for variant, word in (("S", "strongly reciprocal"), ("W", "weakly reciprocal"),
                          ("P", "permissive")):  # fmt: skip
        if re.search(rf"cern open hardware licen[cs]e version 2 - {word}", head):
            return f"CERN-OHL-{variant}-2.0", f"CERN OHL v2 {word}"
    m = re.search(r"cern[- ]ohl[- ]([psw])(?:-2\.0|[- ]v ?2)", t)
    if m:
        return f"CERN-OHL-{m.group(1).upper()}-2.0", "names CERN OHL v2"
    if re.search(r"cern (open hardware licence|ohl) v\.? ?1\.2", t):
        return "CERN-OHL-1.2", "CERN OHL v1.2"
    if "attribution-sharealike 4.0" in t or "cc-by-sa-4.0" in t or "cc by-sa 4.0" in t:
        return "CC-BY-SA-4.0", "Creative Commons BY-SA 4.0"
    if "attribution-sharealike 3.0" in t or "cc-by-sa-3.0" in t:
        return "CC-BY-SA-3.0", "Creative Commons BY-SA 3.0"
    if "attribution 4.0 international" in t or "cc-by-4.0" in t or "cc by 4.0" in t:
        return "CC-BY-4.0", "Creative Commons BY 4.0"
    if "cc0 1.0 universal" in t:
        return "CC0-1.0", "CC0 public domain dedication"
    if "solderpad hardware licen" in t:
        m = re.search(r"solderpad hardware licen[cs]e,? (?:version )?v?(\d\.\d+)", t)
        return f"SHL-{m.group(1) if m else '2.1'}", "Solderpad Hardware License"
    if "tapr open hardware license" in t:
        return "TAPR-OHL-1.0", "TAPR OHL"
    if "apache license" in head and "version 2.0" in t:
        return "Apache-2.0", "Apache 2.0"
    if "gnu lesser general public license" in head:
        return ("LGPL-3.0" if "version 3" in head else "LGPL-2.1"), "GNU LGPL"
    if "gnu affero general public license" in head:
        return "AGPL-3.0", "GNU AGPL"
    if "gnu general public license" in head:
        if "version 3" in head:
            return "GPL-3.0", "GNU GPL v3"
        if "version 2" in head:
            return "GPL-2.0", "GNU GPL v2"
    if "mozilla public license version 2.0" in t:
        return "MPL-2.0", "Mozilla Public License 2.0"
    if "permission is hereby granted, free of charge" in t:
        return "MIT", "MIT"
    if "redistribution and use in source and binary forms" in t:
        if "neither the name" in t or "endorse or promote" in t:
            return "BSD-3-Clause", "BSD 3-clause"
        return "BSD-2-Clause", "BSD 2-clause"
    if "permission to use, copy, modify, and/or distribute this software for any purpose" in t:
        if "provided that the above copyright notice" in t:
            return "ISC", "ISC"
        return "0BSD", "zero-clause BSD"
    if "free and unencumbered software released into the public domain" in t:
        return "Unlicense", "Unlicense"
    # short notices that just name the licence by SPDX id
    for spdx in ("CERN-OHL-S-2.0", "CERN-OHL-W-2.0", "CERN-OHL-P-2.0", "CC-BY-SA-4.0",
                 "CC-BY-4.0", "Apache-2.0", "MIT", "GPL-3.0", "GPL-2.0"):  # fmt: skip
        if re.search(rf"(?<![\w-]){re.escape(spdx.lower())}(?![\w-]|\.\d)", t):
            return spdx, f"names {spdx}"
    return None, "unrecognised licence text"


def _license_dirs(board_path: str) -> list[str]:
    """The board's folder and its ancestors, closest first ('' = repository root)."""
    parts = board_path.split("/")[:-1]
    return ["/".join(parts[:i]) for i in range(len(parts), -1, -1)]


def find_license(
    cache: Path, repo: str, commit: str, board_path: str, extra: str | None = None
) -> tuple[str, str] | None:
    """``(path, text)`` of the licence file closest to the board."""
    paths = [extra] if extra else []
    for d in _license_dirs(board_path):
        paths += [f"{d}/{n}" if d else n for n in LICENSE_NAMES]
    for p in paths:
        data = _cached_fetch(cache, repo, commit, p)
        if data is not None:
            return p, data.decode("utf-8", "replace")
    return None


def _copyright_line(text: str) -> str | None:
    for line in text.splitlines():
        s = line.strip().lstrip("#*/ ").strip()
        if re.match(r"(?i)(copyright|\(c\)|©)", s) and re.search(r"\d{4}", s):
            return s[:160]
    return None


def _difficulty(p: dict[str, float]) -> tuple[str, str]:
    """(difficulty, route_policy) from the stripped board's profile."""
    nets, density = p["nets_to_route"], p["pad_density_cm2"]
    if nets > 2000 or p["pads"] > 12000:
        return "torture", "stress"
    if nets >= 60 and density > 8.0:
        return "dense", "route"
    if nets >= 250:
        return "large", "route"
    if nets >= 60:
        return "normal", "route"
    return "small", "route"


def derived_tags(p: dict[str, float], complete: bool) -> list[str]:
    layers, signal, planes = int(p["copper_layers"]), int(p["signal_layers"]), p["plane_layers"]
    tags = [f"{layers}layer", "planes" if planes else "all_signal"]
    if signal == 1:
        tags.append("single_routable_layer")
    if signal >= 4 and not planes:
        tags.append("free_signal_4plus")
    if p["long_net_frac"] > 0.8:
        tags.append("long_nets")
    if p["pad_density_cm2"] > 8.0:
        tags.append("dense_pads")
    if not complete:
        tags.append("reference_incomplete")
    return tags


# --------------------------------------------------------------- evaluation
def evaluate_candidate(cand: dict[str, Any], cache: Path) -> dict[str, Any]:
    """Accept or reject one candidate; never raises for a bad candidate.

    Verdicts are cached per (candidate, resolved commit, :data:`CURATE_VERSION`)
    in ``cache/results/``; network and tool errors are not cached."""
    out: dict[str, Any] = {"repo": cand.get("repo"), "board": cand.get("board")}
    try:
        commit = resolve_commit(str(cand["repo"]), str(cand.get("ref", "HEAD")))
        key = hashlib.sha256(
            json.dumps([cand, commit, CURATE_VERSION], sort_keys=True).encode()
        ).hexdigest()[:24]
        memo = cache / "results" / f"{key}.json"
        if memo.is_file():
            cached: dict[str, Any] = json.loads(memo.read_text(encoding="utf-8"))
            return cached
        try:
            out.update(_evaluate(cand, cache, commit))
            out["status"] = "accepted"
        except CandidateRejectedError as exc:
            out.update(status="rejected", reason=str(exc))
        memo.parent.mkdir(parents=True, exist_ok=True)
        memo.write_text(json.dumps(out) + "\n", encoding="utf-8")
    except CandidateRejectedError as exc:  # unresolvable ref
        out.update(status="rejected", reason=str(exc))
    except Exception as exc:  # network or tool failure: record, keep going
        out.update(status="rejected", reason=f"error: {type(exc).__name__}: {exc}"[:300])
    return out


def _evaluate(cand: dict[str, Any], cache: Path, commit: str) -> dict[str, Any]:
    from pcbrouter.kicad.loader import load_board
    from pcbrouter.kicad.rule_adapter import load_project_rules
    from pcbrouter.learning.features import board_profile
    from pcbrouter.routing.board_router import BoardRouterSettings, make_plan
    from pcbrouter.routing.request import RouteRequest
    from pcbrouter.routing.working_board import WorkingBoard

    repo, ref, board_path = str(cand["repo"]), str(cand.get("ref", "HEAD")), str(cand["board"])
    board_bytes = _cached_fetch(cache, repo, commit, board_path)
    if board_bytes is None:
        raise CandidateRejectedError("board file not found at that commit")
    stem = board_path[: -len(".kicad_pcb")]
    sidecars: list[dict[str, Any]] = []
    side_bytes: dict[str, bytes] = {}
    for suffix in (".kicad_pro", ".kicad_dru"):
        data = _cached_fetch(cache, repo, commit, stem + suffix)
        if data is not None:
            sidecars.append(
                {"path": stem + suffix, "git_blob_sha1": git_blob_sha1(data), "size": len(data)}
            )
            side_bytes[stem + suffix] = data

    found = find_license(cache, repo, commit, board_path, cand.get("license_path"))
    license_path, text = found if found is not None else (None, "")
    copyright_line = _copyright_line(text) if text else None
    if cand.get("license_override"):
        # the curator read the licence (README, unusual text): record why
        license_id = str(cand["license_override"])
        license_note = str(cand.get("license_note") or "licence set by curator")
    else:
        if found is None:
            raise CandidateRejectedError("no licence file found")
        spdx, license_note = classify_license(text)
        if spdx is None:
            raise CandidateRejectedError(f"unknown licence ({license_path}: {license_note})")
        license_id = spdx
        if cand.get("license_note"):
            license_note = f"{license_note}; {cand['license_note']}"

    if stem + ".kicad_pro" not in side_bytes:
        raise CandidateRejectedError("no .kicad_pro project file (KiCad 5 or board-only upload)")

    work = cache / "eval" / f"{repo.replace('/', '__')}__{commit[:12]}" / stem.replace("/", "__")
    orig_dir, stripped_dir = work / "original", work / "stripped"
    name = board_path.rsplit("/", 1)[-1]
    for folder in (orig_dir, stripped_dir):
        folder.mkdir(parents=True, exist_ok=True)
        for side_path, data in side_bytes.items():
            (folder / side_path.rsplit("/", 1)[-1]).write_bytes(data)
    orig = orig_dir / name
    orig.write_bytes(board_bytes)

    try:
        loaded = load_board(orig)
        rules = load_project_rules(orig)
    except Exception as exc:
        raise CandidateRejectedError(
            f"parse failure: {type(exc).__name__}: {str(exc)[:160]}"
        ) from exc
    version = loaded.board.metadata.format_version
    if version is None or version < KICAD6_MIN_VERSION:
        raise CandidateRejectedError(f"pre-KiCad-6 board format ({version})")
    pre = WorkingBoard(loaded.board, rules).engine.connectivity.metrics()
    complete = pre["partially_connected"] == 0 and pre["unrouted"] == 0
    if not complete and not cand.get("keep_incomplete"):
        # prefer boards whose own routing is a known-good reference; keep an
        # incomplete one only when the curator says it adds something rare
        raise CandidateRejectedError(
            f"original routing incomplete ({pre['remaining_connections']} connections "
            f"missing on {pre['partially_connected'] + pre['unrouted']} nets)"
        )

    try:
        stripped_text, removed = strip_routing_copper(board_bytes.decode("utf-8-sig"))
    except ValueError as exc:
        raise CandidateRejectedError(f"cannot strip routing: {exc}") from exc
    stripped = stripped_dir / name
    stripped.write_text(stripped_text, encoding="utf-8")
    s_loaded = load_board(stripped)
    s_rules = load_project_rules(stripped)
    wb = WorkingBoard(s_loaded.board, s_rules)
    plan = make_plan(wb, BoardRouterSettings(base_request=RouteRequest("")))
    if not plan.tasks:
        raise CandidateRejectedError("nothing to route after stripping")
    profile = board_profile(wb.board, plan.tasks)
    stats = {k: round(float(profile[k]), 4) for k in STAT_KEYS}
    difficulty, route_policy = _difficulty(profile)
    tags = [str(t) for t in cand.get("tags", ())]
    domain = str(cand.get("domain") or "misc")
    for t in [domain, *derived_tags(profile, complete)]:
        if t not in tags:
            tags.append(t)
    return {
        "name": name,
        "family": domain,
        "repository": repo,
        "ref": ref,
        "commit": commit,
        "source_path": board_path,
        "source_size_bytes": len(board_bytes),
        "git_blob_sha1": git_blob_sha1(board_bytes),
        "sidecars": sidecars,
        "license": license_id,
        "license_file": license_path,
        "license_note": license_note,
        "copyright": copyright_line,
        "attribution": f"{repo} @ {commit}",
        "format_version": version,
        "difficulty": difficulty,
        "route_policy": route_policy,
        "tags": tags,
        "reference_complete": complete,
        "reference_connectivity": pre,
        "removed": removed,
        "stats": stats,
    }


# ------------------------------------------------------------------ outputs
MANIFEST_BOARD_KEYS = (
    "id", "name", "family", "repository", "commit", "ref", "source_path", "source_size_bytes",
    "git_blob_sha1", "sidecars", "license", "license_file", "license_note", "attribution",
    "difficulty", "route_policy", "tags", "reference_complete", "copyright", "stats",
)  # fmt: skip


def build_manifest(results: list[dict[str, Any]]) -> dict[str, Any]:
    accepted = [r for r in results if r["status"] == "accepted"]
    boards = []
    for i, r in enumerate(accepted, 1):
        entry = {**r, "id": f"O{i:03d}"}
        boards.append({k: entry[k] for k in MANIFEST_BOARD_KEYS if k in entry})
    return {
        "schema": OPENBOARDS_SCHEMA,
        "suite_name": SUITE_NAME,
        "suite_version": SUITE_VERSION,
        "created_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "board_count": len(boards),
        "notes": [
            "Open-source KiCad 6+ projects from many repositories, each pinned by commit "
            "and Git blob SHA-1 (board and sidecar .kicad_pro/.kicad_dru).",
            "Board bytes are fetched on demand and are not vendored in DAVESROUTER.",
            "Benchmark boards are unrouted derivatives: top-level segments, vias and copper "
            "track arcs are stripped after hash verification; zones and rules are kept.",
            "Generated by benchmarks/openboards/curate.py from candidates.json.",
        ],
        "boards": boards,
    }


def _f(v: float, nd: int = 2) -> str:
    return f"{v:.{nd}f}"


def coverage(boards: list[dict[str, Any]]) -> dict[str, Any]:
    st = [b["stats"] for b in boards]
    return {
        "boards": len(boards),
        "repositories": len({b["repository"] for b in boards}),
        "copper_layers": dict(sorted(Counter(int(s["copper_layers"]) for s in st).items())),
        "signal_layers": dict(sorted(Counter(int(s["signal_layers"]) for s in st).items())),
        "domains": dict(Counter(b["family"] for b in boards).most_common()),
        "with_planes": sum(1 for s in st if s["plane_layers"] > 0),
        "all_signal": sum(1 for s in st if s["plane_layers"] == 0),
        "reference_complete": sum(1 for b in boards if b["reference_complete"]),
        "gap_free_signal_4plus": sum(
            1 for s in st if s["signal_layers"] >= 4 and s["plane_layers"] == 0
        ),
        "gap_long_net_frac_gt_0.8": sum(1 for s in st if s["long_net_frac"] > 0.8),
        "gap_pad_density_gt_8": sum(1 for s in st if s["pad_density_cm2"] > 8.0),
        "gap_single_routable_layer": sum(1 for s in st if s["signal_layers"] == 1),
        "licenses": dict(Counter(b["license"] for b in boards).most_common()),
    }


def write_corpus_md(manifest: dict[str, Any], results: list[dict[str, Any]], path: Path) -> None:
    boards = manifest["boards"]
    cov = coverage(boards)
    lines = [
        "# OpenBoards corpus",
        "",
        f"{cov['boards']} accepted boards from {cov['repositories']} repositories "
        f"(suite {manifest['suite_version']}, generated {manifest['created_utc']} by "
        "`curate.py`). Numbers are `learning.features.board_profile` of the **stripped** "
        "board: `signal_layers` = copper layers minus plane-like layers (>60 % zone "
        "cover), `long_net_frac` = share of nets whose airwire exceeds a quarter of the "
        "board diagonal, `demand` = airwire length per routable area and signal layer.",
        "",
        "## Coverage",
        "",
        f"- copper layers: {cov['copper_layers']}",
        f"- signal (routable) layers: {cov['signal_layers']}",
        f"- with plane layers: {cov['with_planes']}; all-signal: {cov['all_signal']}",
        f"- domains: {cov['domains']}",
        f"- original routing complete (`reference_complete`): "
        f"{cov['reference_complete']}/{cov['boards']}",
        f"- selector gaps: 4+ free signal layers without planes: "
        f"{cov['gap_free_signal_4plus']}; long-net share > 0.8: "
        f"{cov['gap_long_net_frac_gt_0.8']}; pad density > 8 /cm²: "
        f"{cov['gap_pad_density_gt_8']}; single routable layer: "
        f"{cov['gap_single_routable_layer']}",
        f"- licences: {cov['licenses']}",
        "",
        "## Accepted boards",
        "",
        "| ID | Board | Domain | Layers | Signal | Nets | Pads | Pads/cm² | Long-net | "
        "Demand | Licence | Ref. complete |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for b in boards:
        s = b["stats"]
        lines.append(
            f"| {b['id']} | `{b['repository']}` {b['source_path']} | {b['family']} | "
            f"{int(s['copper_layers'])} | {int(s['signal_layers'])} | "
            f"{int(s['nets_to_route'])} | {int(s['pads'])} | {_f(s['pad_density_cm2'], 1)} | "
            f"{_f(s['long_net_frac'])} | {_f(s['demand'], 3)} | {b['license']} | "
            f"{'yes' if b['reference_complete'] else '**no**'} |"
        )
    rejected = [r for r in results if r["status"] != "accepted"]
    lines += ["", f"## Rejected candidates ({len(rejected)})", ""]
    if rejected:
        lines += ["| Candidate | Reason |", "|---|---|"]
        for r in rejected:
            reason = str(r.get("reason", "")).replace("|", "/").replace("\n", " ")
            lines.append(f"| `{r['repo']}` {r['board']} | {reason} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_attribution_md(manifest: dict[str, Any], path: Path) -> None:
    lines = [
        "# OpenBoards attribution",
        "",
        "The OpenBoards corpus does **not** vendor any board files. `fetch` downloads each "
        "file from its original repository at the pinned commit; `prepare` makes a local "
        "unrouted derivative (routing copper removed) for benchmarking only. All rights "
        "remain with the original authors under the licences below; see each repository "
        "for the full licence text and any file-specific notices.",
        "",
        "| ID | Project file | Source (repository @ commit) | Licence | Licence file | Notice |",
        "|---|---|---|---|---|---|",
    ]
    for b in manifest["boards"]:
        url = f"https://github.com/{b['repository']}/blob/{b['commit']}/{b['source_path']}"
        notice = (b.get("copyright") or "").replace("|", "/")
        lines.append(
            f"| {b['id']} | [{b['name']}]({url.replace(' ', '%20')}) | "
            f"{b['attribution']} | {b['license']} | {b.get('license_file') or 'curator'} | "
            f"{notice} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Curate the OpenBoards benchmark manifest")
    ap.add_argument("--candidates", type=Path, default=HERE / "candidates.json")
    ap.add_argument("--workdir", type=Path, default=HERE / "work")
    ap.add_argument("--out-dir", type=Path, default=HERE)
    ap.add_argument("--only", default=None, help="evaluate candidates whose repo contains this")
    ap.add_argument("--no-write", action="store_true", help="do not write manifest/markdown")
    a = ap.parse_args(argv)
    cands = json.loads(a.candidates.read_text(encoding="utf-8"))
    cache = a.workdir / "curate"
    cache.mkdir(parents=True, exist_ok=True)
    if a.only:
        cands = [c for c in cands if a.only in c["repo"]]
    results: list[dict[str, Any]] = []
    for i, cand in enumerate(cands, 1):
        t0 = time.perf_counter()
        r = evaluate_candidate(cand, cache)
        results.append(r)
        what = "ok " if r["status"] == "accepted" else "REJ"
        extra = r.get("reason") or (
            f"{int(r['stats']['copper_layers'])}L sig{int(r['stats']['signal_layers'])} "
            f"nets {int(r['stats']['nets_to_route'])} "
            f"complete={r['reference_complete']} {r['license']}"
        )
        print(
            f"[{i:03d}/{len(cands)}] {what} {cand['repo']} {cand['board']} "
            f"({time.perf_counter() - t0:.1f}s): {extra}",
            flush=True,
        )
    (cache / "results.json").write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
    accepted = sum(r["status"] == "accepted" for r in results)
    print(f"accepted {accepted}/{len(results)}")
    if a.no_write or a.only:
        return 0
    manifest = build_manifest(results)
    (a.out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    write_corpus_md(manifest, results, a.out_dir / "CORPUS.md")
    write_attribution_md(manifest, a.out_dir / "ATTRIBUTION.md")
    print(f"wrote {a.out_dir / 'manifest.json'}, CORPUS.md, ATTRIBUTION.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
