"""KiCad DRC oracle: does KiCad itself agree that the router's copper is legal?

The application's own validator decides legality while routing. This module is
the independent check used by benchmarks and tests: it runs real KiCad DRC
(``kicad-cli pcb drc``) on the board **before** routing and on the exported board
**after** routing, then diffs the two reports so that problems the board already
had are never blamed on the router:

* ``pre_existing`` - in both reports;
* ``new_generated`` - only after routing, and an item of the violation is copper
  the router added (matched by KiCad UUID): **a router legality bug** (or a rule
  the engine reads differently from KiCad);
* ``new_unrelated`` - only after routing, touching no router copper (usually a
  zone refill reacting to the new copper);
* ``resolved`` - only before routing.

Zone fills: the router treats foreign zone fills as refillable (KiCad's own
workflow: route, then refill). DRC on the exported file alone would flag every
track that crosses a stale fill, so each board is checked twice when a refill
is available:

* ``as_exported`` - the file exactly as written (stale fills);
* ``refilled`` - after KiCad refilled every zone (``kicad-cli pcb drc
  --refill-zones`` on KiCad 9+, or the KiCad Python API on KiCad 8). This is
  what the user gets after pressing *B* in KiCad, so it is the verdict.

KiCad only ever receives files: the oracle runs fixed argument lists (never a
shell), works on copies in its own folder and never touches the source board.
Nothing the router or a model produces is executed. The KiCad version and
capabilities are recorded with every result, because DRC differs between
KiCad releases.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pcbrouter.kicad.kicad_cli import find_kicad_cli

ORACLE_SCHEMA = "davesrouter-kicad-oracle/1"
DRC_TIMEOUT_S = 600.0
# the newest board file format each KiCad release reads (``(version N)``); a
# board newer than this is refused by that KiCad, so it is reported, not run
KNOWN_MAX_FORMAT = {7: 20221018, 8: 20240108, 9: 20241229}
SIDECAR_SUFFIXES = (".kicad_pro", ".kicad_dru")
# fixed refill program for KiCad's own Python (never generated, never user text)
_REFILL_PROGRAM = (
    "import sys\n"
    "import pcbnew\n"
    "b = pcbnew.LoadBoard(sys.argv[1])\n"
    "pcbnew.ZONE_FILLER(b).Fill(b.Zones())\n"
    "b.Save(sys.argv[2])\n"
)
_PYTHON_CANDIDATES = ("python3", "python3.13", "python3.12", "python3.11")
_KICAD_PY_PATHS = ("/usr/lib/python3/dist-packages",)


# ------------------------------------------------------------------ the tool
@dataclass(frozen=True)
class OracleTool:
    """One KiCad installation able to judge boards."""

    cli: Path
    version: str
    refill: str  # "cli" (drc --refill-zones), "python" (pcbnew API) or "none"
    python: str | None = None

    @property
    def major(self) -> int | None:
        m = re.match(r"(\d+)", self.version)
        return int(m.group(1)) if m else None

    @property
    def max_format(self) -> int | None:
        return KNOWN_MAX_FORMAT.get(self.major or -1)

    def can_read(self, board_format: int | None) -> bool:
        limit = self.max_format
        return board_format is None or limit is None or board_format <= limit

    def metadata(self) -> dict[str, Any]:
        return {
            "kicad_version": self.version,
            "kicad_cli": str(self.cli),
            "refill": self.refill,
            "kicad_python": self.python,
            "max_board_format": self.max_format,
        }


def _run(args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
    from pcbrouter.utils.process import hidden_console_kwargs

    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout_s, check=False,
        **hidden_console_kwargs(),
    )  # fmt: skip


def _kicad_python(explicit: str | None, version: str) -> str | None:
    """A Python interpreter whose ``pcbnew`` module is the same KiCad release as
    the kicad-cli (a KiCad 8 refill judged by KiCad 9 DRC would be meaningless)."""
    probe = f"import sys; sys.path[:0] = {list(_KICAD_PY_PATHS)!r}; import pcbnew; "
    probe += "print(pcbnew.Version())"
    for exe in [explicit] if explicit else _PYTHON_CANDIDATES:
        path = shutil.which(exe) if exe else None
        if path is None:
            continue
        try:
            proc = _run([path, "-c", probe], 60)
        except (OSError, subprocess.SubprocessError):
            continue
        lines = proc.stdout.strip().splitlines()
        if proc.returncode == 0 and lines and lines[-1].split("-")[0] == version.split("-")[0]:
            return path
    return None


def find_oracle(cli: str | None = None, python: str | None = None) -> OracleTool | None:
    """Locate kicad-cli (``cli`` overrides the search) and how it can refill zones.

    Returns ``None`` when there is no KiCad, or the KiCad has no ``pcb drc``
    (KiCad 7): the caller must report "oracle unavailable", never "clean"."""
    if cli:
        exe = shutil.which(cli) or (cli if Path(cli).is_file() else None)
        if exe is None:
            return None
        try:
            version = _run([exe, "version"], 30).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        found = (Path(exe), version)
    else:
        k = find_kicad_cli()
        if k is None:
            return None
        found = (k.path, k.version or "")
    try:
        helptext = _run([str(found[0]), "pcb", "drc", "--help"], 30)
    except (OSError, subprocess.SubprocessError):
        return None
    text = helptext.stdout + helptext.stderr
    if "INPUT_FILE" not in text and "--format" not in text:
        return None  # KiCad 7: no 'pcb drc' command
    if "--refill-zones" in text:
        return OracleTool(found[0], found[1], "cli")
    py = _kicad_python(python, found[1])
    return OracleTool(found[0], found[1], "python" if py else "none", py)


def board_format(path: Path) -> int | None:
    """The ``(version N)`` of a board file (read from its first bytes)."""
    try:
        head = path.read_bytes()[:400].decode("utf-8", "replace")
    except OSError:
        return None
    m = re.search(r"\(version\s+(\d+)", head)
    return int(m.group(1)) if m else None


# ------------------------------------------------------------------ reports
@dataclass(frozen=True)
class KItem:
    description: str
    uuid: str | None
    x: float | None
    y: float | None

    def ident(self) -> str:
        if self.uuid:
            return self.uuid
        return f"{self.description}@{self.x},{self.y}"


@dataclass(frozen=True)
class KViolation:
    type: str
    severity: str
    description: str
    items: tuple[KItem, ...]

    def key(self) -> tuple[str, tuple[str, ...]]:
        """Identity across two DRC runs of the same board (type + items)."""
        if self.items:
            return (self.type, tuple(sorted(i.ident() for i in self.items)))
        return (self.type, (self.description,))

    def touches(self, ids: set[str]) -> bool:
        return any(i.uuid in ids for i in self.items if i.uuid)

    def nets(self) -> set[str]:
        out: set[str] = set()
        for i in self.items:
            m = re.search(r"\[([^\]]*)\]", i.description)
            if m:
                out.add(m.group(1))
        return out

    def brief(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "severity": self.severity,
            "description": self.description[:200],
            "items": [i.description[:120] for i in self.items[:4]],
            "pos": next(([i.x, i.y] for i in self.items if i.x is not None), None),
        }


def _items(raw: Any) -> tuple[KItem, ...]:
    out = []
    for it in raw if isinstance(raw, list) else []:
        if not isinstance(it, dict):
            continue
        raw_pos = it.get("pos")
        pos: dict[str, Any] = raw_pos if isinstance(raw_pos, dict) else {}
        out.append(
            KItem(
                str(it.get("description", "")),
                str(it["uuid"]) if it.get("uuid") else None,
                pos.get("x"),
                pos.get("y"),
            )
        )
    return tuple(out)


def _violations(raw: Any) -> list[KViolation]:
    return [
        KViolation(
            str(v.get("type", "?")),
            str(v.get("severity", "?")),
            str(v.get("description", "")),
            _items(v.get("items")),
        )
        for v in (raw if isinstance(raw, list) else [])
        if isinstance(v, dict)
    ]


@dataclass
class DrcRun:
    """One KiCad DRC run of one board."""

    status: str  # "ok", "not_run", "error"
    variant: str  # "as_exported" or "refilled"
    violations: list[KViolation] = field(default_factory=list)
    unconnected: list[KViolation] = field(default_factory=list)
    seconds: float = 0.0
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def parse_report(data: dict[str, Any], variant: str) -> DrcRun:
    return DrcRun(
        "ok",
        variant,
        _violations(data.get("violations")),
        _violations(data.get("unconnected_items")),
    )


# ------------------------------------------------------------------ running
def stage_board(board: Path, dest_dir: Path, stem: str, project_of: Path | None = None) -> Path:
    """Copy *board* and its project sidecars to *dest_dir* as ``stem.*`` so that
    KiCad reads the same project rules, and nothing ever writes beside the
    source file. *project_of* names the board whose ``.kicad_pro`` /
    ``.kicad_dru`` to use (a routed export shares its source's project; without
    them KiCad would judge it by its built-in defaults)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{stem}.kicad_pcb"
    shutil.copy2(board, out)
    for suffix in SIDECAR_SUFFIXES:
        side = (project_of or board).with_suffix(suffix)
        if side.is_file():
            shutil.copy2(side, dest_dir / f"{stem}{suffix}")
    return out


