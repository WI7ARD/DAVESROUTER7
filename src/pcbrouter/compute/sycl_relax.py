"""Grid relaxation as one fused SYCL kernel per sweep (Intel GPUs via dpctl).

The same integer sweep as ``routing/search/relax.py`` (the NumPy reference), but
one kernel launch relaxes every (layer, cell) state — 8 moves plus vias — instead
of ~60 separate array operations with temporaries (the reason the old dpnp
wavefront was 17-236x slower than the CPU A* on an Iris Xe). Grid arrays are
uploaded once per search and stay on the device; the host reads two integers
every ``SWEEPS_PER_SYNC`` sweeps for the exact stop test. Results are
bit-identical to the reference (integer arithmetic, same sweep count).

The kernel is OpenCL C, built at run time with ``dpctl.program`` for the chosen
queue; a device whose runtime cannot build it is reported (``--gpu-check`` stage
``fused_kernel``) and routing then stays on the CPU.
"""

from __future__ import annotations

import ctypes
import threading
import time
from typing import Any

import numpy as np
import numpy.typing as npt

from pcbrouter.routing.search.astar import DIRS, SearchStatus
from pcbrouter.routing.search.relax import INF, MQ, SWEEPS_PER_SYNC, RelaxGrid

MAX_LAYERS = 32
_DX = ",".join(str(dx) for dx, _dy in DIRS)
_DY = ",".join(str(dy) for _dx, dy in DIRS)

KERNEL_SRC = f"""
#define INF {INF}
#define MQ {MQ}
#define MAXL {MAX_LAYERS}
__constant int DX[8] = {{{_DX}}};
__constant int DY[8] = {{{_DY}}};

__kernel void relax_sweep(
    __global const int *din, __global int *dout,
    __global const uchar *pass, __global const int *mult,
    __global const int *step, __global const uchar *via_ok,
    __global const uchar *target, __global int *stats,
    int L, int ny, int nx, int via_cost, int octi, int slot)
{{
    int gid = get_global_id(0);
    int n = ny * nx;
    if (gid >= n) return;
    int y = gid / nx, x = gid - (gid / nx) * nx;
    int newv[MAXL];
    for (int l = 0; l < L; ++l) {{
        int c = l * n + gid;
        if (!pass[c]) {{ newv[l] = INF; continue; }}
        int v = din[c];
        int m = mult[c];
        for (int d = 0; d < 8; ++d) {{
            if (!octi && (d & 1)) continue;
            int dx = DX[d], dy = DY[d];
            int py = y - dy, px = x - dx;
            if (py < 0 || py >= ny || px < 0 || px >= nx) continue;
            int pv = din[l * n + py * nx + px];
            if (pv >= INF) continue;
            if (dx != 0 && dy != 0) {{
                if (!pass[l * n + py * nx + x] || !pass[l * n + y * nx + px]) continue;
            }}
            int cand = pv + (step[l * 8 + d] * m + MQ / 2) / MQ;
            if (cand < v) v = cand;
        }}
        newv[l] = v;
    }}
    if (via_cost > 0 && via_ok[gid]) {{
        int best = INF;
        for (int l = 0; l < L; ++l) best = min(best, newv[l]);
        if (best < INF) {{
            int via = min(best + via_cost, INF);
            for (int l = 0; l < L; ++l)
                if (pass[l * n + gid] && via < newv[l]) newv[l] = via;
        }}
    }}
    for (int l = 0; l < L; ++l) {{
        int c = l * n + gid;
        int v = newv[l];
        dout[c] = v;
        if (v < din[c]) atomic_min(&stats[2 * slot], v);
        if (target[c] && v < INF) atomic_min(&stats[2 * slot + 1], v);
    }}
}}
"""

_PROGRAMS: dict[str, Any] = {}
_LOCK = threading.Lock()


