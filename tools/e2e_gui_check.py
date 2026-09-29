"""End-to-end acceptance check of the *real* application window.

Builds the main window exactly like ``pcbrouter`` does (``build_services`` +
``MainWindow`` + log panel), then drives it through the same actions a user
clicks, on the real Qt event loop (no ``processEvents`` loops, no mocks):

    open board → Route Board → cancel → Route Board again → wait → accept
    → Internal Geometry Check → export (new file) → reopen export → close

While it runs, a 10 ms heartbeat timer measures how long the GUI event loop is
ever blocked, and the routing overlay's phase/detail text is sampled so progress
changes can be verified. Everything is written as JSON (``--out``).

    python tools/e2e_gui_check.py BOARD.kicad_pcb --out result.json
    (Linux without a display: QT_QPA_PLATFORM=offscreen or xvfb-run)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("board", type=Path)
    ap.add_argument("--out", type=Path, default=None, help="JSON result file")
    ap.add_argument("--cancel-after", type=float, default=4.0, help="seconds before Cancel")
    ap.add_argument("--route-timeout", type=float, default=900.0)
    ap.add_argument("--budget", type=float, default=None, help="board routing budget (s)")
    ap.add_argument("--screenshots", type=Path, default=None, help="folder for PNGs")
    ap.add_argument("--no-preview", action="store_true",
                    help="diagnostic: disconnect the live routing preview")  # fmt: skip
    ap.add_argument("--mode", choices=["accuracy", "speed"], default=None,
                    help="routing mode (default: the saved setting, Accuracy)")  # fmt: skip
    args = ap.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="pcbrouter-e2e-"))
    for var, sub in (("PCBROUTER_CONFIG_DIR", "config"), ("PCBROUTER_DATA_DIR", "data"),
                     ("PCBROUTER_LOG_DIR", "logs")):  # fmt: skip
        os.environ.setdefault(var, str(tmp / sub))

    import logging

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from pcbrouter.app.application import build_services
    from pcbrouter.app_logging import add_handler, setup_logging
    from pcbrouter.settings.settings import ComputeBackendChoice, SettingsStore
    from pcbrouter.ui.log_panel import LogPanel
    from pcbrouter.ui.main_window import MainWindow
    from pcbrouter.utils.paths import log_dir

    log_file = setup_logging(log_dir(), logging.INFO, console=False)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    store = SettingsStore()
    settings = store.load()
    settings.default_compute_backend = ComputeBackendChoice.CPU  # the reference backend
    if args.mode:
        settings.routing.route_mode = args.mode
    store.save(settings)
    services = build_services(settings, detect_gpu_now=False)
    log_panel = LogPanel()
    add_handler(log_panel.handler)
    w = MainWindow(bus=services.bus, compute=services.compute, settings=settings,
                   settings_store=store, log_panel=log_panel)  # fmt: skip
    w.resize(1400, 900)
    w.show()
    if args.no_preview:
        w.route_jobs.partial.disconnect(w.routing_ui._on_board_partial)

    board = args.board.resolve()
    source_sha = sha(board)
    out_board = tmp / (board.stem + "_routed.kicad_pcb")
    report: dict[str, Any] = {"board": str(board), "log_file": str(log_file), "steps": []}
    shots = args.screenshots
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------- responsiveness probe
    gaps: dict[str, float] = {}
    phase_name = ["startup"]
    last_tick = [time.perf_counter()]

    big_gaps: list[tuple[str, float, int]] = []
    phase_t0 = [time.perf_counter()]

    def tick() -> None:
        now = time.perf_counter()
        gap = (now - last_tick[0]) * 1000
        last_tick[0] = now
        key = phase_name[0]
        gaps[key] = max(gaps.get(key, 0.0), gap)
        if gap > 100:
            big_gaps.append((key, round(now - phase_t0[0], 2), round(gap)))

    heartbeat = QTimer()
    heartbeat.setInterval(10)
    heartbeat.timeout.connect(tick)
    heartbeat.start()

    # stall tracer: when the GUI thread misses ticks for >150 ms, record where it is
    import threading
    import traceback as _tb

    main_id = threading.main_thread().ident
    stalls: dict[str, int] = {}
    stop_tracer = threading.Event()

    def tracer() -> None:
        while not stop_tracer.wait(0.05):
            if time.perf_counter() - last_tick[0] > 0.15:
                frames = sys._current_frames()
                frame = frames.get(main_id or 0)
                if frame is None:
                    continue
                stack = _tb.extract_stack(frame)
                own = [f for f in stack if "pcbrouter" in f.filename]
                if not own:  # GUI thread idle in Qt: who holds the GIL?
                    names = {t.ident: t.name for t in threading.enumerate()}
                    for tid, fr in frames.items():
                        if tid in (main_id, threading.get_ident()):
                            continue
                        st = [f for f in _tb.extract_stack(fr) if "pcbrouter" in f.filename]
                        if st:
                            own = st
                            key0 = f"[thread {names.get(tid, tid)}] "
                            break
                    else:
                        key0 = "[qt] "
                else:
                    key0 = ""
                key = f"{phase_name[0]}: {key0}" + " <- ".join(
                    f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in reversed(own[-7:])
                )
                stalls[key] = stalls.get(key, 0) + 1

    threading.Thread(target=tracer, daemon=True).start()

    samples: list[tuple[float, str, str, bool]] = []

    def sample(t0: float) -> None:
        ov = w.routing_overlay
        spin = getattr(ov, "spinner", None)
        samples.append((round(time.perf_counter() - t0, 2), ov.phase.text(), ov.detail.text(),
                        bool(spin is not None and spin.running)))  # fmt: skip

    def step(name: str, ok: bool, **info: Any) -> None:
        report["steps"].append({"step": name, "ok": bool(ok), **info})
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {json.dumps(info, default=str)[:300]}",
              flush=True)  # fmt: skip

    def shot(name: str) -> None:
        if shots:
            w.grab().save(str(shots / f"{name}.png"))

    # ---------------------------------------------------------- scripted steps
    def script() -> Iterator[tuple[Callable[[], bool], float]]:
        """Yields (condition, timeout_s); the driver resumes when it holds."""
        wb = lambda: services.project.working  # noqa: E731
        jobs = w.route_jobs
        yield (lambda: True), 1
        phase_name[0] = "load"
        t = time.perf_counter()
        ok = w.open_board(board)
        load_ms = (time.perf_counter() - t) * 1000
        session = services.project.session
        model = w.nets_panel.table.model()
        n_nets = model.rowCount() if model is not None else -1
        step("open board", ok and session is not None, load_blocking_ms=round(load_ms),
             nets_listed=n_nets, pads=len(session.board.pads) if session else 0,
             tracks=len(session.board.tracks) if session else 0)  # fmt: skip
        if not ok:
            return
        rendered = len(w.canvas.scene().items()) if w.canvas.scene() else 0
        step("board renders", rendered > 0, scene_items=rendered)
        shot("01_loaded")
        base_fp = wb().fingerprint
        n_tracks0 = len(wb().board.tracks)

        # --- route, then cancel
        phase_name[0] = "route_then_cancel"
        rs = w.routing_ui.board_settings()
        if args.budget:
            rs.budget_s = args.budget
        t0 = time.perf_counter()
        started = w.routing_ui.route_board(rs)
        step("route board started", started, overlay_visible=w.routing_overlay.isVisible())
        n0 = len(samples)
        end = time.perf_counter() + args.cancel_after
        while time.perf_counter() < end and jobs.busy:
            sample(t0)
            yield (lambda: True), 0.25
        shot("02_routing")
        texts = {(s[1], s[2]) for s in samples[n0:]}
        step("progress visible while routing", w.routing_overlay.isVisible() or not jobs.busy,
             distinct_progress_texts=len(texts), spinner_running=any(s[3] for s in samples[n0:]),
             last=samples[-1] if samples else None)  # fmt: skip
        if jobs.busy:
            w.routing_overlay.cancel_button.click()
            tc = time.perf_counter()
            yield (lambda: not jobs.busy), 30
            done = jobs.last_done
            step("cancel", not jobs.busy and done is not None and done.status == "CANCELED",
                 cancel_ms=round((time.perf_counter() - tc) * 1000),
                 status=done.status if done else None,
                 board_unchanged=wb().fingerprint == base_fp,
                 max_gap_ms=round(gaps.get("route_then_cancel", 0)))  # fmt: skip
        else:
            step("cancel", False, note="routing finished before cancel could be pressed")

        # --- route to completion
        phase_name[0] = "route_full"
        t0 = time.perf_counter()
        phase_t0[0] = t0
        started = w.routing_ui.route_board(rs)
        step("route board restarted", started)
        n0 = len(samples)
        deadline = time.perf_counter() + args.route_timeout
        while jobs.busy and time.perf_counter() < deadline:
            sample(t0)
            if len(samples) - n0 == 20:
                shot("03_routing_progress")
            yield (lambda: True), 0.5
        done = jobs.last_done
        result = w.routing_ui.last_board_result
        texts = [s for s in samples[n0:]]
        distinct = len({(s[1], s[2]) for s in texts})
        m = result.metrics if result is not None else None
        fails = []
        if result is not None:
            for net, o in result.outcomes.items():
                if o.status.value not in ("SUCCESS", "ALREADY_CONNECTED"):
                    fails.append(f"{net}: {o.status.value} {o.message}"[:220])
        step("route board completes", result is not None and not jobs.busy,
             status=done.status if done else None, elapsed_s=round(time.perf_counter() - t0, 1),
             summary=result.summary() if result else (done.error if done else None),
             nets_total=m.nets_attempted if m else None,
             nets_routed=m.nets_completed if m else None,
             nets_failed=m.nets_failed if m else None, vias=m.new_vias if m else None,
             backend=done.backend.text() if done and done.backend else None,
             distinct_progress_texts=distinct, failures=fails[:15],
             max_gap_ms=round(gaps.get("route_full", 0)))  # fmt: skip
        report["progress_samples"] = texts[:: max(1, len(texts) // 40)]
        if result is None:
            return
        shot("04_routed_preview")

        # --- accept (validated commit)
        phase_name[0] = "accept"
        ok = w.routing_ui.accept_board(None)
        added = len(wb().board.tracks) - n_tracks0
        step("accept result", ok, tracks_added=added, vias=len(wb().board.vias))
        yield (lambda: True), 0.5

        # --- internal geometry check
        phase_name[0] = "drc"
        w.engine_ui.run_geometry_check()
        yield (lambda: not w.engine_ui.jobs.is_running("drc")), 600
        drc = w.engine_ui.drc_panel.result
        text = w.engine_ui.lbl_drc.text()
        step("internal geometry check", "Running" not in text, label=text,
             errors=len(drc.errors) if drc is not None else None,
             max_gap_ms=round(gaps.get("drc", 0)))  # fmt: skip
        shot("05_drc")

        # --- export to a new file (worker: DRC gate, write, reload self-check)
        phase_name[0] = "export"
        started = w.export_ui._export(out_board, overwrite=False, allow_unverified=True)
        yield (lambda: not w.export_ui.export_running), 600
        rep = w.export_ui.last_export
        step("export", bool(rep is not None and rep.ok and out_board.exists()),
             summary=rep.summary() if rep else None, started=started,
             source_unchanged=sha(board) == source_sha)  # fmt: skip

        # --- reopen the exported board
        phase_name[0] = "reopen"
        exp_tracks = len(wb().board.tracks)
        exp_vias = len(wb().board.vias)
        ok = out_board.exists() and w.open_board(out_board)
        s2 = services.project.session
        step("reopen export", bool(ok and s2 is not None
                                   and len(s2.board.tracks) == exp_tracks
                                   and len(s2.board.vias) == exp_vias),
             tracks=len(s2.board.tracks) if s2 else None, expected_tracks=exp_tracks,
             vias=len(s2.board.vias) if s2 else None, expected_vias=exp_vias,
             load_warnings=len(s2.load_result.warnings) if s2 else None)  # fmt: skip
        shot("06_reopened")
        phase_name[0] = "close"

    it = script()

    def drive() -> None:
        try:
            cond, timeout = next(it)
        except StopIteration:
            finish()
            return
        except Exception:  # the check itself failed: record it, never hide it
            report["harness_error"] = traceback.format_exc()
            print(report["harness_error"], flush=True)
            finish()
            return
        deadline = time.perf_counter() + timeout

        def poll() -> None:
            if cond():
                QTimer.singleShot(0, drive)
            elif time.perf_counter() > deadline:
                report["steps"].append({"step": "wait", "ok": False, "note": "timed out"})
                QTimer.singleShot(0, drive)
            else:
                QTimer.singleShot(50, poll)

        QTimer.singleShot(int(min(timeout, 0.25) * 1000) if timeout else 0, poll)

    def finish() -> None:
        w.close()
        closed = not w.isVisible()
        step("close window", closed)
        report["max_event_loop_gap_ms"] = {k: round(v) for k, v in gaps.items()}
        stop_tracer.set()
        report["big_gaps"] = big_gaps
        report["stall_samples"] = dict(sorted(stalls.items(), key=lambda kv: -kv[1])[:15])
        report["ok"] = all(s["ok"] for s in report["steps"]) and "harness_error" not in report
        app.quit()

    QTimer.singleShot(200, drive)
    t_start = time.perf_counter()
    code = app.exec()
    report["exit_code"] = code
    report["total_s"] = round(time.perf_counter() - t_start, 1)
    services.compute.shutdown()
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(f"RESULT ok={report.get('ok')} gaps_ms={report.get('max_event_loop_gap_ms')}")
    print("  big gaps (phase, t since route start, ms):", report.get("big_gaps"))
    for k, v in report.get("stall_samples", {}).items():
        print(f"  stall x{v}: {k}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