def run_drc(
    tool: OracleTool, board: Path, variant: str, timeout_s: float = DRC_TIMEOUT_S
) -> DrcRun:
    """KiCad DRC of *board* (a staged copy). ``variant="refilled"`` refills every
    zone first; the refilled board is written next to it (``*.refilled.kicad_pcb``)."""
    t0 = time.perf_counter()
    fmt = board_format(board)
    if not tool.can_read(fmt):
        return DrcRun(
            "not_run", variant,
            message=f"board format {fmt} is newer than KiCad {tool.version} reads "
            f"(max {tool.max_format})",
        )  # fmt: skip
    if variant == "refilled" and tool.refill == "none":
        return DrcRun("not_run", variant, message="this KiCad cannot refill zones headless")
    target = board
    extra: list[str] = []
    try:
        if variant == "refilled" and tool.refill == "python":
            assert tool.python is not None
            target = board.with_name(board.stem + ".refilled.kicad_pcb")
            for suffix in SIDECAR_SUFFIXES:
                side = board.with_suffix(suffix)
                if side.is_file():
                    shutil.copy2(side, target.with_suffix(suffix))
            prog = f"import sys; sys.path[:0] = {list(_KICAD_PY_PATHS)!r}\n" + _REFILL_PROGRAM
            proc = _run([tool.python, "-c", prog, str(board), str(target)], timeout_s)
            if proc.returncode != 0 or not target.is_file():
                return DrcRun(
                    "error", variant, seconds=time.perf_counter() - t0,
                    message="zone refill failed: " + (proc.stderr or proc.stdout)[-300:],
                )  # fmt: skip
        elif variant == "refilled":
            extra = ["--refill-zones"]
        report = target.with_name(target.stem + f".{variant}.drc.json")
        args = [
            str(tool.cli), "pcb", "drc", "--format", "json", "--severity-all",
            *extra, "--output", str(report), str(target),
        ]  # fmt: skip
        proc = _run(args, timeout_s)
        if not report.is_file():
            return DrcRun(
                "error", variant, seconds=time.perf_counter() - t0,
                message=(proc.stderr or proc.stdout or "no report")[-300:],
            )  # fmt: skip
        run = parse_report(json.loads(report.read_text(encoding="utf-8")), variant)
    except subprocess.TimeoutExpired:
        return DrcRun("error", variant, seconds=time.perf_counter() - t0, message="timed out")
    except (OSError, ValueError) as exc:
        return DrcRun("error", variant, seconds=time.perf_counter() - t0, message=str(exc)[:300])
    run.seconds = time.perf_counter() - t0
    return run