def _usm(a: Any) -> Any:
    """USM memory of a dpnp array (what ``SyclQueue.submit`` takes)."""
    return a.get_array().usm_data


class SyclRelax:
    """Owns an in-order queue on one device and the built kernel."""

    def __init__(self, device: Any) -> None:
        import dpctl
        import dpctl.program as dprog

        self.device = device
        self.queue = dpctl.SyclQueue(device, property="in_order")
        key = device.filter_string
        with _LOCK:
            prog = _PROGRAMS.get(key)
            if prog is None:
                prog = dprog.create_program_from_source(self.queue, KERNEL_SRC)
                _PROGRAMS[key] = prog
        self.kernel = prog.get_sycl_kernel("relax_sweep")

    def solve(
        self,
        g: RelaxGrid,
        dist0: npt.NDArray[np.int32],
        targets: npt.NDArray[np.bool_],
        *,
        deadline: float,
        cancel: threading.Event | None = None,
        max_sweeps: int | None = None,
    ) -> tuple[SearchStatus, npt.NDArray[np.int32], int]:
        """Same contract and result as ``relax.solve`` (bit-identical)."""
        import dpnp

        nl, ny, nx = g.shape
        if nl > MAX_LAYERS:
            raise ValueError(f"{nl} layers: the kernel supports at most {MAX_LAYERS}")
        q = self.queue
        n = ny * nx

        def up(a: npt.NDArray[Any], dtype: Any) -> Any:
            return dpnp.asarray(np.ascontiguousarray(a, dtype=dtype).reshape(-1), sycl_queue=q)

        d_pass = up(g.passable, np.uint8)
        d_mult = up(g.mult, np.int32)
        d_step = up(g.step, np.int32)
        d_via = up(g.via_ok, np.uint8)
        d_tgt = up(targets, np.uint8)
        bufs = [up(dist0, np.int32), dpnp.empty(nl * n, dtype=dpnp.int32, sycl_queue=q)]
        stats = dpnp.empty(2 * SWEEPS_PER_SYNC, dtype=dpnp.int32, sycl_queue=q)
        octi = 1 if any(nd % 2 for nd in g.moves) else 0
        grange = [((n + 63) // 64) * 64]
        fixed = [
            ctypes.c_int(nl), ctypes.c_int(ny), ctypes.c_int(nx),
            ctypes.c_int(int(g.via_cost) if g.via_ok.any() else 0), ctypes.c_int(octi),
        ]  # fmt: skip
        limit = max_sweeps if max_sweeps is not None else nl * n + 1
        sweeps, cur = 0, 0
        while True:
            stats.fill(INF)
            for slot in range(SWEEPS_PER_SYNC):
                src, dst = bufs[cur], bufs[1 - cur]
                arrays = (src, dst, d_pass, d_mult, d_step, d_via, d_tgt, stats)
                args = [*(_usm(a) for a in arrays), *fixed, ctypes.c_int(slot)]
                q.submit(self.kernel, args, grange)
                cur = 1 - cur
                sweeps += 1
            last = dpnp.asnumpy(stats[-2:])  # waits for the in-order queue
            min_changed, best_t = int(last[0]), int(last[1])
            status = None
            if min_changed >= INF or (best_t < INF and min_changed >= best_t):
                status = SearchStatus.FOUND if best_t < INF else SearchStatus.NO_PATH
            elif sweeps >= limit:
                status = SearchStatus.NODE_LIMIT
            elif time.perf_counter() > deadline:
                status = SearchStatus.TIMEOUT
            elif cancel is not None and cancel.is_set():
                status = SearchStatus.CANCELLED
            if status is not None:
                dist = dpnp.asnumpy(bufs[cur]).astype(np.int32).reshape(nl, ny, nx)
                return status, dist, sweeps


# ------------------------------------------------------------------ self-test / bench
def _random_grid(seed: int, nl: int, ny: int, nx: int) -> tuple[RelaxGrid, Any, Any]:
    from pcbrouter.routing.search.relax import UNIT

    rng = np.random.default_rng(seed)
    passable = (rng.random((nl, ny, nx)) > 0.25).astype(np.uint8)
    mult = rng.integers(MQ, 3 * MQ, size=(nl, ny, nx)).astype(np.int32)
    step = np.array([[UNIT, 91, UNIT, 91, UNIT, 91, UNIT, 91]] * nl, dtype=np.int32)
    step[0, [2, 6]] *= 3  # wrong-way costs on layer 0
    via_ok = (rng.random((ny, nx)) > 0.5).astype(np.uint8)
    g = RelaxGrid(passable, mult, step, via_ok, 5 * UNIT, tuple(range(8)), 100_000.0)
    passable[0, 1, 1] = passable[nl - 1, ny - 2, nx - 2] = 1
    dist0 = np.full((nl, ny, nx), INF, dtype=np.int32)
    dist0[0, 1, 1] = 0
    targets = np.zeros((nl, ny, nx), dtype=bool)
    targets[nl - 1, ny - 2, nx - 2] = True
    return g, dist0, targets


def relax_selftest(queue: Any) -> tuple[bool, str]:
    """Build the kernel on ``queue``'s device and compare a full relaxation with
    the NumPy reference on a random obstacle grid (2 layers, vias, wrong-way)."""
    from pcbrouter.routing.search import relax

    t0 = time.perf_counter()
    eng = SyclRelax(queue.sycl_device)
    build_s = time.perf_counter() - t0
    g, dist0, targets = _random_grid(7, 2, 48, 64)
    far = time.perf_counter() + 60
    st_ref, ref, n_ref = relax.solve(np, g, dist0, targets, deadline=far)
    st_dev, dev, n_dev = eng.solve(g, dist0, targets, deadline=far)
    same = st_ref is st_dev and n_ref == n_dev and np.array_equal(ref, dev)
    return same, (
        f"kernel built in {build_s * 1000:.0f} ms; {n_dev} sweeps; "
        + ("distance field identical to NumPy" if same else
           f"MISMATCH: {st_ref.value}/{st_dev.value}, sweeps {n_ref}/{n_dev}, "
           f"{int((ref != dev).sum())} cells differ")
    )  # fmt: skip


def benchmark_relax(queue: Any, ny: int = 512, nx: int = 512, sweeps: int = 32) -> dict[str, Any]:
    """Per-sweep time of the fused kernel vs the NumPy reference (2 layers)."""
    from pcbrouter.routing.search import relax

    g, dist0, targets = _random_grid(11, 2, ny, nx)
    targets[:] = False  # no early stop: time exactly ``sweeps`` sweeps
    eng = SyclRelax(queue.sycl_device)
    eng.solve(g, dist0, targets, deadline=time.perf_counter() + 60, max_sweeps=16)  # warm-up
    t0 = time.perf_counter()
    eng.solve(g, dist0, targets, deadline=time.perf_counter() + 600, max_sweeps=sweeps)
    dev_ms = (time.perf_counter() - t0) * 1000 / sweeps
    dev_arrays = relax._device_arrays(np, g)
    d = dist0
    t0 = time.perf_counter()
    for _ in range(8):
        d = relax.sweep(np, g, d, dev_arrays)
    cpu_ms = (time.perf_counter() - t0) * 1000 / 8
    cells = 2 * ny * nx
    return {
        "cells": cells,
        "device_ms_per_sweep": round(dev_ms, 3),
        "numpy_ms_per_sweep": round(cpu_ms, 3),
        "speedup_vs_numpy": round(cpu_ms / dev_ms, 2) if dev_ms else None,
        "summary": f"{cells:,} cells: {dev_ms:.2f} ms/sweep on the device vs "
        f"{cpu_ms:.2f} ms/sweep NumPy ({cpu_ms / dev_ms:.1f}x)",
    }
