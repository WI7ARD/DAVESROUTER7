"""Messages between the GUI and the routing worker process.

Everything here is a plain picklable dataclass: no Qt objects, no locks, no open
files. The GUI sends one *job* (a ``*Job`` dataclass) at a time; the worker answers
with a stream of :class:`RouteProgress`, :class:`Heartbeat` and :class:`LogBatch`
messages and exactly one :class:`JobDone`.
"""

from __future__ import annotations

import copyreg
import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pcbrouter.routing.working_board import WorkingBoard


# ------------------------------------------------------------------ pickling
def _mappingproxy(data: dict[str, Any]) -> MappingProxyType[str, Any]:
    return MappingProxyType(data)


def _reduce_mappingproxy(m: MappingProxyType[str, Any]) -> tuple[Any, tuple[dict[str, Any]]]:
    return _mappingproxy, (dict(m),)


def install_pickling() -> None:
    """Read-only mappings (rules, proposal metadata) cross the process boundary as
    dicts and are re-wrapped on arrival. Idempotent; called on both sides."""
    copyreg.pickle(MappingProxyType, _reduce_mappingproxy)


install_pickling()


# ------------------------------------------------------------------ phases / status
class JobPhase(StrEnum):
    STARTING = "STARTING"
    PREPARING_BOARD = "PREPARING_BOARD"
    BUILDING_GRID = "BUILDING_GRID"
    PLANNING = "PLANNING"
    ROUTING = "ROUTING"
    RIPUP_REROUTE = "RIPUP_REROUTE"
    OPTIMIZING = "OPTIMIZING"
    VALIDATING = "VALIDATING"
    EXPORTING = "EXPORTING"
    RUNNING_DRC = "RUNNING_DRC"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


PHASE_TEXT = {
    JobPhase.STARTING: "Starting the routing worker…",
    JobPhase.PREPARING_BOARD: "Preparing the board…",
    JobPhase.BUILDING_GRID: "Building the routing grid…",
    JobPhase.PLANNING: "Planning…",
    JobPhase.ROUTING: "Routing…",
    JobPhase.RIPUP_REROUTE: "Rip-up and reroute…",
    JobPhase.OPTIMIZING: "Optimising routed nets…",
    JobPhase.VALIDATING: "Validating…",
    JobPhase.EXPORTING: "Writing the exported board…",
    JobPhase.RUNNING_DRC: "Running KiCad DRC…",
    JobPhase.COMPLETED: "Completed",
    JobPhase.FAILED: "Failed",
    JobPhase.CANCELED: "Canceled",
}