# ------------------------------------------------------------------ verdict
def compare(pre: DrcRun, post: DrcRun, generated: set[str]) -> dict[str, Any]:
    """Diff a pre-route and a post-route DRC run of the same board."""
    if not (pre.ok and post.ok):
        why = pre.message if not pre.ok else post.message
        return {"variant": post.variant, "status": "NOT_RUN", "reason": why}
    before = {v.key() for v in pre.violations}
    after = {v.key() for v in post.violations}
    new = [v for v in post.violations if v.key() not in before]
    gen = [v for v in new if v.touches(generated)]
    unrelated = [v for v in new if not v.touches(generated)]
    resolved = [v for v in pre.violations if v.key() not in after]
    pre_unc_nets = set().union(*(v.nets() for v in pre.unconnected)) if pre.unconnected else set()
    post_unc_nets = (
        set().union(*(v.nets() for v in post.unconnected)) if post.unconnected else set()
    )
    gen_unc = [v for v in post.unconnected if v.touches(generated)]

    def errors(vs: list[KViolation]) -> int:
        return sum(1 for v in vs if v.severity == "error")

    status = "CLEAN"
    if errors(gen):
        status = "ROUTER_VIOLATIONS"
    elif errors(unrelated):
        status = "NEW_UNRELATED_VIOLATIONS"
    return {
        "variant": post.variant,
        "status": status,
        "pre_existing": len(before & after),
        "new_generated_errors": errors(gen),
        "new_generated_warnings": len(gen) - errors(gen),
        "new_unrelated_errors": errors(unrelated),
        "new_unrelated_warnings": len(unrelated) - errors(unrelated),
        "resolved": len(resolved),
        "new_generated_types": dict(Counter(v.type for v in gen)),
        "new_unrelated_types": dict(Counter(v.type for v in unrelated)),
        "unconnected_before": len(pre.unconnected),
        "unconnected_after": len(post.unconnected),
        "unconnected_nets_after": sorted(post_unc_nets),
        "newly_unconnected_nets": sorted(post_unc_nets - pre_unc_nets),
        "unconnected_touching_generated": len(gen_unc),
        "examples_generated": [v.brief() for v in gen[:8]],
        "examples_unrelated": [v.brief() for v in unrelated[:5]],
        "examples_unconnected_generated": [v.brief() for v in gen_unc[:5]],
        "drc_s": round(pre.seconds + post.seconds, 2),
    }


