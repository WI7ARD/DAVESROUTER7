"""The GUI stays responsive while routing runs in the worker process.

Each test runs a real worker process. FakeJob is a CPU-bound Python loop, like the
router, so a blocked or GIL-starved Qt thread would show up as missing or late timer
ticks."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QTimer
from pytestqt.qtbot import QtBot

from pcbrouter.jobs.protocol import FakeJob, JobDone, JobStatus, RouteProgress
from pcbrouter.settings import SettingsStore
from pcbrouter.ui.main_window import MainWindow
from pcbrouter.ui.route_jobs import JobState
from tests.integration.test_routing_ui import wait
from tests.integration.test_ui import make_window

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot: QtBot, tmp_path: Path) -> Iterator[MainWindow]:
    w = make_window(qtbot, SettingsStore(tmp_path / "config" / "settings.json"))
    yield w
    w.close()


class Ticker:
    """A 20 ms Qt timer: records when the event loop actually served it."""

    def __init__(self) -> None:
        self.ticks: list[float] = []
        self.timer = QTimer()
        self.timer.timeout.connect(lambda: self.ticks.append(time.monotonic()))
        self.timer.start(20)

    def max_gap(self) -> float:
        return max((b - a for a, b in zip(self.ticks, self.ticks[1:], strict=False)), default=0)


def run_until(pred: Callable[[], bool], timeout_s: float = 30.0) -> None:
    end = time.monotonic() + timeout_s
    while not pred() and time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)
    assert pred(), "condition not reached in time"


def submit(w: MainWindow, job: FakeJob) -> list[JobDone]:
    out: list[JobDone] = []
    assert w.route_jobs.submit(job, out.append) is not None
    return out


def test_long_job_keeps_the_event_loop_responsive(window: MainWindow) -> None:
    rj, ov = window.route_jobs, window.routing_overlay
    progress: list[RouteProgress] = []
    rj.progress.connect(progress.append)
    states: list[str] = []
    rj.stateChanged.connect(states.append)
    ticker = Ticker()
    done = submit(window, FakeJob(duration_s=3.0))
    assert ov.isVisible() and ov.spinner.running  # overlay immediately visible
    run_until(lambda: rj.state is JobState.RUNNING)
    frames0 = ov.spinner.frames
    run_until(lambda: bool(done), 30)
    assert done[0].status == JobStatus.COMPLETED.value
    assert ov.spinner.frames - frames0 > 30  # the animation really advanced
    assert len(ticker.ticks) > 80  # ~3 s of 20 ms ticks were served
    assert ticker.max_gap() < 0.25, f"GUI stalled for {ticker.max_gap():.3f} s"
    assert len(progress) >= 5 and progress[-1].fraction() is not None
    assert states[:2] == ["STARTING", "RUNNING"] and states[-1] == "IDLE"
    assert not ov.isVisible()


def test_cancel_during_job_restores_controls(window: MainWindow) -> None:
    rj, ov = window.route_jobs, window.routing_overlay
    done = submit(window, FakeJob(duration_s=20.0))
    run_until(lambda: rj.state is JobState.RUNNING)
    ov.cancel_button.click()
    assert rj.state is JobState.CANCELING and not ov.cancel_button.isEnabled()
    assert "Stopping" in ov.phase.text()
    assert not rj.cancel()  # a duplicate request is ignored
    run_until(lambda: bool(done), 10)
    assert done[0].status == JobStatus.CANCELED.value
    assert rj.state is JobState.IDLE and not ov.isVisible()


def test_worker_exception_and_crash_are_reported(window: MainWindow) -> None:
    done = submit(window, FakeJob(duration_s=0.5, outcome="fail"))
    run_until(lambda: bool(done))
    assert done[0].status == JobStatus.FAILED.value
    assert "synthetic routing failure" in done[0].error
    assert "Traceback" in done[0].traceback and "_fake" in done[0].traceback
    done = submit(window, FakeJob(duration_s=0.5, outcome="crash"))
    run_until(lambda: bool(done), 30)
    assert done[0].status == JobStatus.FAILED.value
    assert "exit code 3" in done[0].error
    # a fresh worker takes the next job
    done = submit(window, FakeJob(duration_s=0.3))
    run_until(lambda: bool(done), 30)
    assert done[0].status == JobStatus.COMPLETED.value


def test_timeout_and_unresponsive_worker_are_stopped(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pcbrouter.ui.route_jobs as rjmod

    done = submit(window, FakeJob(duration_s=30.0, timeout_s=1.0))
    run_until(lambda: bool(done), 15)
    assert done[0].status == JobStatus.TIMED_OUT.value
    monkeypatch.setattr(rjmod, "CANCEL_GRACE_S", 1.0)
    done = submit(window, FakeJob(duration_s=0.2, outcome="hang"))
    run_until(lambda: window.route_jobs.state is JobState.RUNNING, 15)
    time.sleep(0.4)
    window.route_jobs.cancel()
    run_until(lambda: bool(done), 15)  # ignored the cancel → worker terminated
    assert done[0].status == JobStatus.CANCELED.value and "terminated" in done[0].error


def test_progress_flood_is_throttled(window: MainWindow) -> None:
    received: list[RouteProgress] = []
    window.route_jobs.progress.connect(received.append)
    ticker = Ticker()
    done = submit(window, FakeJob(outcome="flood", flood=200_000))
    run_until(lambda: bool(done), 60)
    value = done[0].value
    assert value["events"] >= 200_000
    assert value["sent"] < 200  # ≤ ~10 messages per second of work
    assert len(received) <= value["sent"] + 1
    assert ticker.max_gap() < 0.25


def test_single_job_at_a_time_and_double_click(
    window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    assert window.open_board(fixture_path("router_basic.kicad_pcb"))
    wait(window)
    ui = window.routing_ui
    assert ui.route_board() is True
    assert ui.route_board() is False  # second click refused while the first runs
    assert not ui.act_route_board.isEnabled() and not ui.act_route_net.isEnabled()
    assert not window.export_ui.act_export.isEnabled()
    wait(window)
    assert ui.act_route_board.isEnabled() and window.export_ui.act_export.isEnabled()
    assert ui.last_board_result is not None


def test_route_failure_is_reported(window: MainWindow, fixture_path: Callable[[str], Path]) -> None:
    from pcbrouter.routing.request import RouteRequest

    assert window.open_board(fixture_path("router_basic.kicad_pcb"))
    wait(window)
    ui = window.routing_ui
    assert ui.route_net(RouteRequest("A", allowed_layers=("F.Cu",), max_vias=0, candidates=1))
    wait(window)
    result = ui.panel.result or ui.last_result
    assert result is not None and window.route_jobs.last_done.status == "COMPLETED"
    assert ui.act_route_net.isEnabled()


def test_close_window_while_routing(qtbot: QtBot, tmp_path: Path) -> None:
    w = make_window(qtbot, SettingsStore(tmp_path / "config" / "settings.json"))
    done = submit(w, FakeJob(duration_s=30.0))
    run_until(lambda: w.route_jobs.state is JobState.RUNNING, 15)
    t0 = time.monotonic()
    w.close()
    assert time.monotonic() - t0 < 3.0  # closing never waits for the router
    assert not done and not w.route_jobs.busy


def test_export_failure_after_successful_routing(
    window: MainWindow, tmp_path: Path, fixture_path: Callable[[str], Path]
) -> None:
    src = fixture_path("router_basic.kicad_pcb")
    assert window.open_board(src)
    wait(window)
    ui = window.routing_ui
    assert ui.route_board()
    wait(window)
    assert ui.accept_board(None)
    before = src.read_bytes()
    window.export_ui.inform = lambda *_: None
    bad = tmp_path / "no_such_dir" / "x.kicad_pcb"
    assert window.export_ui.export_routed(bad)
    wait(window)
    report = window.export_ui.last_export
    assert report is not None and not report.ok  # reported, not crashed
    assert not bad.exists() and src.read_bytes() == before
    assert window.bus.context.project.working.modified  # routed work kept
    assert ui.act_route_board.isEnabled()


def test_gpu_setup_dialog_opens_and_board_load_keeps_gpu_runtimes_out(
    window: MainWindow, fixture_path: Callable[[str], Path]
) -> None:
    import sys

    assert window.open_board(fixture_path("router_basic.kicad_pcb"))
    wait(window)
    window.open_gpu_setup()
    dlg = window._gpu_setup_dialog
    assert dlg.isVisible() and dlg.summary.text()
    dlg.reject()
    # the GUI process never loads a GPU runtime (they live in the routing worker)
    assert not {"cupy", "dpnp", "dpctl"} & set(sys.modules)


def test_every_gpu_setup_button_responds(
    window: MainWindow, fixture_path: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    import pcbrouter.ui.gpu_setup_dialog as mod
    from pcbrouter.settings.settings import ComputeBackendChoice

    window.open_gpu_setup()
    dlg = window._gpu_setup_dialog
    run_until(lambda: not dlg._jobs.is_running(), 20)  # background re-detection
    text = lambda: dlg.output.toPlainText()  # noqa: E731
    # every button is clickable and answers, even with no GPU and no board
    for b in (dlg.install_button, dlg.use_button, dlg.test_button):
        assert b.isEnabled()
    dlg.test_button.click()
    assert "Open a board first" in text()
    dlg.install_button.click()
    assert "No GPU found" in text()
    # install for a hand-picked vendor (pip replaced by a harmless command)
    monkeypatch.setattr(
        mod, "pip_command", lambda pkg: [sys.executable, "-c", f"print('pip {pkg} ok')"]
    )
    dlg.vendor.setCurrentText("Intel")
    dlg.install_button.click()
    run_until(lambda: dlg.process is None and "pip dpnp ok" in text(), 30)
    assert dlg.install_button.isEnabled()
    dlg.use_button.click()
    assert window.settings.default_compute_backend is ComputeBackendChoice.GPU
    assert "Routing backend set to GPU" in text()
    # test on a board: runs in the worker and reports back into the dialog
    assert window.open_board(fixture_path("router_basic.kicad_pcb"))
    wait(window)
    dlg.test_button.click()
    assert "GPU test running" in text()
    wait(window)
    run_until(lambda: "GPU check" in text() or "GPU works" in text(), 30)
    dlg.reject()


def test_guides_cover_features_and_buttons_work(window: MainWindow) -> None:
    from pcbrouter.ui.guides import GUIDES

    assert window.act_guides.shortcut().toString() == "F1"
    window.open_guides("gpu")
    dlg = window._guide_dialog
    assert dlg.isVisible() and "Set Up GPU" in dlg.text.toPlainText()
    keys = {g.key for g in GUIDES}
    assert {
        "start",
        "route_net",
        "route_board",
        "export",
        "gpu",
        "ollama",
        "ai",
        "checks",
        "workbench",
        "trouble",
        "keys",
    } <= keys
    for row, g in enumerate(GUIDES):
        dlg.topics.setCurrentRow(row)
        assert dlg.text.toPlainText().strip()
        for _label, method in g.actions:
            assert callable(getattr(window, method, None)), method
    window._guide_route_board()  # no board: answers instead of failing
    assert "Open a board first" in window.statusBar().currentMessage()
    dlg.close()