class JobStatus(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    TIMED_OUT = "TIMED_OUT"


@dataclass
class BackendInfo:
    """What the user asked for and what actually ran (never claim GPU on CPU)."""

    requested: str  # "CPU" | "GPU" | "AUTO"
    selected: str  # e.g. "CPU", "CPU fallback", "GPU (dpnp: Intel Iris Xe)"
    reason: str = ""
    searches_cpu: int = 0
    searches_gpu: int = 0
    gpu_fallbacks: int = 0

    def text(self) -> str:
        out = f"Requested: {self.requested} · Selected: {self.selected}"
        return out + (f" · {self.reason}" if self.reason else "")


@dataclass
class RouteProgress:
    """One (throttled) progress update. Unknown counts stay None: the GUI then shows
    an indeterminate indicator instead of an invented percentage."""

    job_id: int
    phase: str = JobPhase.STARTING.value
    message: str = ""
    elapsed_s: float = 0.0
    current_net: int | None = None  # 1-based index within the current pass
    current_net_name: str | None = None
    completed_nets: int | None = None
    total_nets: int | None = None
    current_pass: int | None = None
    total_passes: int | None = None
    candidates_completed: int | None = None
    candidates_total: int | None = None
    connection: int | None = None
    connections_total: int | None = None
    routes_succeeded: int | None = None
    routes_failed: int | None = None
    ripups: int | None = None
    grid: tuple[int, int, int] | None = None  # nx, ny, layers
    backend: BackendInfo | None = None
    #: raw board-router progress dict (Routing Jobs panel)
    board_info: dict[str, Any] | None = None

    def fraction(self) -> float | None:
        """Real progress in [0, 1] when it can be measured, else None."""
        if self.total_nets and self.completed_nets is not None:
            return max(0.0, min(1.0, self.completed_nets / self.total_nets))
        if self.candidates_total and self.candidates_completed is not None:
            return max(0.0, min(1.0, self.candidates_completed / self.candidates_total))
        return None


@dataclass
class Heartbeat:
    job_id: int | None
    worker_pid: int
    rss_bytes: int | None = None
    peak_rss_bytes: int | None = None


@dataclass
class LogBatch:
    job_id: int | None
    lines: list[str]
    suppressed: int = 0


@dataclass
class WorkerReady:
    pid: int
    python: str


@dataclass
class JobDone:
    job_id: int
    status: str  # JobStatus value
    value: Any = None
    error: str = ""
    traceback: str = ""
    elapsed_s: float = 0.0
    phase_timings: dict[str, float] = field(default_factory=dict)
    backend: BackendInfo | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ board snapshot
@dataclass
class WorkingSnapshot:
    """Everything the worker needs to rebuild an equivalent :class:`WorkingBoard`:
    immutable boards (source + current), rules, and the user's locks/constraints.
    The GUI's own WorkingBoard is never shared or mutated by the worker."""

    key: str
    source: Any  # Board
    board: Any  # Board
    project_rules: Any
    overrides: Any
    config: Any
    provenance: dict[str, Any]
    locks: set[str]
    locked_regions: list[Any]
    net_constraints: dict[str, dict[str, Any]]
    corridors: list[Any]

    @classmethod
    def from_working(cls, wb: WorkingBoard) -> WorkingSnapshot:
        state = (
            wb.source.fingerprint,
            wb.board.fingerprint,
            repr(wb.overrides),
            repr(wb.config),
            repr(sorted(wb.locks)),
            repr(wb.locked_regions),
            repr(sorted(wb.net_constraints.items())),
            repr(wb.corridors),
            repr(sorted((k, v.value) for k, v in wb.provenance.items())),
        )
        key = hashlib.sha256("\n".join(state).encode("utf-8")).hexdigest()
        return cls(
            key,
            wb.source,
            wb.board,
            wb.project_rules,
            wb.overrides,
            wb.config,
            dict(wb.provenance),
            set(wb.locks),
            list(wb.locked_regions),
            {k: dict(v) for k, v in wb.net_constraints.items()},
            list(wb.corridors),
        )

    def build(self) -> WorkingBoard:
        from pcbrouter.routing.working_board import WorkingBoard

        wb = WorkingBoard(self.source, self.project_rules, self.overrides, self.config)
        wb.board = self.board
        wb.provenance = dict(self.provenance)
        wb.locks = set(self.locks)
        wb.locked_regions = list(self.locked_regions)
        wb.net_constraints = {k: dict(v) for k, v in self.net_constraints.items()}
        wb.corridors = list(self.corridors)
        return wb


# ------------------------------------------------------------------ jobs
@dataclass
class JobBase:
    #: watchdog: the GUI cancels (then stops the worker) when exceeded; None = only
    #: the user's Cancel and the routing limits apply
    timeout_s: float | None = field(default=None, kw_only=True)
    mode: str = field(default="cpu", kw_only=True)  # "cpu" | "gpu" | "auto"
    kind: str = field(default="job", init=False)
    title: str = field(default="Working…", init=False)


@dataclass
class RouteNetJob(JobBase):
    snapshot: WorkingSnapshot
    request: Any  # RouteRequest
    record_explored: bool = False
    #: local reroute: generated copper removed (in the worker) before routing
    remove_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.kind, self.title = "route_net", f"Routing net {self.request.net}…"


@dataclass
class RouteBoardJob(JobBase):
    snapshot: WorkingSnapshot
    settings: Any  # BoardRouterSettings

    def __post_init__(self) -> None:
        self.kind, self.title = "route_board", "Routing board…"


@dataclass
class AIPlanJob(JobBase):
    snapshot: WorkingSnapshot
    plan: Any  # ExecutionPlan

    def __post_init__(self) -> None:
        self.kind, self.title = "ai_plan", "Running the AI routing plan…"


@dataclass
class OptimizeJob(JobBase):
    snapshot: WorkingSnapshot
    net: str
    goal: Any  # OptimizeGoal

    def __post_init__(self) -> None:
        self.kind, self.title = "optimize", f"Optimising {self.net}…"


@dataclass
class GpuCheckJob(JobBase):
    snapshot: WorkingSnapshot
    nets: list[str]
    base_request: Any

    def __post_init__(self) -> None:
        self.kind, self.title = "gpu_check", "Testing the GPU…"


@dataclass
class ExportJob(JobBase):
    snapshot: WorkingSnapshot
    source_path: Path
    source_sha256: str
    out_path: Path
    backup_dir: Path
    allow_unverified: bool = False
    run_kicad_drc: bool = False
    overwrite_source: bool = False

    def __post_init__(self) -> None:
        self.kind, self.title = "export", f"Exporting {self.out_path.name}…"


@dataclass
class FakeJob(JobBase):
    """Diagnostics and tests: a synthetic long job with real progress messages.
    ``outcome``: success | fail | crash | hang | flood."""

    duration_s: float = 3.0
    rate_hz: float = 50.0
    outcome: str = "success"
    flood: int = 0

    def __post_init__(self) -> None:
        self.kind, self.title = "fake", "Synthetic job…"


@dataclass
class OptimizationPlan:
    """Result of an :class:`OptimizeJob`: copper to add/remove relative to the board
    it was computed on. The GUI applies it as one validated, undoable commit."""

    base_fingerprint: str
    net: str
    goal: Any
    add_tracks: list[Any]
    add_vias: list[Any]
    remove_ids: list[str]
    report: Any
    improved: bool