def oracle_check(
    tool: OracleTool,
    pre_board: Path,
    post_board: Path,
    generated: set[str],
    workdir: Path,
    *,
    variants: tuple[str, ...] = ("refilled", "as_exported"),
    claimed_complete_nets: set[str] | None = None,
    cache: dict[tuple[str, ...], DrcRun] | None = None,
) -> dict[str, Any]:
    """Judge one routed board: stage both boards (with sidecars) in *workdir*, run
    each DRC variant on both and diff them. *cache* keeps pre-route runs, keyed by
    the source file's hash, so several routes of one board share them. The
    verdict is the ``refilled`` variant when it ran, else ``as_exported`` (stale
    fills: flagged)."""
    pre = stage_board(pre_board, workdir, "pre")
    post = stage_board(post_board, workdir, "post", project_of=pre_board)
    out: dict[str, Any] = {
        "schema": ORACLE_SCHEMA,
        **tool.metadata(),
        "board_format": board_format(pre_board),
        "generated_objects": len(generated),
        "variants": {},
    }
    digest = hashlib.sha256(pre_board.read_bytes()).hexdigest()
    for variant in variants:
        key = (digest, variant, tool.version, tool.refill)
        pre_run = cache.get(key) if cache is not None else None
        if pre_run is None:
            pre_run = run_drc(tool, pre, variant)
            if cache is not None:
                cache[key] = pre_run
        result = compare(pre_run, run_drc(tool, post, variant), generated)
        if claimed_complete_nets is not None and result["status"] != "NOT_RUN":
            # nets the router reported finished that KiCad still sees open
            open_nets = set(result["unconnected_nets_after"])
            result["completion_disagreements"] = sorted(claimed_complete_nets & open_nets)
        out["variants"][variant] = result
    ran = [v for v in variants if out["variants"][v]["status"] != "NOT_RUN"]
    verdict = ran[0] if ran else None
    out["verdict_variant"] = verdict
    out["status"] = out["variants"][verdict]["status"] if verdict else "NOT_RUN"
    if verdict == "as_exported":
        out["note"] = "zones not refilled: violations against stale fills are expected"
    if verdict is None:
        out["reason"] = "; ".join(f"{v}: {out['variants'][v].get('reason', '')}" for v in variants)
    return out
