"""KiCad adapter layer.

Public surface: :func:`load_board` plus the error types. Nothing else in the
application may import from :mod:`pcbrouter.kicad.parser` — S-expression nodes are
an implementation detail of this package.

Stage 9 will add a *writer* here (``writer.py``). Until then this package is
strictly read-only.
"""

from __future__ import annotations

from pcbrouter.kicad.adapter import LoadWarning, WarningSeverity
from pcbrouter.kicad.errors import (
    BoardFileNotFoundError,
    BoardPermissionError,
    KiCadLoadError,
    MalformedBoardError,
    UnsupportedKiCadVersion,
    WrongFileTypeError,
)
from pcbrouter.kicad.loader import (
    BOARD_SUFFIX,
    LoadResult,
    LoadStats,
    load_board,
    sha256_of_file,
    validate_board_path,
)

__all__ = [
    "BOARD_SUFFIX",
    "BoardFileNotFoundError",
    "BoardPermissionError",
    "KiCadLoadError",
    "LoadResult",
    "LoadStats",
    "LoadWarning",
    "MalformedBoardError",
    "UnsupportedKiCadVersion",
    "WarningSeverity",
    "WrongFileTypeError",
    "load_board",
    "sha256_of_file",
    "validate_board_path",
]
