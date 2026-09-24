"""One open board's deterministic engine: geometry + rules + validator + analyses.

Built lazily from a :class:`~pcbrouter.project.manager.ProjectSession` and tied to
``board.fingerprint`` and the rule-set digest. Expensive results are cached here and
invalidated by building a new engine (any board or rule change creates one). Nothing
in this module writes to the board, the source files or the domain model.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from pcbrouter.domain.board import Board
from pcbrouter.domain.units import Nm
from pcbrouter.drc import DRCResult, run_geometry_check
from pcbrouter.geometry import GEOMETRY_ENGINE_VERSION
from pcbrouter.geometry.board import BoardGeometry
from pcbrouter.geometry.extract import build_board_geometry
from pcbrouter.kicad.rule_adapter import ProjectRuleData
from pcbrouter.routing.congestion import CongestionMap, build_congestion
from pcbrouter.routing.connectivity import BoardConnectivity, analyse_connectivity
from pcbrouter.routing.escape import PinEscape, analyse_pin_escape
from pcbrouter.routing.occupancy import OccupancyMap, build_occupancy
from pcbrouter.routing.validator import RouteValidator
from pcbrouter.rules.overrides import RuleOverrides
from pcbrouter.rules.resolver import RuleResolver
from pcbrouter.rules.ruleset import RULE_ENGINE_VERSION, RuleSet, build_ruleset

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EngineConfig:
    #: Unknown critical rules make validation refuse (RULE_UNKNOWN). Default ON.
    conservative: bool = True


class BoardEngine:
    """Thread-safe lazily built analyses for one board state."""

    def __init__(
        self,
        board: Board,
        project_rules: ProjectRuleData | None = None,
        overrides: RuleOverrides | None = None,
        config: EngineConfig | None = None,
    ) -> None:
        self.board = board
        self.project_rules = project_rules or ProjectRuleData()
        self.overrides = overrides or RuleOverrides()
        self.config = config or EngineConfig()
        self._lock = threading.RLock()
        self._occupancy: dict[tuple[Any, ...], OccupancyMap] = {}
        self._congestion: dict[tuple[Any, ...], CongestionMap] = {}
        self.last_drc: DRCResult | None = None

    # ------------------------------------------------------------ core pieces
    @cached_property
    def geometry(self) -> BoardGeometry:
        with self._lock:
            return build_board_geometry(self.board)

    @cached_property
    def ruleset(self) -> RuleSet:
        with self._lock:
            rs = build_ruleset(self.board, self.project_rules)
            log.info(
                "rules.loaded classes=%d custom=%d unsupported=%d (critical %d) digest=%s",
                len(rs.classes.classes), len(rs.custom_rules), len(rs.unsupported),
                len(rs.critical_unsupported), rs.digest,
            )  # fmt: skip
            for u in rs.unsupported:
                log.warning("rules.unsupported %s critical=%s", u.describe(), u.critical)
            return rs

    @cached_property
    def resolver(self) -> RuleResolver:
        return RuleResolver(self.ruleset, self.overrides, conservative=self.config.conservative)

    @cached_property
    def validator(self) -> RouteValidator:
        return RouteValidator(self.geometry, self.resolver)

    @cached_property
    def connectivity(self) -> BoardConnectivity:
        with self._lock:
            return analyse_connectivity(self.geometry)

    @property
    def cache_key(self) -> tuple[str, str, str]:
        return (self.board.fingerprint, self.ruleset.digest, repr(self.overrides.to_dict()))

    # ------------------------------------------------------------ analyses
    def run_drc(self) -> DRCResult:
        result = run_geometry_check(self.geometry, self.resolver, self.connectivity)
        self.last_drc = result
        return result

    def occupancy(self, layer: str, net: str | None, width: Nm, cell: Nm) -> OccupancyMap:
        key = (*self.cache_key, layer, net, width, cell)
        with self._lock:
            cached = self._occupancy.get(key)
            if cached is None:
                cached = build_occupancy(self.geometry, self.resolver, layer, net, width, cell)
                if len(self._occupancy) > 16:
                    self._occupancy.pop(next(iter(self._occupancy)))
                self._occupancy[key] = cached
            return cached

    def congestion(self, layer: str) -> CongestionMap:
        key = (*self.cache_key, layer)
        with self._lock:
            cached = self._congestion.get(key)
            if cached is None:
                cached = build_congestion(self.geometry, self.resolver, layer)
                self._congestion[key] = cached
            return cached

    def pin_escape(self, pad_uid: str) -> PinEscape:
        return analyse_pin_escape(
            self.geometry, self.validator.engine, self.geometry.copper[pad_uid]
        )

    def with_overrides(self, overrides: RuleOverrides) -> BoardEngine:
        """A new engine sharing this board's geometry (rules re-resolved)."""
        engine = BoardEngine(self.board, self.project_rules, overrides, self.config)
        if "geometry" in self.__dict__:
            engine.__dict__["geometry"] = self.geometry
            engine.__dict__["connectivity"] = self.connectivity
        if "ruleset" in self.__dict__:
            engine.__dict__["ruleset"] = self.ruleset
        return engine

    # ------------------------------------------------------------ diagnostics
    def diagnostics(self) -> dict[str, Any]:
        geo = self.geometry
        rs = self.ruleset
        drc = self.last_drc
        return {
            "board_fingerprint": self.board.fingerprint,
            "geometry_engine_version": GEOMETRY_ENGINE_VERSION,
            "rule_engine_version": RULE_ENGINE_VERSION,
            "rules_digest": rs.digest,
            "conservative_rule_handling": self.config.conservative,
            "object_counts": geo.counts(),
            "spatial_index": {
                "type": geo.hole_index.kind,
                "cell_size_um": geo.index_cell_size // 1000,
                "entries": geo.index_entry_count,
                "build_ms": round(geo.index_seconds * 1e3, 2),
            },
            "geometry_build_ms": round(geo.build_seconds * 1e3, 2),
            "board_region": geo.region.status.value,
            "unsupported_geometry": list(geo.notes)
            + list(geo.unsupported)
            + list(geo.unfilled_zones),
            "unsupported_rules": [u.describe() for u in rs.unsupported],
            "last_drc_status": drc.status.value if drc else "not run",
            "last_drc_duration_s": round(drc.elapsed_time, 3) if drc else None,
        }
