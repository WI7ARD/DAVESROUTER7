"""Deterministic board-fact tools for the planner (Stage 7).

The model may *ask* for facts (``fact_requests`` in its JSON); the application
answers with these functions — pure reads of the engine, the working board and
recorded router results. They take a tool name and an optional target, never
code, paths or expressions; unknown tools or targets return an error fact.
Outputs are small JSON-safe dicts labelled as deterministic facts.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pcbrouter.domain.units import internal_to_mm

if TYPE_CHECKING:
    from pcbrouter.board_engine import BoardEngine

FactFn = Callable[["BoardEngine", str | None, list[dict[str, Any]]], dict[str, Any]]
MAX_ITEMS = 40


def _mm(v: int | None) -> float | None:
    return None if v is None else round(internal_to_mm(v), 4)


def board_summary(engine: BoardEngine, _t: str | None, _f: list[dict[str, Any]]) -> dict[str, Any]:
    b = engine.board
    s = b.statistics
    m = engine.connectivity.metrics()
    box = b.bounds
    return {
        "copper_layers": list(engine.geometry.copper_layers),
        "size_mm": [_mm(box.width), _mm(box.height)] if box else None,
        "components": s.footprint_count,
        "pads": s.pad_count,
        "nets": s.net_count,
        "tracks": s.track_count,
        "vias": s.via_count,
        "connectivity": m,
    }


def net_info(engine: BoardEngine, target: str | None, _f: list[dict[str, Any]]) -> dict[str, Any]:
    if not target or target not in {n.name for n in engine.board.nets}:
        return {"error": f"unknown net {target!r}"}
    c = engine.connectivity.net(target)
    s = engine.resolver.summary(target)
    return {
        "net": target,
        "classes": list(s.net_classes),
        "status": c.status.value if c else "no pads",
        "pads": c.pad_count if c else 0,
        "groups": len(c.groups) if c else 0,
        "remaining_connections": c.remaining_connections if c else 0,
        "routed_length_mm": _mm(c.routed_length) if c else 0,
        "vias": c.via_count if c else 0,
    }


def component_info(
    engine: BoardEngine, target: str | None, _f: list[dict[str, Any]]
) -> dict[str, Any]:
    comp = next((c for c in engine.board.components if c.reference == target), None)
    if comp is None:
        return {"error": f"unknown component {target!r}"}
    nets = sorted({p.net_name for p in comp.footprint.pads if p.net_name})
    return {
        "reference": comp.reference,
        "value": comp.value,
        "side": comp.footprint.side.value,
        "pads": len(comp.footprint.pads),
        "nets": nets[:MAX_ITEMS],
    }


def rule_info(engine: BoardEngine, target: str | None, _f: list[dict[str, Any]]) -> dict[str, Any]:
    s = engine.resolver.summary(target)
    return {
        "net": target,
        "preferred_width_mm": _mm(s.width.preferred.value),
        "min_width_mm": _mm(s.width.minimum.value),
        "clearance_mm": _mm(s.clearance.value),
        "via_mm": [_mm(s.via.diameter.value), _mm(s.via.drill.value)],
        "allowed_layers": list(s.layers.allowed),
        "max_vias": s.max_vias.value,
        "sources": {
            "width": s.width.preferred.source.describe(),
            "clearance": s.clearance.source.describe(),
        },
        "unsupported_rules": [u.name for u in engine.ruleset.unsupported][:10],
    }


def connectivity(
    engine: BoardEngine, target: str | None, _f: list[dict[str, Any]]
) -> dict[str, Any]:
    conn = engine.connectivity
    if target:
        return net_info(engine, target, _f)
    open_nets = [n for n, c in sorted(conn.nets.items()) if c.remaining_connections]
    return {"metrics": conn.metrics(), "incomplete_nets": open_nets[:MAX_ITEMS]}


def congestion(engine: BoardEngine, _t: str | None, _f: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"label": "geometric congestion estimate"}
    for layer in engine.geometry.copper_layers:
        cmap = engine.congestion(layer)
        out[layer] = {"mean": round(cmap.mean, 3), "max": round(float(cmap.values.max()), 3)}
    return out


def route_failure(
    _e: BoardEngine, target: str | None, facts: list[dict[str, Any]]
) -> dict[str, Any]:
    hits = [f for f in facts if f.get("net") == target and f.get("status") != "SUCCESS"]
    return hits[-1] if hits else {"net": target, "router_result": "no failed route recorded"}


def route_metrics(
    _e: BoardEngine, target: str | None, facts: list[dict[str, Any]]
) -> dict[str, Any]:
    hits = [f for f in facts if (target is None or f.get("net") == target) and "best" in f]
    return (
        {"routes": hits[-MAX_ITEMS:]}
        if hits
        else {"router_result": "no routed candidates recorded"}
    )


TOOLS: dict[str, FactFn] = {
    "get_board_summary": board_summary,
    "get_net_info": net_info,
    "get_component_info": component_info,
    "get_rule_info": rule_info,
    "get_connectivity": connectivity,
    "get_congestion": congestion,
    "get_route_failure": route_failure,
    "get_route_metrics": route_metrics,
}


def answer(
    engine: BoardEngine, tool: str, target: str | None, router_facts: list[dict[str, Any]]
) -> dict[str, Any]:
    fn = TOOLS.get(tool)
    if fn is None:
        return {"tool": tool, "error": "unknown tool"}
    try:
        return {"tool": tool, "target": target, "fact": fn(engine, target, router_facts)}
    except Exception as exc:  # a fact tool failing must not break the session
        return {"tool": tool, "target": target, "error": f"unavailable: {exc}"}
