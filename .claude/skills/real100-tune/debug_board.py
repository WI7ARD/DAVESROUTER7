"""Route one prepared Real100 board and explain every failed net.

    python .claude/skills/real100-tune/debug_board.py K037 [speed|accuracy] [budget_s]

Prints the project rules found, unsupported rules, pre-route connectivity, the plan
and each failed net with its reason and message. Same settings as the Real100
worker (conservative rules, one worker, 30 s per-search limit).
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from pcbrouter.board_engine import EngineConfig  # noqa: E402
from pcbrouter.kicad.loader import load_board  # noqa: E402
from pcbrouter.kicad.rule_adapter import load_project_rules  # noqa: E402
from pcbrouter.routing.board_router import BoardRouter, BoardRouterSettings, make_plan  # noqa: E402
from pcbrouter.routing.presets import RouteMode, adjust_board_settings  # noqa: E402
from pcbrouter.routing.request import RouteRequest  # noqa: E402
from pcbrouter.routing.working_board import WorkingBoard  # noqa: E402


def main() -> int:
    bid = sys.argv[1]
    mode = RouteMode(sys.argv[2] if len(sys.argv) > 2 else "speed")
    budget = float(sys.argv[3]) if len(sys.argv) > 3 else 60.0
    path = next((ROOT / "benchmarks/real100/work/prepared" / bid).glob("*.kicad_pcb"))
    loaded = load_board(path)
    rules = load_project_rules(path)
    print(f"{bid} {path.name}: project rules {'found' if rules.found_any else 'MISSING'}")
    wb = WorkingBoard(loaded.board, rules, config=EngineConfig(conservative=True))
    for u in wb.engine.resolver.ruleset.unsupported[:10]:
        print(f"  unsupported rule {u.name!r} critical={u.critical}")
    conn = wb.engine.connectivity
    print("  connectivity:", conn.metrics())
    print("  net status:", dict(Counter(c.status.value for c in conn.nets.values())))
    base = RouteRequest("", candidates=1, time_limit_s=30.0)
    st = adjust_board_settings(BoardRouterSettings(budget_s=budget, base_request=base), base, mode)
    plan = make_plan(wb, st)
    print(f"  planned {len(plan.tasks)} net(s) {plan.notes}")
    t0 = time.perf_counter()
    res = BoardRouter(wb, st).run(plan)
    print(f"{res.summary()} ({time.perf_counter() - t0:.1f} s)")
    reasons = Counter(
        (o.reason.value if o.reason else o.status.value)
        for o in res.outcomes.values()
        if o.status.value != "SUCCESS"
    )
    print("  failed by reason:", dict(reasons))
    for net, o in res.outcomes.items():
        if o.status.value != "SUCCESS":
            why = o.reason.value if o.reason else o.status.value
            print(f"  FAIL {net}: {why}: {(o.message or '')[:220]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
