from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

from pcbrouter.kicad import (
    BoardFileNotFoundError,
    BoardPermissionError,
    MalformedBoardError,
    WrongFileTypeError,
    load_board,
    sha256_of_file,
)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(BoardFileNotFoundError) as info:
        load_board(tmp_path / "nope.kicad_pcb")
    assert "not found" in info.value.user_message.lower()


@pytest.mark.parametrize(
    ("name", "hint"),
    [
        ("proj.kicad_pro", "project file"),
        ("sheet.kicad_sch", "schematic"),
        ("board.brd", "not supported"),
        ("notes.txt", "Only KiCad board files"),
        ("noext", "Only KiCad board files"),
    ],
)
def test_wrong_extension_has_helpful_hint(tmp_path: Path, name: str, hint: str) -> None:
    path = tmp_path / name
    path.write_text("(kicad_pcb)")
    with pytest.raises(WrongFileTypeError) as info:
        load_board(path)
    assert hint in info.value.user_message


def test_extension_check_is_case_insensitive(
    tmp_path: Path, fixture_path: Callable[[str], Path]
) -> None:
    upper = tmp_path / "BOARD.KICAD_PCB"
    upper.write_bytes(fixture_path("empty.kicad_pcb").read_bytes())
    assert load_board(upper).board.components == ()


def test_directory_is_rejected(tmp_path: Path) -> None:
    d = tmp_path / "dir.kicad_pcb"
    d.mkdir()
    with pytest.raises(BoardFileNotFoundError):
        load_board(d)


def test_permission_error_is_translated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture_path: Callable[[str], Path]
) -> None:
    # Simulated: chmod-based tests are unreliable as root and on Windows.
    target = tmp_path / "locked.kicad_pcb"
    target.write_bytes(fixture_path("empty.kicad_pcb").read_bytes())
    original_open = Path.open

    def deny(self: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if self == target:
            raise PermissionError(13, "Permission denied")
        return original_open(self, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(Path, "open", deny)
    with pytest.raises(BoardPermissionError) as info:
        load_board(target)
    assert "Permission denied" in info.value.user_message


def test_binary_garbage_is_malformed(tmp_path: Path) -> None:
    path = tmp_path / "garbage.kicad_pcb"
    path.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\x00")
    with pytest.raises(MalformedBoardError, match="UTF-8"):
        load_board(path)


def test_empty_file_is_malformed(tmp_path: Path) -> None:
    path = tmp_path / "empty.kicad_pcb"
    path.write_bytes(b"")
    with pytest.raises(MalformedBoardError, match="empty"):
        load_board(path)


def test_utf8_bom_is_accepted(tmp_path: Path, fixture_path: Callable[[str], Path]) -> None:
    path = tmp_path / "bom.kicad_pcb"
    path.write_bytes(b"\xef\xbb\xbf" + fixture_path("empty.kicad_pcb").read_bytes())
    assert load_board(path).board.metadata.format_version == 20240108


def test_stats_and_hash(fixture_path: Callable[[str], Path]) -> None:
    path = fixture_path("traces.kicad_pcb")
    result = load_board(path)
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    assert result.stats.sha256 == expected == sha256_of_file(path)
    assert result.stats.file_size_bytes == path.stat().st_size
    assert result.stats.parse_seconds >= 0 and result.stats.total_seconds >= 0
    assert result.path == path.resolve()
