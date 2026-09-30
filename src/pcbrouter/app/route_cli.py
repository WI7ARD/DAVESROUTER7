"""``pcbrouter --route BOARD`` — route a board without the GUI.

Same pipeline as Router ▸ Route Board: the board's own KiCad rules, the Speed or
Accuracy preset, optional parallel helpers, the exact validator on every commit,
then the normal export (internal geometry check, append-only write, reload
self-check, optional KiCad DRC). The source board is never changed unless
``--overwrite`` is given, and then a timestamped backup is written first. Used by
the Windows release smoke test against the installed application.

Exit codes: 0 fully routed and exported · 3 partially routed (exported) ·
1 error (nothing written, or the export failed) · 2 command-line usage error.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXIT_OK, EXIT_ERROR, EXIT_PARTIAL = 0, 1, 3


@dataclass
class HeadlessRun:
    """One headless routing command: report dict, messages and the exit code."""

    board: Path
    report_path: Path | None
    report: dict[str, Any] = field(default_factory=dict)

    def finish(self, code: int, message: str) -> int:
        print(message)
        self.report["exit_code"], self.report["message"] = code, message
        if self.report_path is not None:
            try:
                self.report_path.write_text(
                    json.dumps(self.report, indent=2, default=str), encoding="utf-8"
                )
            except OSError as exc:
                print(f"Could not write the report {self.report_path}: {exc}")
                return EXIT_ERROR
        return code

    def output_path(self, output: Path | None, overwrite: bool) -> Path | str:
        """The output file, or an error message."""
        from pcbrouter.kicad.writer import default_export_path

        out = output or default_export_path(self.board)
        if out.suffix.lower() != ".kicad_pcb":
            return f"The output must be a .kicad_pcb file: {out}"
        if out.resolve() == self.board.resolve() and not overwrite:
            return ("Refusing to overwrite the source board; choose another --output "
                    "(or add --overwrite: a backup is made first).")  # fmt: skip
        if not out.parent.is_dir():
            return f"Output folder does not exist: {out.parent}"
        return out

    def load(self) -> Any:
        """(load result, WorkingBoard) or an error message."""
        from pcbrouter.kicad.loader import load_board
        from pcbrouter.kicad.rule_adapter import load_project_rules
        from pcbrouter.routing.working_board import WorkingBoard

        if not self.board.is_file():
            return f"Board not found: {self.board}"
        try:
            load = load_board(self.board)
        except Exception as exc:  # malformed/unsupported file: a message, not a traceback
            return f"Could not read {self.board.name}: {exc}"
        rules = load_project_rules(self.board)
        wb = WorkingBoard(load.board, rules)
        geo = wb.engine.geometry
        self.report.update(
            board=str(self.board),
            format_version=load.board.metadata.format_version,
            layers=list(geo.copper_layers),
            pads=len(load.board.pads),
            nets=len(load.board.nets),
            rules_project=str(rules.project_path) if rules.project_path else None,
            rules_dru=str(rules.dru_path) if rules.dru_path else None,
        )  # fmt: skip
        print(f"{self.board.name}: {len(geo.copper_layers)} copper layers "
              f"({', '.join(geo.copper_layers)}), {len(load.board.pads)} pads, "
              f"{len(load.board.nets)} nets")  # fmt: skip
        if rules.project_path is None:
            print("No .kicad_pro next to the board: design rules are unknown, so nets will be "
                  "reported as RULE_UNKNOWN (no rules are guessed).")  # fmt: skip
        return load, wb

    def export(self, load: Any, wb: Any, result: Any, out: Path, overwrite: bool,
               kicad_drc: bool, what: str) -> int:  # fmt: skip
        """Commit the result's copper, export through the normal gate, exit code."""
        from pcbrouter.commands.export_commands import perform_export
        from pcbrouter.routing.board_router import BoardStatus
        from pcbrouter.routing.result import RouteStatus
        from pcbrouter.routing.working_board import Provenance

        self.report["summary"] = result.summary()
        self.report["metrics"] = result.metrics.to_dict()
        print(result.summary())
        failed = {
            n: f"{o.status.value}: {o.message}"
            for n, o in result.outcomes.items()
            if o.status not in (RouteStatus.SUCCESS, RouteStatus.ALREADY_CONNECTED)
        }
        self.report["failed_nets"] = failed
        for net, why in list(failed.items())[:20]:
            print(f"  not routed: {net} — {why[:160]}")
        if len(failed) > 20:
            print(f"  … and {len(failed) - 20} more (see --report)")
        tracks, vias, removed = result.objects_for(None)
        if result.status is BoardStatus.NOTHING_TO_ROUTE:
            msg = "Nothing to route: every net is already connected; no file written."
            return self.finish(EXIT_OK, msg)
        if not tracks and not vias:
            return self.finish(EXIT_ERROR, "Nothing was routed; no file written.")
        wb.commit_objects(tracks, vias, removed, what, Provenance.ROUTER_GENERATED)
        backups = self.board.parent / "pcbrouter-backups" if overwrite else None
        with tempfile.TemporaryDirectory(prefix="pcbrouter-route-") as tmp:
            exp = perform_export(
                wb, self.board, load.stats.sha256, out, backup_dir=backups or Path(tmp),
                allow_unverified=True, run_kicad_drc=kicad_drc,
                overwrite_source=overwrite and out.resolve() == self.board.resolve(),
            )  # fmt: skip
        self.report["export"] = exp.summary()
        self.report["output"] = str(exp.path) if exp.path else None
        self.report["verified"] = exp.verified
        if exp.backup is not None:
            self.report["backup"] = str(exp.backup)
            print(f"Backup of the original: {exp.backup}")
        if not exp.ok:
            return self.finish(EXIT_ERROR, f"Export failed: {exp.summary()}")
        print(exp.summary())
        for m in exp.messages:
            print(f"  {m}")
        code = EXIT_OK if result.status is BoardStatus.FULLY_ROUTED else EXIT_PARTIAL
        return self.finish(code, f"Wrote {exp.path}")


