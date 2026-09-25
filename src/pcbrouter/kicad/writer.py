"""Round-trip-safe KiCad export (Stage 9).

Strategy — **never rebuild the file**. The exported board is the source file's
bytes, unchanged, with only the router's new ``(segment …)`` / ``(via …)`` nodes
inserted before the final closing parenthesis, written in the file's own style
(layer quoting, ``uuid`` vs ``tstamp``, net codes vs names, line endings).
Every construct we do not understand is therefore preserved verbatim.

Safety gates, in order (any failure blocks the export and writes nothing):

1. the source file must still have the SHA-256 recorded when it was opened;
2. the file format version must be one this writer supports (KiCad 6 … 10);
3. the working board may only *add* copper to the source (never remove any);
4. every new object's net must resolve to the file's net reference style;
5. the result is parsed again and must contain exactly the source objects plus
   the new ones, with identical geometry;
6. the file is written atomically (temp file, fsync, rename). Overwriting the
   source requires an explicit flag and first makes a backup copy.

Default output name: ``<name>_routed.kicad_pcb`` next to the source.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from pcbrouter import __version__
from pcbrouter.domain.board import Board
from pcbrouter.domain.track import Track
from pcbrouter.domain.units import Nm
from pcbrouter.domain.via import Via

log = logging.getLogger(__name__)

#: KiCad 6.0 (20211014) … KiCad 10 (20260206). Newer formats are refused until tested.
MIN_EXPORT_VERSION = 20211014
MAX_EXPORT_VERSION = 20261231
ROUTED_SUFFIX = "_routed"
SIDECAR_SUFFIX = ".pcbrouter.json"
SIDECAR_FORMAT = "pcbrouter-export-provenance/1"


class ExportStatus(Enum):
    EXPORTED = "EXPORTED"
    EXPORT_BLOCKED_UNSUPPORTED_CONSTRUCT = "EXPORT_BLOCKED_UNSUPPORTED_CONSTRUCT"
    EXPORT_BLOCKED_SOURCE_CHANGED = "EXPORT_BLOCKED_SOURCE_CHANGED"
    EXPORT_BLOCKED_OVERWRITE = "EXPORT_BLOCKED_OVERWRITE"
    EXPORT_BLOCKED_DRC = "EXPORT_BLOCKED_DRC"
    ERROR = "ERROR"


class ExportError(RuntimeError):
    def __init__(self, status: ExportStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class ExportReport:
    status: ExportStatus
    path: Path | None = None
    added_tracks: int = 0
    added_vias: int = 0
    verified: bool = False
    verification: str = "not checked"
    output_sha256: str | None = None
    backup: Path | None = None
    sidecar: Path | None = None
    messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status is ExportStatus.EXPORTED

    def summary(self) -> str:
        if not self.ok:
            return f"{self.status.value}: {'; '.join(self.messages)}"
        return (
            f"Exported {self.path.name if self.path else '?'}: +{self.added_tracks} segment(s), "
            f"+{self.added_vias} via(s) — {self.verification}"
        )


@dataclass(frozen=True, slots=True)
class FileStyle:
    version: int
    quoted_layers: bool
    id_token: str  # "uuid" or "tstamp"
    net_by_code: bool
    newline: str
    quoted_ids: bool = True


def default_export_path(source: Path) -> Path:
    base = source.with_name(source.stem + ROUTED_SUFFIX + source.suffix)
    n = 2
    candidate = base
    while candidate.exists():
        candidate = source.with_name(f"{source.stem}{ROUTED_SUFFIX}-{n}{source.suffix}")
        n += 1
    return candidate


def detect_style(text: str) -> FileStyle:
    m = re.search(r"\(\s*kicad_pcb\s*\(\s*version\s+(\d+)\s*\)", text)
    if m is None:
        raise ExportError(
            ExportStatus.EXPORT_BLOCKED_UNSUPPORTED_CONSTRUCT,
            "the file has no recognisable (kicad_pcb (version …)) header",
        )
    version = int(m.group(1))
    if not MIN_EXPORT_VERSION <= version <= MAX_EXPORT_VERSION:
        raise ExportError(
            ExportStatus.EXPORT_BLOCKED_UNSUPPORTED_CONSTRUCT,
            f"file format version {version} is outside the supported export range "
            f"{MIN_EXPORT_VERSION}–{MAX_EXPORT_VERSION} (KiCad 6–10). Open and save the "
            "board in a supported KiCad version first; nothing was written.",
        )
    quoted = re.search(r'\(layer\s+"', text) is not None or version >= 20211014
    id_token = "uuid" if (version >= 20221018 or "(uuid " in text) else "tstamp"
    net_by_code = re.search(r'\(net\s+\d+\s+"', text) is not None
    newline = "\r\n" if "\r\n" in text else "\n"
    # mimic the file: KiCad 8+ quotes ids ((uuid "…")), KiCad 6/7 do not
    quoted_ids = re.search(rf'\({id_token}\s+"', text) is not None or (
        re.search(rf"\({id_token}\s+[0-9a-fA-F]", text) is None and version >= 20231120
    )
    return FileStyle(version, quoted, id_token, net_by_code, newline, quoted_ids)


def _mm(nm: Nm) -> str:
    sign = "-" if nm < 0 else ""
    whole, frac = divmod(abs(nm), 1_000_000)
    text = f"{sign}{whole}.{frac:06d}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _id(style: FileStyle, value: str) -> str:
    return _q(value) if style.quoted_ids else value


def _layer(style: FileStyle, name: str) -> str:
    return _q(name) if style.quoted_layers else name


def _net(style: FileStyle, net: str | None, codes: dict[str, int]) -> str:
    if not net:
        return "(net 0)" if style.net_by_code else '(net "")'
    if style.net_by_code:
        if net not in codes:
            raise ExportError(
                ExportStatus.EXPORT_BLOCKED_UNSUPPORTED_CONSTRUCT,
                f"net {net!r} has no net code in the source file",
            )
        return f"(net {codes[net]})"
    return f"(net {_q(net)})"


def render_track(t: Track, style: FileStyle, codes: dict[str, int]) -> str:
    if t.mid is not None:
        raise ExportError(
            ExportStatus.EXPORT_BLOCKED_UNSUPPORTED_CONSTRUCT,
            "arc tracks are not generated by this router",
        )
    return (
        f"\t(segment (start {_mm(t.start.x)} {_mm(t.start.y)}) (end {_mm(t.end.x)} "
        f"{_mm(t.end.y)}) (width {_mm(t.width)}) (layer {_layer(style, t.layer)}) "
        f"{_net(style, t.net_name, codes)} ({style.id_token} {_id(style, t.id)}))"
    )


def render_via(v: Via, style: FileStyle, codes: dict[str, int]) -> str:
    if v.drill is None or v.start_layer is None or v.end_layer is None:
        raise ExportError(
            ExportStatus.EXPORT_BLOCKED_UNSUPPORTED_CONSTRUCT,
            f"via {v.id} is incomplete (drill/layers)",
        )
    if v.via_type.value != "through":
        raise ExportError(
            ExportStatus.EXPORT_BLOCKED_UNSUPPORTED_CONSTRUCT, "only through vias are exported"
        )
    return (
        f"\t(via (at {_mm(v.position.x)} {_mm(v.position.y)}) (size {_mm(v.diameter)}) "
        f"(drill {_mm(v.drill)}) (layers {_layer(style, v.start_layer)} "
        f"{_layer(style, v.end_layer)}) {_net(style, v.net_name, codes)} "
        f"({style.id_token} {_id(style, v.id)}))"
    )


def compose(source_text: str, tracks: list[Track], vias: list[Via], codes: dict[str, int]) -> str:
    """Source text + new nodes before the final ')' (nothing else changes)."""
    style = detect_style(source_text)
    if not tracks and not vias:
        return source_text
    end = source_text.rstrip()
    if not end.endswith(")"):
        raise ExportError(
            ExportStatus.EXPORT_BLOCKED_UNSUPPORTED_CONSTRUCT,
            "the file does not end with the board's closing parenthesis",
        )
    cut = len(end) - 1
    nl = style.newline
    body = nl.join(
        [render_track(t, style, codes) for t in tracks]
        + [render_via(v, style, codes) for v in vias]
    )
    head = source_text[:cut].rstrip(" \t")
    if not head.endswith(("\n", "\r")):
        head += nl
    return head + body + nl + source_text[cut:]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    """Temp file in the same directory, fsync, then rename (no truncated files)."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _signature(board: Board) -> tuple[set[tuple[Any, ...]], set[tuple[Any, ...]]]:
    t = {(x.start, x.end, x.width, x.layer, x.net_name) for x in board.tracks}
    v = {(x.position, x.diameter, x.drill, x.net_name) for x in board.vias}
    return t, v


