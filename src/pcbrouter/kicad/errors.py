"""KiCad loading errors.

Every error carries a short, user-facing ``user_message`` (shown in dialogs) and the
full technical detail in ``str(error)`` (written to the log).
"""

from __future__ import annotations

from pathlib import Path


class KiCadLoadError(Exception):
    """Base class for all board loading failures."""

    def __init__(self, message: str, *, path: Path | None = None, user_message: str | None = None):
        super().__init__(message)
        self.path = path
        self.user_message = user_message or message


class BoardFileNotFoundError(KiCadLoadError):
    """The path does not exist or is not a regular file."""


class WrongFileTypeError(KiCadLoadError):
    """The path is not a ``.kicad_pcb`` file (e.g. a schematic or project file)."""


class BoardPermissionError(KiCadLoadError):
    """The operating system refused read access."""


class MalformedBoardError(KiCadLoadError):
    """The file is not valid KiCad S-expression board data."""

    def __init__(
        self,
        message: str,
        *,
        path: Path | None = None,
        line: int | None = None,
        column: int | None = None,
        user_message: str | None = None,
    ):
        location = f" (line {line}, column {column})" if line is not None else ""
        super().__init__(
            f"{message}{location}",
            path=path,
            user_message=user_message or f"The board file is malformed: {message}{location}",
        )
        self.line = line
        self.column = column


class UnsupportedKiCadVersion(KiCadLoadError):  # noqa: N818 - name mandated by spec
    """The board's file-format version predates what this application supports."""

    def __init__(self, version: int, minimum: int, *, path: Path | None = None):
        super().__init__(
            f"KiCad board format version {version} is older than the minimum supported "
            f"version {minimum} (KiCad 5).",
            path=path,
            user_message=(
                f"This board was saved by a KiCad version that is too old (format {version}). "
                "Open and re-save it in KiCad 5 or newer, then try again."
            ),
        )
        self.version = version
        self.minimum = minimum
