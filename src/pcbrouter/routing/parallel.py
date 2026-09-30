"""Parallel board routing: speculative, spatially independent batches (CPU).

The board router stays the single owner of the routing fork. Helpers only
*propose*:

1. ``N`` helper processes (spawn context) each rebuild a replica of the fork from
   a :class:`~pcbrouter.jobs.protocol.WorkingSnapshot` once.
2. Before every batch the master sends each replica the copper committed since
   its last sync (applied without re-validation: the master validated it).
3. Work queue: whenever a helper is free it gets the next net (plan order, a
   bounded look-ahead) whose pad region, grown by a margin, does not overlap a
   net still in flight; it is synced to the master first. Long nets therefore
   never hold the other helpers back.
4. Results are committed as they arrive through the exact validator
   (``WorkingBoard.commit_proposals``). A result that is no longer legal (copper
   committed meanwhile) is re-queued once, then routed on the master. Illegal
   copper is never accepted, whatever the helpers return.

Commit order follows completion, so parallel runs are not bit-for-bit repeatable
(every result is still validated); "Single worker" is fully deterministic.

GPU mode is not parallelised (no device contexts in helpers). If helpers cannot
start, routing continues sequentially and says so.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import queue
import time
import traceback
from dataclasses import dataclass
from typing import Any

from pcbrouter.domain.geometry import BoundingBox
from pcbrouter.routing.request import RouteRequest
from pcbrouter.routing.working_board import WorkingBoard

log = logging.getLogger(__name__)

#: below this many tasks the helper start-up cost outweighs the gain
PARALLEL_MIN_TASKS = 12
#: nets closer than this (pad regions) are never routed in the same batch
REGION_MARGIN_NM = 2_000_000
#: how far ahead in plan order a batch may look for independent nets
LOOKAHEAD = 4
MAX_WORKERS = 4
START_TIMEOUT_S = 120.0
#: Auto/N workers start helpers only if the plan packs into batches this wide
#: on average (see estimate_speedup)
PARALLEL_MIN_SPEEDUP = 1.5


def auto_workers(requested: int) -> int:
    """``requested`` < 0 → automatic: CPU cores - 1, at most MAX_WORKERS."""
    cores = os.cpu_count() or 1
    if requested < 0:
        return max(0, min(MAX_WORKERS, cores - 1))
    return min(requested, max(1, cores))


def _helper_main(snapshot: Any, inbox: Any, outbox: Any, cancel: Any) -> None:
    """Helper process: replica of the fork; routes one request at a time."""
    from pcbrouter.routing.board_router import _congestion_provider, route_net_refined
    from pcbrouter.routing.router import Router

    try:
        wb = snapshot.build()
        _ = wb.engine.geometry  # build now, not on the first request
        cache: dict[Any, Any] = {}
        outbox.put(("ready", os.getpid(), None))
    except Exception:  # reported to the master, which then routes sequentially
        outbox.put(("dead", os.getpid(), traceback.format_exc()))
        return
    while True:
        msg = inbox.get()
        kind = msg[0]
        if kind == "stop":
            return
        try:
            if kind == "sync":
                tracks, vias, removed = msg[1]
                if tracks or vias or removed:
                    wb.commit_objects(tracks, vias, removed, "parallel sync", validate=False)
                continue
            if kind == "route":
                _kind, idx, request, congestion = msg
                router = Router(wb.engine, grid_cache=cache)
                if len(cache) > 4:
                    cache.pop(next(iter(cache)))
                penalties = _congestion_provider(wb) if congestion else None
                res = route_net_refined(router, request, cancel, penalties)
                outbox.put(("result", idx, res))
        except Exception:
            outbox.put(("error", msg[1] if kind == "route" else -1, traceback.format_exc()))


@dataclass
class _Helper:
    process: Any
    inbox: Any
    synced: set[str]


class ParallelRouter:
    """Helper pool bound to one routing fork (see module docstring)."""

    def __init__(self, fork: WorkingBoard, workers: int) -> None:
        from pcbrouter.jobs.protocol import WorkingSnapshot

        self.fork = fork
        self.ctx = mp.get_context("spawn")
        self.cancel = self.ctx.Event()
        self.outbox = self.ctx.Queue()
        snapshot = WorkingSnapshot.from_working(fork)
        ids = self._ids()
        self.helpers: list[_Helper] = []
        for _ in range(workers):
            inbox = self.ctx.Queue()
            proc = self.ctx.Process(
                target=_helper_main,
                args=(snapshot, inbox, self.outbox, self.cancel),
                name="pcbrouter-route-helper",
                daemon=True,
            )
            proc.start()
            self.helpers.append(_Helper(proc, inbox, set(ids)))
        ready = 0
        deadline = time.monotonic() + START_TIMEOUT_S
        while ready < workers:
            try:
                kind, _pid, detail = self.outbox.get(timeout=1.0)
            except queue.Empty:
                if time.monotonic() > deadline or any(
                    h.process.exitcode is not None for h in self.helpers
                ):
                    self.close()
                    raise RuntimeError("route helpers did not start") from None
                continue
            if kind == "dead":
                self.close()
                raise RuntimeError(f"a route helper failed to start:\n{detail}")
            ready += 1
        log.info("parallel.start workers=%d", workers)

    @property
    def workers(self) -> int:
        return len(self.helpers)

    def _ids(self) -> set[str]:
        board = self.fork.board
        return {t.id for t in board.tracks} | {v.id for v in board.vias}

    def sync(self) -> None:
        """Bring every replica to the master fork's current copper."""
        for i in range(len(self.helpers)):
            self.sync_helper(i)

    def sync_helper(self, index: int) -> None:
        """Send helper ``index`` the copper committed since its last sync."""
        board = self.fork.board
        current = self._ids()
        h = self.helpers[index]
        added_t = [t for t in board.tracks if t.id not in h.synced]
        added_v = [v for v in board.vias if v.id not in h.synced]
        removed = sorted(h.synced - current)
        h.inbox.put(("sync", (added_t, added_v, removed)))
        h.synced = current

    def dispatch(self, index: int, request: RouteRequest, congestion: bool) -> None:
        """Sync helper ``index`` to the master fork, then give it one net."""
        self.sync_helper(index)
        self.helpers[index].inbox.put(("route", index, request, congestion))

    def poll(self, busy: set[int], timeout: float = 0.2) -> tuple[int, str, Any] | None:
        """Next finished request: (helper index, "result" | "error", payload), or None
        after ``timeout``. A helper that died while busy is reported as an error."""
        try:
            kind, idx, payload = self.outbox.get(timeout=timeout)
        except queue.Empty:
            for i in sorted(busy):
                if self.helpers[i].process.exitcode is not None:
                    return i, "error", "the route helper process stopped unexpectedly"
            return None
        return int(idx), str(kind), payload

    def route(
        self, requests: list[RouteRequest], congestion: bool, stop: Any
    ) -> list[tuple[str, Any]]:
        """Route up to ``workers`` requests concurrently (one per helper) and return
        one ("result", RouteResult) or ("error", text) per request, in order."""
        busy = set()
        for i, req in enumerate(requests):
            self.dispatch(i, req, congestion)
            busy.add(i)
        out: dict[int, tuple[str, Any]] = {}
        while busy:
            if stop():
                self.cancel.set()
            got = self.poll(busy)
            if got is None:
                continue
            i, kind, payload = got
            if i in busy:
                busy.discard(i)
                out[i] = (kind, payload)
        self.cancel.clear()
        return [out[i] for i in range(len(requests))]

    def close(self) -> None:
        for h in self.helpers:
            try:
                h.inbox.put(("stop",))
            except Exception:  # queue already broken: terminate below
                log.debug("parallel.stop_failed", exc_info=True)
        for h in self.helpers:
            h.process.join(timeout=5)
            if h.process.exitcode is None:
                h.process.terminate()
                h.process.join(timeout=5)
        self.helpers = []


