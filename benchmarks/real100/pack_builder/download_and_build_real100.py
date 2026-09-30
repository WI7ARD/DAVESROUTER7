from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# In the DAVESROUTER repository the manifest and attribution live one level up
# (benchmarks/real100/) and output goes to the gitignored work/ folder; as a
# standalone download everything sits next to this script.
_IN_REPO = not (ROOT / "manifest.json").exists() and (ROOT.parent / "manifest.json").exists()
_BASE = ROOT.parent if _IN_REPO else ROOT
_OUT = ROOT.parent / "work" / "pack" if _IN_REPO else ROOT
MANIFEST = _BASE / "manifest.json"
OUT_ROOT = _OUT / "Real100-Boards-and-Rules"
FINAL_ZIP = _OUT / "DAVESROUTER-Real100-Unrouted-Boards-and-Rules.zip"
USER_AGENT = "DAVESROUTER-Real100-PackBuilder/1.1"


def git_blob_sha1(data: bytes) -> str:
    hdr = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(hdr + data).hexdigest()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _head_symbol(text: str, open_paren: int) -> str:
    """Return the S-expression symbol immediately after '(' at open_paren."""
    i = open_paren + 1
    n = len(text)
    while i < n and text[i].isspace():
        i += 1
    start = i
    while i < n and (not text[i].isspace()) and text[i] not in "()":
        i += 1
    return text[start:i]


def _matching_paren(text: str, start: int) -> int:
    """Find the matching ')' for text[start] == '(', respecting strings/comments."""
    depth = 0
    in_string = False
    escaped = False
    in_comment = False

    i = start
    while i < len(text):
        ch = text[i]
        if in_comment:
            if ch in "\r\n":
                in_comment = False
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
        if ch == ';':
            in_comment = True
        elif ch == '"':
            in_string = True
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError(f"Unbalanced S-expression starting at byte/char {start}")


def strip_routing(board_data: bytes) -> tuple[bytes, dict[str, int]]:
    """
    Remove routed copper from a KiCad .kicad_pcb while preserving footprints,
    pads, nets, board outline, zones, setup, and embedded design rules.

    Removed top-level routing objects:
      - (segment ...)
      - (via ...)
      - (arc ...)       # curved copper tracks; board graphics are gr_arc

    Zones are deliberately preserved because they are design intent / copper
    constraints rather than autorouter-created point-to-point routes.
    """
    text = board_data.decode("utf-8-sig")
    remove_symbols = {"segment", "via", "arc"}
    counts = {"segment": 0, "via": 0, "arc": 0}

    out: list[str] = []
    last = 0
    depth = 0
    in_string = False
    escaped = False
    in_comment = False
    i = 0

    while i < len(text):
        ch = text[i]
        if in_comment:
            if ch in "\r\n":
                in_comment = False
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
        if ch == ';':
            in_comment = True
            i += 1
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch == '(':
            # The root kicad_pcb expression lives at depth 0. Its direct
            # children (segments/vias/arcs) therefore begin while depth == 1.
            if depth == 1:
                sym = _head_symbol(text, i)
                if sym in remove_symbols:
                    end = _matching_paren(text, i)
                    out.append(text[last:i])
                    # Consume trailing spaces/tabs and one line break so the
                    # removed item does not leave thousands of blank lines.
                    j = end + 1
                    while j < len(text) and text[j] in " \t":
                        j += 1
                    if j < len(text) and text[j] == '\r':
                        j += 1
                        if j < len(text) and text[j] == '\n':
                            j += 1
                    elif j < len(text) and text[j] == '\n':
                        j += 1
                    last = j
                    counts[sym] += 1
                    i = j
                    continue
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth < 0:
                raise ValueError("Malformed KiCad board: unexpected ')'")
        i += 1

    if depth != 0 or in_string:
        raise ValueError("Malformed KiCad board: unbalanced parentheses/string")
    out.append(text[last:])
    stripped = "".join(out).encode("utf-8")
    return stripped, counts


