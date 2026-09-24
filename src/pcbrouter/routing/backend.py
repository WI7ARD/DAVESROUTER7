"""Choose the search backend for a router (CPU now; GPU is added in Stage 6)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pcbrouter.board_engine import BoardEngine
from pcbrouter.routing.router import Router

if TYPE_CHECKING:
    from pcbrouter.compute.manager import ComputeManager


def router_for(engine: BoardEngine, compute: ComputeManager | None = None) -> Router:
    """A router using the CPU A* search (the authoritative reference backend)."""
    return Router(engine, backend_name="cpu")
