"""``--worker-selftest``: prove the routing worker process starts and answers in
this installation (development tree or frozen build), without a GUI."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from pcbrouter.jobs.protocol import FakeJob, JobDone, RouteBoardJob, RouteProgress, WorkingSnapshot


def _run(host: Any, job_id: int, job: Any, timeout_s: float) -> tuple[JobDone | None, int]:
    host.submit(job_id, job)
    progress = 0
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        for msg in host.poll(budget_s=0.05):
            if isinstance(msg, RouteProgress) and msg.job_id == job_id:
                progress += 1
            if isinstance(msg, JobDone) and msg.job_id == job_id:
                return msg, progress
        if not host.alive():
            return None, progress
        time.sleep(0.02)  # CLI only: no GUI event loop exists here
    return None, progress


def run_worker_selftest(board: Path | None = None) -> dict[str, Any]:
    from pcbrouter.ui.route_jobs import WorkerHost

    t0 = time.monotonic()
    host = WorkerHost()
    report: dict[str, Any] = {"worker_pid": host.process.pid}
    try:
        done, n = _run(host, 1, FakeJob(duration_s=1.0), 60.0)
        report["fake_job"] = {
            "status": done.status if done else "NO RESPONSE",
            "progress_messages": n,
            "exit_code": host.exitcode,
        }
        report["startup_and_fake_s"] = round(time.monotonic() - t0, 2)
        if board is not None and done is not None:
            from pcbrouter.kicad.loader import load_board
            from pcbrouter.kicad.rule_adapter import load_project_rules
            from pcbrouter.routing.board_router import BoardRouterSettings
            from pcbrouter.routing.working_board import WorkingBoard

            wb = WorkingBoard(load_board(board).board, load_project_rules(board))
            job = RouteBoardJob(WorkingSnapshot.from_working(wb), BoardRouterSettings())
            done2, n2 = _run(host, 2, job, 600.0)
            value = getattr(done2, "value", None)
            report["board_job"] = {
                "status": done2.status if done2 else "NO RESPONSE",
                "progress_messages": n2,
                "result": value.summary() if value is not None else None,
                "error": done2.error if done2 else "",
                "backend": done2.backend.text() if done2 and done2.backend else None,
            }
    finally:
        host.stop()
        host.kill()
    ok = report["fake_job"]["status"] == "COMPLETED" and (
        "board_job" not in report or report["board_job"]["status"] == "COMPLETED"
    )
    report["ok"] = ok
    return report
