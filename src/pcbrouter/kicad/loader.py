"""Read-only entry point for loading ``.kicad_pcb`` files.

Safety contract: this module opens board files **only** in binary read mode and
never writes, renames, locks or touches them. The SHA-256 of the exact bytes read
is returned so callers can later prove the source is unchanged.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from pcbrouter.domain.board import Board
from pcbrouter.kicad.adapter import KiCadBoardAdapter, LoadWarning
from pcbrouter.kicad.errors import (
    BoardFileNotFoundError,
    BoardPermissionError,
    KiCadLoadError,
    MalformedBoardError,
    WrongFileTypeError,
)
from pcbrouter.kicad.parser import parse_sexpr

log = logging.getLogger(__name__)

BOARD_SUFFIX = ".kicad_pcb"
#: Refuse absurdly large inputs instead of exhausting memory (largest real-world
#: boards are tens of MB).
MAX_FILE_BYTES = 512 * 1024 * 1024

_WRONG_TYPE_HINTS = {
    ".kicad_pro": "That is a KiCad project file. Open the .kicad_pcb file next to it.",
    ".pro": "That is a legacy KiCad project file. Open the .kicad_pcb file next to it.",
    ".kicad_sch": "That is a schematic. Open the board (.kicad_pcb) instead.",
    ".sch": "That is a legacy schematic. Open the board (.kicad_pcb) instead.",
    ".brd": "Legacy KiCad 4 .brd boards are not supported. Re-save the board in KiCad 5+.",
    ".kicad_mod": "That is a single footprint, not a board.",
}


@dataclass(frozen=True, slots=True)
class LoadStats:
    file_size_bytes: int
    sha256: str
    read_seconds: float
    parse_seconds: float
    build_seconds: float

    @property
    def total_seconds(self) -> float:
        return self.read_seconds + self.parse_seconds + self.build_seconds


@dataclass(frozen=True, slots=True)
class LoadResult:
    board: Board
    warnings: tuple[LoadWarning, ...]
    stats: LoadStats
    path: Path


def sha256_of_file(path: Path) -> str:
    """SHA-256 of a file, streamed in read-only binary mode."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_board_path(path: Path | str) -> Path:
    """Check that ``path`` names an existing, readable ``.kicad_pcb`` file."""
    p = Path(path).expanduser()
    suffix = p.suffix.lower()
    if suffix != BOARD_SUFFIX:
        hint = _WRONG_TYPE_HINTS.get(suffix, "Only KiCad board files (.kicad_pcb) can be opened.")
        raise WrongFileTypeError(
            f"unsupported file extension {p.suffix or '(none)'!r} for {p}",
            path=p,
            user_message=f"'{p.name}' is not a KiCad board file. {hint}",
        )
    if not p.exists():
        raise BoardFileNotFoundError(
            f"board file does not exist: {p}", path=p, user_message=f"File not found:\n{p}"
        )
    if not p.is_file():
        raise BoardFileNotFoundError(
            f"not a regular file: {p}", path=p, user_message=f"'{p}' is not a file."
        )
    return p.resolve()


def read_board_bytes(path: Path) -> bytes:
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise KiCadLoadError(
                f"file too large: {size} bytes (limit {MAX_FILE_BYTES})",
                path=path,
                user_message=f"'{path.name}' is too large to open ({size / 1e6:.0f} MB).",
            )
        with path.open("rb") as fh:  # read-only, binary: never modifies the file
            return fh.read()
    except PermissionError as exc:
        raise BoardPermissionError(
            f"permission denied reading {path}: {exc}",
            path=path,
            user_message=f"Permission denied: cannot read '{path.name}'.",
        ) from exc
    except OSError as exc:
        if isinstance(exc, KiCadLoadError):  # pragma: no cover - defensive
            raise
        raise KiCadLoadError(
            f"could not read {path}: {exc}",
            path=path,
            user_message=f"Could not read '{path.name}': {exc.strerror or exc}",
        ) from exc


def load_board(path: Path | str) -> LoadResult:
    """Load a KiCad board read-only and translate it into the domain model."""
    p = validate_board_path(path)
    log.info("board.load.start path=%s", p)

    t0 = time.perf_counter()
    data = read_board_bytes(p)
    sha = hashlib.sha256(data).hexdigest()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MalformedBoardError(
            f"file is not valid UTF-8 text (byte offset {exc.start})",
            path=p,
            user_message=f"'{p.name}' is not a text KiCad board (invalid UTF-8 data).",
        ) from exc
    t1 = time.perf_counter()

    try:
        root = parse_sexpr(text)
    except MalformedBoardError as exc:
        exc.path = p
        raise
    t2 = time.perf_counter()

    adapter = KiCadBoardAdapter(root, source_path=p, text=text)
    try:
        board = adapter.build()
    except KiCadLoadError as exc:
        exc.path = p
        raise
    t3 = time.perf_counter()

    stats = LoadStats(
        file_size_bytes=len(data),
        sha256=sha,
        read_seconds=t1 - t0,
        parse_seconds=t2 - t1,
        build_seconds=t3 - t2,
    )
    warnings = tuple(adapter.warnings)
    s = board.statistics
    log.info(
        "board.load.done path=%s bytes=%d sha256=%s format=%s read_ms=%.1f parse_ms=%.1f "
        "build_ms=%.1f footprints=%d pads=%d nets=%d tracks=%d vias=%d copper_layers=%d "
        "warnings=%d",
        p, stats.file_size_bytes, sha[:16], board.metadata.format_version,
        stats.read_seconds * 1e3, stats.parse_seconds * 1e3, stats.build_seconds * 1e3,
        s.footprint_count, s.pad_count, s.net_count, s.track_count, s.via_count,
        s.copper_layer_count, len(warnings),
    )  # fmt: skip
    for w in warnings:
        log.log(
            logging.WARNING if w.severity.value == "warning" else logging.INFO,
            "board.load.note %s",
            w,
        )
    return LoadResult(board=board, warnings=warnings, stats=stats, path=p)
