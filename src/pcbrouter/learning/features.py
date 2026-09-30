"""Board profile: a small, explainable feature vector describing a routing job.

Used by the policy selector (is this board like the ones a policy was trained
on?) and by the experience log. Every feature is cheap, deterministic and
computed from the loaded board plus the routing plan; none of them identifies
the board or its nets.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np

#: bump when a feature's definition changes (profiles are compared numerically)
PROFILE_VERSION = 1

#: features used for distance / out-of-distribution checks, with the transform
#: that makes them comparable across boards ("log": sizes spanning decades)
DISTANCE_FEATURES: dict[str, str] = {
    "copper_layers": "lin",
    "signal_layers": "lin",
    "area_cm2": "log",
    "nets_to_route": "log",
    "pads_per_net": "lin",
    "pad_density_cm2": "log",
    "pitch_p10_mm": "log",
    "airwire_mean_mm": "log",
    "long_net_frac": "lin",
    "demand": "log",
    "low_escape_frac": "lin",
    "congestion_mean": "lin",
    "multi_pad_frac": "lin",
}


def _polygon_area(points: Sequence[Any]) -> float:
    if len(points) < 3:
        return 0.0
    xs = np.array([float(p.x) for p in points])
    ys = np.array([float(p.y) for p in points])
    return abs(float(np.dot(xs, np.roll(ys, -1)) - np.dot(ys, np.roll(xs, -1)))) / 2.0


def _pitch_percentile(board: Any, q: float) -> float:
    """q-th percentile of the nearest-neighbour distance between pad centres (mm):
    a robust "how fine is the pitch" measure (p10 = the fine-pitch parts)."""
    pts = np.array(
        [(float(p.position.x), float(p.position.y)) for p in board.pads], dtype=np.float64
    )
    if len(pts) < 2:
        return 0.0
    nn = np.empty(len(pts))
    for i in range(0, len(pts), 512):  # chunked: bounded memory on large boards
        d = np.hypot(pts[i : i + 512, None, 0] - pts[None, :, 0],
                     pts[i : i + 512, None, 1] - pts[None, :, 1])  # fmt: skip
        d[np.arange(d.shape[0]), np.arange(i, i + d.shape[0])] = np.inf
        nn[i : i + 512] = d.min(axis=1)
    nn = nn[np.isfinite(nn) & (nn > 0)]
    return float(np.percentile(nn, q)) / 1e6 if len(nn) else 0.0


def board_profile(board: Any, tasks: Sequence[Any]) -> dict[str, float]:
    """Numbers describing *board* and the nets still to route (*tasks*)."""
    s = board.statistics
    w_mm = (s.width or 0) / 1e6
    h_mm = (s.height or 0) / 1e6
    area_cm2 = max(w_mm * h_mm / 100.0, 1e-3)
    diag_mm = math.hypot(w_mm, h_mm) or 1.0
    layers = max(1, s.copper_layer_count)

    # copper layers mostly covered by zones behave like planes, not routing layers
    covered: dict[str, float] = {}
    for z in getattr(board, "zones", ()):
        if z.is_keepout:
            continue
        a = _polygon_area(z.outline) / 1e12  # nm^2 -> mm^2
        for layer in z.copper_layers:
            covered[layer] = covered.get(layer, 0.0) + a
    board_mm2 = max(w_mm * h_mm, 1e-6)
    planes = sum(1 for a in covered.values() if a / board_mm2 > 0.6)

    n = len(tasks)
    pads = [t.pads for t in tasks]
    lengths = [t.airwire_length / 1e6 for t in tasks]
    escapes = [t.escape_options for t in tasks]
    cong = [t.congestion for t in tasks]
    total_len = float(sum(lengths))
    return {
        "copper_layers": float(layers),
        "signal_layers": float(max(1, layers - planes)),
        "plane_layers": float(planes),
        "area_cm2": area_cm2,
        "pads": float(s.pad_count),
        "nets_to_route": float(n),
        "pads_per_net": float(np.mean(pads)) if n else 0.0,
        "multi_pad_frac": float(np.mean([p >= 4 for p in pads])) if n else 0.0,
        "pad_density_cm2": s.pad_count / area_cm2,
        "pitch_p10_mm": _pitch_percentile(board, 10),
        "pitch_p50_mm": _pitch_percentile(board, 50),
        "airwire_total_mm": total_len,
        "airwire_mean_mm": total_len / n if n else 0.0,
        "long_net_frac": float(np.mean([x > 0.25 * diag_mm for x in lengths])) if n else 0.0,
        # routing demand: airwire length per unit of routable area and signal layer
        "demand": total_len / (area_cm2 * 100.0 * max(1, layers - planes)),
        "low_escape_frac": float(np.mean([e <= 2 for e in escapes])) if n else 0.0,
        "congestion_mean": float(np.mean(cong)) if n else 0.0,
        "existing_tracks": float(s.track_count),
        "existing_vias": float(s.via_count),
        "zones": float(s.zone_count),
        "diff_pair_frac": (
            float(np.mean([getattr(t.kind, "value", "") == "diff_pair" for t in tasks]))
            if n
            else 0.0
        ),
    }


def transform(profile: dict[str, float]) -> np.ndarray:
    """The distance-feature vector (log-scaled where sizes span decades)."""
    out = []
    for name, how in DISTANCE_FEATURES.items():
        v = float(profile.get(name, 0.0))
        out.append(math.log10(max(v, 1e-3)) if how == "log" else v)
    return np.array(out, dtype=np.float64)