def fetch(url: str, *, timeout: float = 300.0, retries: int = 4) -> bytes:
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as exc:
            last = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                raise
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
    raise last  # type: ignore[misc]


def raw_url(ref: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/KiCad/kicad-source-mirror/{ref}/{path}"


def same_stem(path: str, suffix: str) -> str:
    return str(Path(path).with_suffix(suffix)).replace("\\", "/")


def write_metadata(pack_dir: Path, manifest: dict, rows: list[dict]) -> None:
    with (pack_dir / "rules_inventory.csv").open("w", newline="", encoding="utf-8") as f:
        fields = [
            "id", "family", "difficulty", "board_file", "source_path", "board_git_blob_sha1",
            "source_board_sha256", "unrouted_board_sha256", "segments_removed", "vias_removed",
            "track_arcs_removed", "kicad_pro", "kicad_dru", "rule_sources", "tags"
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    summary = {
        "suite": "DAVESROUTER Real100 Boards and Rules",
        "board_count": len(rows),
        "source_repository": manifest["source_repository"],
        "source_ref": manifest["source_ref"],
        "source_ref_date": manifest.get("source_ref_date"),
        "boards_with_kicad_pro": sum(bool(r["kicad_pro"]) for r in rows),
        "boards_with_kicad_dru": sum(bool(r["kicad_dru"]) for r in rows),
        "boards_with_explicit_sidecar_rules": sum(bool(r["kicad_pro"] or r["kicad_dru"]) for r in rows),
        "routing_transform": "Removed all top-level KiCad segment, via, and track-arc objects; zones preserved.",
        "total_segments_removed": sum(int(r["segments_removed"]) for r in rows),
        "total_vias_removed": sum(int(r["vias_removed"]) for r in rows),
        "total_track_arcs_removed": sum(int(r["track_arcs_removed"]) for r in rows),
        "note": (
            "KiCad rules may exist in .kicad_dru, project settings in .kicad_pro, and/or embedded in the "
            ".kicad_pcb itself (setup/netclasses/board settings). Absence of a .kicad_dru does not mean a board has no rules."
        ),
    }
    (pack_dir / "PACK_INFO.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    lines = [
        "# DAVESROUTER Real100 — Boards and Rules",
        "",
        f"Pinned KiCad source commit: `{manifest['source_ref']}`",
        f"Boards: **{len(rows)}**",
        "",
        "Each K### folder contains an **unrouted derivative** of the pinned upstream `.kicad_pcb`, plus matching `.kicad_pro` and `.kicad_dru` sidecars when those exist upstream.",
        "",
        "The builder verifies the original board against its pinned Git blob SHA **before** transformation, then removes top-level `(segment ...)`, `(via ...)`, and copper-track `(arc ...)` objects.",
        "Copper zones are preserved as design intent/constraints.",
        "",
        "Rule sources and net assignments are intentionally preserved as-is. KiCad design rules can live in:",
        "- `.kicad_dru` custom rule files",
        "- `.kicad_pro` project settings/net classes",
        "- the `.kicad_pcb` board file itself.",
        "",
        "See `rules_inventory.csv` for per-board source hashes, unrouted hashes, and removed-route counts.",
        "",
        "## Important",
        "These are upstream KiCad QA/demo fixtures. Keep the included attribution/source information with the corpus.",
    ]
    (pack_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    if not MANIFEST.exists():
        print("manifest.json missing", file=sys.stderr)
        return 2
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    boards = manifest["boards"]
    if len(boards) != 100:
        raise RuntimeError(f"Expected exactly 100 boards, got {len(boards)}")

    if OUT_ROOT.exists():
        shutil.rmtree(OUT_ROOT)
    OUT_ROOT.mkdir(parents=True)

    # Retain attribution in generated pack.
    attr = _BASE / "ATTRIBUTION.md"
    if attr.exists():
        shutil.copy2(attr, OUT_ROOT / "ATTRIBUTION.md")
    shutil.copy2(MANIFEST, OUT_ROOT / "manifest.json")

    rows: list[dict] = []
    ref = manifest["source_ref"]

    for i, b in enumerate(boards, 1):
        bid = b["id"]
        src = b["source_path"]
        name = b["name"]
        folder = OUT_ROOT / bid
        folder.mkdir(parents=True, exist_ok=True)

        print(f"[{i:03d}/100] {bid}  {src}", flush=True)
        board_url = raw_url(ref, src)
        data = fetch(board_url, timeout=600.0 if b.get("source_size_bytes", 0) > 20_000_000 else 180.0)
        got_blob = git_blob_sha1(data)
        expected_blob = b["git_blob_sha1"]
        if got_blob != expected_blob:
            raise RuntimeError(f"{bid}: Git blob SHA mismatch: expected {expected_blob}, got {got_blob}")
        unrouted_data, removed = strip_routing(data)
        verify_data, leftovers = strip_routing(unrouted_data)
        if any(leftovers.values()) or verify_data != unrouted_data:
            raise RuntimeError(f"{bid}: route stripping verification failed: {leftovers}")
        (folder / name).write_bytes(unrouted_data)
        print(
            f"          stripped: {removed['segment']} segments, "
            f"{removed['via']} vias, {removed['arc']} track arcs",
            flush=True,
        )

        found = {}
        for suffix in (".kicad_pro", ".kicad_dru"):
            side_src = same_stem(src, suffix)
            side_name = Path(side_src).name
            try:
                side_data = fetch(raw_url(ref, side_src), timeout=120.0)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    found[suffix] = ""
                    continue
                raise
            (folder / side_name).write_bytes(side_data)
            found[suffix] = side_name

        # Source metadata per board makes the pack self-auditing.
        source_meta = {
            "id": bid,
            "source_repository": manifest["source_repository"],
            "source_ref": ref,
            "source_path": src,
            "git_blob_sha1": expected_blob,
            "source_sha256": sha256(data),
            "unrouted_sha256": sha256(unrouted_data),
            "transform": {
                "name": "strip_routing",
                "removed": removed,
                "zones_preserved": True,
            },
            "family": b.get("family"),
            "difficulty": b.get("difficulty"),
            "tags": b.get("tags", []),
            "sidecars": [v for v in found.values() if v],
        }
        (folder / "SOURCE.json").write_text(json.dumps(source_meta, indent=2), encoding="utf-8")

        rule_sources = ["embedded_board_settings"]
        if found.get(".kicad_pro"):
            rule_sources.append("kicad_pro")
        if found.get(".kicad_dru"):
            rule_sources.append("kicad_dru")

        rows.append({
            "id": bid,
            "family": b.get("family", ""),
            "difficulty": b.get("difficulty", ""),
            "board_file": name,
            "source_path": src,
            "board_git_blob_sha1": expected_blob,
            "source_board_sha256": sha256(data),
            "unrouted_board_sha256": sha256(unrouted_data),
            "segments_removed": removed["segment"],
            "vias_removed": removed["via"],
            "track_arcs_removed": removed["arc"],
            "kicad_pro": found.get(".kicad_pro", ""),
            "kicad_dru": found.get(".kicad_dru", ""),
            "rule_sources": ";".join(rule_sources),
            "tags": ";".join(b.get("tags", [])),
        })

    write_metadata(OUT_ROOT, manifest, rows)

    if FINAL_ZIP.exists():
        FINAL_ZIP.unlink()
    print("\nCreating ZIP...", flush=True)
    with zipfile.ZipFile(FINAL_ZIP, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
        for p in sorted(OUT_ROOT.rglob("*")):
            if p.is_file():
                z.write(p, Path(OUT_ROOT.name) / p.relative_to(OUT_ROOT))

    print("\nDONE")
    print(f"Folder: {OUT_ROOT}")
    print(f"ZIP:    {FINAL_ZIP}")
    print(f"Size:   {FINAL_ZIP.stat().st_size / (1024*1024):.1f} MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