def pick_batch(queue_: list[Any], regions: dict[str, BoundingBox | None], size: int) -> list[Any]:
    """The next nets in plan order whose regions do not overlap (up to ``size``),
    looking at most ``size * LOOKAHEAD`` tasks ahead. Nets without a known region
    are routed alone."""
    batch: list[Any] = []
    taken: list[BoundingBox] = []
    for task in queue_[: size * LOOKAHEAD]:
        box = regions.get(task.net)
        if box is None:
            if not batch:
                return [task]
            continue
        grown = box.expanded(REGION_MARGIN_NM)
        if any(grown.intersects(b) for b in taken):
            continue
        batch.append(task)
        taken.append(grown)
        if len(batch) >= size:
            break
    return batch or queue_[:1]


def estimate_speedup(tasks: list[Any], regions: dict[str, BoundingBox | None], size: int) -> float:
    """How many nets could run at once on average: tasks / rounds when the plan is
    packed greedily into non-overlapping batches (equal net times assumed). Near
    1.0 means the nets crowd one area (e.g. all fan out of one QFP) and helpers
    would mostly wait — measured slower, and a different commit order, there."""
    queue_ = list(tasks)
    rounds = 0
    while queue_:
        batch = pick_batch(queue_, regions, size)
        ids = {id(t) for t in batch}
        queue_ = [t for t in queue_ if id(t) not in ids]
        rounds += 1
    return len(tasks) / rounds if rounds else 0.0