def export_board(
    source_path: Path,
    source_sha256: str,
    source_board: Board,
    working_board: Board,
    out_path: Path,
    *,
    overwrite_source: bool = False,
    backup_dir: Path | None = None,
    provenance: dict[str, str] | None = None,
    verification: tuple[bool, str] = (False, "not checked"),
    extra_metadata: dict[str, Any] | None = None,
) -> ExportReport:
    """Write ``working_board``'s additions onto the source file. Never raises for
    expected problems: returns a report with a blocking status instead."""
    from pcbrouter.kicad.loader import load_board

    report = ExportReport(ExportStatus.ERROR)
    try:
        raw = source_path.read_bytes()
        if _sha(raw) != source_sha256:
            raise ExportError(
                ExportStatus.EXPORT_BLOCKED_SOURCE_CHANGED,
                "the source file changed on disk since it was opened; reopen it "
                "and route again (nothing was written)",
            )
        same = out_path.resolve() == source_path.resolve()
        if same and not overwrite_source:
            raise ExportError(
                ExportStatus.EXPORT_BLOCKED_OVERWRITE,
                "refusing to overwrite the source board without explicit "
                "confirmation; export to a new file instead",
            )
        if out_path.suffix.lower() != ".kicad_pcb":
            raise ExportError(
                ExportStatus.EXPORT_BLOCKED_OVERWRITE, "the output name must end in .kicad_pcb"
            )
        text = raw.decode("utf-8")
        src_t = {t.id for t in source_board.tracks}
        src_v = {v.id for v in source_board.vias}
        work_t = {t.id for t in working_board.tracks}
        work_v = {v.id for v in working_board.vias}
        if not src_t <= work_t or not src_v <= work_v:
            raise ExportError(
                ExportStatus.EXPORT_BLOCKED_UNSUPPORTED_CONSTRUCT,
                "the working board is missing copper from the source file; "
                "export only adds copper",
            )
        tracks = [t for t in working_board.tracks if t.id not in src_t]
        vias = [v for v in working_board.vias if v.id not in src_v]
        codes = {n.name: n.code for n in source_board.nets if n.code is not None}
        out_text = compose(text, tracks, vias, codes)
        data = out_text.encode("utf-8")
        # 5: parse the result again and compare semantically
        with tempfile.TemporaryDirectory() as tmpdir:
            probe = Path(tmpdir) / "probe.kicad_pcb"
            probe.write_bytes(data)
            reloaded = load_board(probe).board
        if _signature(reloaded) != _signature(working_board):
            raise ExportError(
                ExportStatus.ERROR,
                "self-check failed: the exported file does not reload to the "
                "working board (nothing was written)",
            )
        if len(reloaded.components) != len(source_board.components) or len(reloaded.zones) != len(
            source_board.zones
        ):
            raise ExportError(ExportStatus.ERROR, "self-check failed: components/zones differ")
        # 6: write atomically (backup first when overwriting the source)
        if same:
            bdir = backup_dir or source_path.parent
            bdir.mkdir(parents=True, exist_ok=True)
            backup = bdir / f"{source_path.stem}-{time.strftime('%Y%m%d-%H%M%S')}.kicad_pcb.bak"
            shutil.copy2(source_path, backup)
            report.backup = backup
        atomic_write(out_path, data)
        if _sha(out_path.read_bytes()) != _sha(data):
            raise ExportError(ExportStatus.ERROR, "written file does not match (disk problem?)")
        report.status = ExportStatus.EXPORTED
        report.path = out_path
        report.added_tracks, report.added_vias = len(tracks), len(vias)
        report.output_sha256 = _sha(data)
        report.verified, report.verification = verification
        report.sidecar = _write_sidecar(
            out_path,
            source_path,
            source_sha256,
            tracks,
            vias,
            provenance or {},
            report,
            extra_metadata or {},
        )
        log.info(
            "export.done path=%s tracks=%d vias=%d verified=%s sha256=%s",
            out_path,
            len(tracks),
            len(vias),
            report.verified,
            report.output_sha256[:16],
        )
    except ExportError as exc:
        report.status = exc.status
        report.messages.append(str(exc))
        log.warning("export.blocked status=%s reason=%s", exc.status.value, exc)
    except (OSError, UnicodeDecodeError) as exc:
        report.status = ExportStatus.ERROR
        report.messages.append(f"{type(exc).__name__}: {exc}")
        log.error("export.failed error=%r", exc)
    return report


