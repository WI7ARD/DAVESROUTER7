"""``--setup-freerouting`` and ``--freeroute BOARD --output OUT`` (no GUI).

``--freeroute`` runs the same pipeline as Router ▸ Route Board with Freerouting:
KiCad exports the board, Freerouting routes it, every net is re-checked by the
exact validator, and only accepted copper is written — to a new file through the
normal export (DRC gate, reload self-check). The source board is never changed.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from pcbrouter.settings.settings import SettingsStore


def setup_freerouting_cli(board: Path | None) -> int:
    from pcbrouter.routing import freerouting_setup as frs

    state = frs.check_setup(SettingsStore().load().routing.freerouting_path)
    print(state.text())
    if not state.ready:
        return 1
    if board is None:
        print("Give a board to also test it: --setup-freerouting BOARD.kicad_pcb")
        return 0
    assert state.tool is not None and state.kicad is not None
    try:
        print(frs.selftest(board, state.tool, state.kicad, lambda m: print("  " + m[-160:])))
    except Exception as exc:
        print(f"Test failed: {exc}")
        return 1
    return 0


def freeroute_cli(board: Path, output: Path | None, passes: int | None) -> int:
    from pcbrouter.commands.export_commands import perform_export
    from pcbrouter.kicad.loader import load_board
    from pcbrouter.kicad.rule_adapter import load_project_rules
    from pcbrouter.kicad.writer import default_export_path
    from pcbrouter.routing.board_router import BoardStatus
    from pcbrouter.routing.freerouting import FreeroutingError, find_freerouting, freeroute
    from pcbrouter.routing.result import RouteStatus
    from pcbrouter.routing.working_board import Provenance, WorkingBoard

    st = SettingsStore().load().routing
    tool = find_freerouting(st.freerouting_path)
    if tool is None:
        print("Freerouting was not found; run --setup-freerouting (or Tools ▸ Set Up "
              "Freerouting… in the app).")  # fmt: skip
        return 1
    out = output or default_export_path(board)
    if out.resolve() == board.resolve():
        print("Refusing to overwrite the source board; choose another --output.")
        return 1
    load = load_board(board)
    working = WorkingBoard(load.board, load_project_rules(board))
    last = [""]

    def show(phase: str, info: dict[str, Any]) -> None:
        line = f"[{phase}] " + str(info.get("message", ""))[-140:]
        if line != last[0]:
            print(line, flush=True)
            last[0] = line

    try:
        result = freeroute(working, board, tool, passes or st.freerouting_passes, show)
    except FreeroutingError as exc:
        print(f"Freerouting failed: {exc}")
        return 1
    except Exception as exc:
        print(f"Freerouting failed: {type(exc).__name__}: {exc}")
        return 1
    print(result.summary())
    for o in result.outcomes.values():
        if o.status not in (RouteStatus.SUCCESS, RouteStatus.ALREADY_CONNECTED):
            print(f"  {o.net}: {o.status.value} {o.message}".rstrip())
    tracks, vias, removed = result.objects_for(None)
    if not tracks and not vias:
        print("Nothing new to write.")
        return 1
    working.commit_objects(tracks, vias, removed, "Freerouting (CLI)", Provenance.ROUTER_GENERATED)
    with tempfile.TemporaryDirectory(prefix="pcbrouter-frcli-") as tmp:
        report = perform_export(working, board, load.stats.sha256, out, backup_dir=Path(tmp))
    print(report.summary())
    if report.ok and result.status is not BoardStatus.FULLY_ROUTED:
        print("Some nets are not routed (listed above); finish them in KiCad or the app.")
    return 0 if report.ok else 1