def _resource_usage() -> dict[str, float | None]:
    """CPU seconds and peak memory of this process plus finished helper processes
    (Unix: getrusage; Windows: this process only, via GetProcessMemoryInfo)."""
    try:
        import resource

        me, kids = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(
            resource.RUSAGE_CHILDREN
        )
        cpu = me.ru_utime + me.ru_stime + kids.ru_utime + kids.ru_stime
        unit = 1.0 if sys.platform == "darwin" else 1024.0  # bytes on macOS, KiB on Linux
        return {"cpu_s": round(cpu, 1), "peak_rss_mb": round(me.ru_maxrss * unit / 2**20, 1),
                "helper_peak_rss_mb": round(kids.ru_maxrss * unit / 2**20, 1) or None}  # fmt: skip
    except ImportError:
        pass
    try:  # Windows
        import ctypes
        from ctypes import wintypes

        class _Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (n, ctypes.c_size_t)
                for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                          "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                          "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")
            ]  # fmt: skip

        c = _Counters()
        c.cb = ctypes.sizeof(c)
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        proc = k32.GetCurrentProcess()
        ctypes.windll.psapi.GetProcessMemoryInfo(proc, ctypes.byref(c), c.cb)  # type: ignore[attr-defined]
        times = [wintypes.FILETIME() for _ in range(4)]
        k32.GetProcessTimes(proc, *[ctypes.byref(t) for t in times])

        def secs(ft: Any) -> float:
            return float((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7

        cpu_s = round(secs(times[2]) + secs(times[3]), 1)
        peak = round(c.PeakWorkingSetSize / 2**20, 1)
        return {"cpu_s": cpu_s, "peak_rss_mb": peak, "helper_peak_rss_mb": None}
    except Exception:
        return {"cpu_s": None, "peak_rss_mb": None, "helper_peak_rss_mb": None}


def route_cli(
    board: Path,
    output: Path | None,
    mode: str,
    workers: int,
    budget_s: float,
    report_path: Path | None,
    kicad_drc: bool,
    *,
    backend: str = "cpu",
    layers: str | None = None,
    grid_mm: float | None = None,
    overwrite: bool = False,
    record_experience: bool = False,
    policy: str | None = None,
) -> int:
    from dataclasses import replace

    from pcbrouter.domain.units import mm_to_internal
    from pcbrouter.jobs.execute import JobContext
    from pcbrouter.routing.board_router import BoardRouter, BoardRouterSettings
    from pcbrouter.routing.parallel import auto_workers
    from pcbrouter.routing.presets import RouteMode, adjust_board_settings
    from pcbrouter.routing.request import RouteRequest

    run = HeadlessRun(board, report_path, {"mode": mode, "backend": backend})
    if budget_s <= 0:
        return run.finish(EXIT_ERROR, "--budget/--timeout must be more than 0 seconds.")
    if grid_mm is not None and not 0.025 <= grid_mm <= 1.0:
        return run.finish(EXIT_ERROR, "--grid must be between 0.025 and 1.0 mm.")
    out = run.output_path(output, overwrite)
    if isinstance(out, str):
        return run.finish(EXIT_ERROR, out)
    loaded = run.load()
    if isinstance(loaded, str):
        return run.finish(EXIT_ERROR, loaded)
    load, wb = loaded
    copper = list(wb.engine.geometry.copper_layers)
    base = RouteRequest("", candidates=1)
    if layers:
        chosen = tuple(x.strip() for x in layers.split(",") if x.strip())
        unknown = [x for x in chosen if x not in copper]
        if unknown or not chosen:
            return run.finish(EXIT_ERROR, f"Unknown copper layer(s) {', '.join(unknown)}; "
                                          f"this board has {', '.join(copper)}.")  # fmt: skip
        base = replace(base, allowed_layers=chosen)
        run.report["routing_layers"] = list(chosen)
    if grid_mm is not None:
        base = replace(base, grid_resolution=mm_to_internal(grid_mm))
    n_workers = auto_workers(workers) if backend == "cpu" else 0  # GPU: one worker
    settings = adjust_board_settings(
        BoardRouterSettings(base_request=base, parallel_workers=n_workers), base, RouteMode(mode)
    )
    settings = replace(settings, budget_s=budget_s)
    if policy:
        from pcbrouter.learning.policy import policy_from_spec

        try:
            chosen_policy = policy_from_spec(policy)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return run.finish(EXIT_ERROR, f"Cannot load --policy {policy}: {exc}")
        trained = getattr(getattr(chosen_policy, "inner", chosen_policy), "trained_modes", None)
        if trained and mode not in trained:
            print(f"Warning: this policy was trained for {', '.join(trained)} mode, not {mode}.")
        settings = replace(settings, policy=chosen_policy)
        run.report["policy"] = getattr(chosen_policy, "name", "fixed")
        pid = getattr(chosen_policy, "policy_id", None)
        run.report["policy_id"] = pid() if callable(pid) else None
    run.report["workers"] = n_workers
    ctx = JobContext(0, send=lambda _msg: None)
    factory = ctx.router_factory(backend, top_level=False)
    if ctx.backend is not None:
        run.report["backend_selected"] = ctx.backend.text()
        print(f"Backend: {ctx.backend.text()}")
        if backend == "gpu" and "fallback" in ctx.backend.selected.lower():
            # explicit GPU: an error, never a silent CPU run (auto may fall back)
            why = f"GPU requested but not usable: {ctx.backend.reason}."
            return run.finish(EXIT_ERROR, why + " Run --gpu-check, or use --backend auto/cpu.")
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
    try:
        result = BoardRouter(wb, settings, router_factory=factory).run(progress=progress)
    except KeyboardInterrupt:
        return run.finish(EXIT_ERROR, "Interrupted; nothing was written.")
    run.report["route_s"] = round(time.monotonic() - t0, 1)
    if result.policy_decision is not None:
        run.report["policy_decision"] = result.policy_decision
        print(f"Policy: {result.policy_decision.get('text') or result.policy_decision}")
    if record_experience:
        from pcbrouter.learning.experience import record_board_job

        run.report["experience_records"] = record_board_job(result, settings, source="cli")
    run.report.update(_resource_usage())
    run.report["log"] = [line for line in result.log if "parallel" in line][:5]
    for line in run.report["log"]:
        print(f"  {line}")
    return run.export(load, wb, result, out, overwrite, kicad_drc, "route (CLI)")