def _objects(tracks: list[Track], vias: list[Via], prov: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in tracks:
        out.append(
            {
                "uuid": t.id,
                "kind": "segment",
                "net": t.net_name,
                "provenance": prov.get(t.id, "router_generated"),
            }
        )
    for v in vias:
        out.append(
            {
                "uuid": v.id,
                "kind": "via",
                "net": v.net_name,
                "provenance": prov.get(v.id, "router_generated"),
            }
        )
    return out


def _write_sidecar(
    out: Path,
    source: Path,
    source_sha: str,
    tracks: list[Track],
    vias: list[Via],
    provenance: dict[str, str],
    report: ExportReport,
    extra: dict[str, Any],
) -> Path | None:
    """Provenance of generated objects lives *beside* the board (KiCad has no
    generic property on tracks; no invalid syntax is injected)."""
    data = {
        "format": SIDECAR_FORMAT,
        "app_version": __version__,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": {"name": source.name, "sha256": source_sha},
        "output_sha256": report.output_sha256,
        "verified": report.verified,
        "verification": report.verification,
        "objects": _objects(tracks, vias, provenance),
        **extra,
    }
    path = out.with_name(out.name + SIDECAR_SUFFIX)
    try:
        atomic_write(path, json.dumps(data, indent=2).encode("utf-8"))
    except OSError as exc:  # the board is written; provenance is a convenience
        report.messages.append(f"provenance sidecar not written: {exc}")
        return None
    return path
