"""Runs inside KiCad's own Python (NOT the app's): Specctra DSN export / SES import.

    python kicad_specctra.py export BOARD.kicad_pcb OUT.dsn
    python kicad_specctra.py import BOARD.kicad_pcb IN.ses OUT.kicad_pcb

Only file paths come in; nothing is evaluated. Existing tracks/vias are locked in
the (temporary) copy before export, so the autorouter treats them as fixed wiring
and never rips up copper that is already on the board.
"""

import sys
from typing import Any

import pcbnew  # type: ignore[import-not-found]  # KiCad's module


def _call(fn: Any, board: Any, path: str) -> Any:
    try:
        return fn(board, path)  # KiCad 7+: (board, filename)
    except TypeError:
        return fn(path)  # older: acts on the current board


def export(src: str, dsn: str) -> None:
    board = pcbnew.LoadBoard(src)
    for item in board.GetTracks():
        item.SetLocked(True)
    if _call(pcbnew.ExportSpecctraDSN, board, dsn) is False:
        raise SystemExit("KiCad could not export the Specctra DSN file")


def import_(src: str, ses: str, out: str) -> None:
    board = pcbnew.LoadBoard(src)
    if _call(pcbnew.ImportSpecctraSES, board, ses) is False:
        raise SystemExit("KiCad could not import the Specctra session file")
    pcbnew.SaveBoard(out, board)


def main(argv: list[str]) -> None:
    if len(argv) >= 4 and argv[1] == "export":
        export(argv[2], argv[3])
    elif len(argv) >= 5 and argv[1] == "import":
        import_(argv[2], argv[3], argv[4])
    else:
        raise SystemExit("usage: kicad_specctra.py export BOARD DSN | import BOARD SES OUT")
    print("OK")  # noqa: T201 - read by the calling app


if __name__ == "__main__":
    main(sys.argv)
