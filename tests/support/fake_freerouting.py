"""Test stand-in for Freerouting's CLI (-de DSN -do SES -mp N --gui.enabled=false).

Routes the board named in the fake DSN with the app's own board router and writes
the added copper as the fake JSON "session". FAKE_FR_SLEEP=<s> simulates a long
run (cancel tests); FAKE_FR_FAIL=1 exits with an error."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    dsn = Path(argv[argv.index("-de") + 1])
    ses = Path(argv[argv.index("-do") + 1])
    if os.environ.get("FAKE_FR_FAIL"):
        print("Exception: simulated Freerouting failure", flush=True)
        return 3
    board_path = Path(json.loads(dsn.read_text(encoding="utf-8"))["board"])
    for i in range(int(float(os.environ.get("FAKE_FR_SLEEP", "0")) * 10)):
        print(f"Auto-routing pass #{i + 1} on board 'x' still running, 9 unrouted", flush=True)
        time.sleep(0.1)
    from pcbrouter.kicad.loader import load_board
    from pcbrouter.kicad.rule_adapter import load_project_rules
    from pcbrouter.routing.board_router import BoardRouter, BoardRouterSettings
    from pcbrouter.routing.working_board import WorkingBoard

    wb = WorkingBoard(load_board(board_path).board, load_project_rules(board_path))
    print("Auto-routing stage started with 5 unrouted nets", flush=True)
    result = BoardRouter(wb, BoardRouterSettings()).run()
    print(
        "Auto-routing pass #1 on board 'x' was completed in 0.10 seconds with score 1", flush=True
    )
    session = {
        "tracks": [
            {
                "a": [t.start.x, t.start.y],
                "b": [t.end.x, t.end.y],
                "w": t.width,
                "layer": t.layer,
                "net": t.net_name,
            }
            for t in result.added_tracks
        ],
        "vias": [
            {
                "at": [v.position.x, v.position.y],
                "d": v.diameter,
                "drill": v.drill,
                "net": v.net_name,
                "layers": [v.start_layer, v.end_layer],
            }
            for v in result.added_vias
        ],
    }
    ses.write_text(json.dumps(session), encoding="utf-8")
    print("Auto-routing was completed in 0.20 seconds with the score of 1.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
