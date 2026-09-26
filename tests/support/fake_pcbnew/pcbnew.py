"""Test stand-in for KiCad's ``pcbnew`` module (Specctra export/import only).

The fake "DSN" names the board file; the fake "SES" is JSON with the copper the
fake router added; SaveBoard appends it using the app's own writer."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any


def Version() -> str:
    return "fake-10.0"


class _Item:
    def SetLocked(self, _v: bool) -> None:
        pass


class _Board:
    def __init__(self, path: str) -> None:
        self.path = path
        self.text = Path(path).read_text(encoding="utf-8")
        self.session: dict[str, Any] = {"tracks": [], "vias": []}

    def GetTracks(self) -> list[_Item]:
        return [_Item()]


def LoadBoard(path: str) -> _Board:
    return _Board(path)


def ExportSpecctraDSN(board: _Board, dsn: str) -> bool:
    Path(dsn).write_text(json.dumps({"board": board.path}), encoding="utf-8")
    return True


def ImportSpecctraSES(board: _Board, ses: str) -> bool:
    board.session = json.loads(Path(ses).read_text(encoding="utf-8"))
    return True


def SaveBoard(out: str, board: _Board) -> bool:
    from pcbrouter.domain.geometry import Point
    from pcbrouter.domain.track import Track
    from pcbrouter.domain.via import Via, ViaType
    from pcbrouter.kicad.loader import load_board
    from pcbrouter.kicad.writer import compose

    src = load_board(board.path).board
    codes = {n.name: n.code for n in src.nets if n.code is not None}
    tracks = [
        Track(str(uuid.uuid4()), Point(*t["a"]), Point(*t["b"]), t["w"], t["layer"], t["net"])
        for t in board.session["tracks"]
    ]
    vias = [
        Via(
            str(uuid.uuid4()),
            Point(*v["at"]),
            v["d"],
            v["drill"],
            v["net"],
            v["layers"][0],
            v["layers"][1],
            ViaType.THROUGH,
        )
        for v in board.session["vias"]
    ]
    Path(out).write_text(compose(board.text, tracks, vias, codes), encoding="utf-8")
    return True
