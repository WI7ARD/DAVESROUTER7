"""``pcbrouter --route BOARD`` — route a board without the GUI.

Same pipeline as Router ▸ Route Board: the board's own KiCad rules, the Speed or
Accuracy preset, optional parallel helpers, the exact validator on every commit,
then the normal export (internal geometry check gate, append-only write, reload
self-check, optional KiCad DRC). The source board is never changed. Used by the
Windows release smoke test against the installed application.

Exit codes: 0 fully routed and exported, 2 partially routed (exported), 1 error.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any


def route_cli(
    board: Path,
    output: Path | None,
    mode: str,
    workers: int,
    budget_s: float,
    report_path: Path | None,
    kicad_drc: bool,
) -> int:
    from dataclasses import replace

    from pcbrouter.commands.export_commands import perform_export
    from pcbrouter.kicad.loader import load_board
    from pcbrouter.kicad.rule_adapter import load_project_rules
    from pcbrouter.kicad.writer import default_export_path
    from pcbrouter.routing.board_router import BoardRouter, BoardRouterSettings, BoardStatus
    from pcbrouter.routing.parallel import auto_workers
    from pcbrouter.routing.presets import RouteMode, adjust_board_settings
    from pcbrouter.routing.request import RouteRequest
    from pcbrouter.routing.result import RouteStatus
    from pcbrouter.routing.working_board import Provenance, WorkingBoard

    report: dict[str, Any] = {"board": str(board), "mode": mode}

    def finish(code: int, message: str) -> int:
        print(message)
        report["exit_code"], report["message"] = code, message
        if report_path is not None:
            report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        return code

    if not board.is_file():
        return finish(1, f"Board not found: {board}")
    out = output or default_export_path(board)
    if out.resolve() == board.resolve():
        return finish(1, "Refusing to overwrite the source board; choose another --output.")
    try:
        load = load_board(board)
    except Exception as exc:  # malformed/unsupported file: a message, not a traceback
        return finish(1, f"Could not read {board.name}: {exc}")
    rules = load_project_rules(board)
    wb = WorkingBoard(load.board, rules)
    geo = wb.engine.geometry
    report.update(
        layers=list(geo.copper_layers),
        pads=len(load.board.pads),
        nets=len(load.board.nets),
        rules_project=str(rules.project_path) if rules.project_path else None,
        rules_dru=str(rules.dru_path) if rules.dru_path else None,
    )
    print(f"{board.name}: {len(geo.copper_layers)} copper layers, {len(load.board.pads)} pads, "
          f"{len(load.board.nets)} nets")  # fmt: skip
    if rules.project_path is None:
        print("No .kicad_pro next to the board: design rules are unknown, so nets will be "
              "reported as RULE_UNKNOWN (no rules are guessed).")  # fmt: skip
    base = RouteRequest("", candidates=1)
    n_workers = auto_workers(workers)
    settings = adjust_board_settings(
        BoardRouterSettings(base_request=base, parallel_workers=n_workers), base, RouteMode(mode)
    )
    settings = replace(settings, budget_s=budget_s)
    report["workers"] = n_workers
    last = [0.0]

    def progress(info: dict[str, Any]) -> None:
        now = time.monotonic()
        if info.get("state") != "routing" or now - last[0] < 2.0:
            return
        last[0] = now
        print(f"  pass {info.get('pass_no')}: net {info.get('index')}/{info.get('total')} "
              f"({info.get('completed_nets', 0)} done) {str(info.get('net'))[:60]}",
              flush=True)  # fmt: skip

    t0 = time.monotonic()
    result = BoardRouter(wb, settings).run(progress=progress)
    report["route_s"] = round(time.monotonic() - t0, 1)
    report["summary"] = result.summary()
    report["metrics"] = result.metrics.to_dict()
    print(result.summary())
    failed = {
        n: f"{o.status.value}: {o.message}"
        for n, o in result.outcomes.items()
        if o.status not in (RouteStatus.SUCCESS, RouteStatus.ALREADY_CONNECTED)
    }
    report["failed_nets"] = failed
    for net, why in list(failed.items())[:20]:
        print(f"  not routed: {net} — {why[:160]}")
    if len(failed) > 20:
        print(f"  … and {len(failed) - 20} more (see --report)")
    tracks, vias, removed = result.objects_for(None)
    if not tracks and not vias:
        return finish(2 if result.status is not BoardStatus.FAILED else 1,
                      "Nothing was routed; no file written.")  # fmt: skip
    wb.commit_objects(tracks, vias, removed, "route (CLI)", Provenance.ROUTER_GENERATED)
    with tempfile.TemporaryDirectory(prefix="pcbrouter-route-") as tmp:
        exp = perform_export(
            wb, board, load.stats.sha256, out, backup_dir=Path(tmp), allow_unverified=True,
            run_kicad_drc=kicad_drc,
        )  # fmt: skip
    report["export"] = exp.summary()
    report["output"] = str(exp.path) if exp.path else None
    report["verified"] = exp.verified
    if not exp.ok:
        return finish(1, f"Export failed: {exp.summary()}")
    print(exp.summary())
    for m in exp.messages:
        print(f"  {m}")
    code = 0 if result.status is BoardStatus.FULLY_ROUTED else 2
    return finish(code, f"Wrote {exp.path}")
