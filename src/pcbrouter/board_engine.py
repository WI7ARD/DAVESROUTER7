"""One open board's deterministic engine: geometry + rules + validator + analyses.

Built lazily from a :class:`~pcbrouter.project.manager.ProjectSession` and tied to
``board.fingerprint`` and the rule-set digest. Expensive results are cached here and
invalidated by building a new engine (any board or rule change creates one). Nothing
in this module writes to the board, the source files or the domain model.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from pcbrouter.domain.board import Board
from pcbrouter.domain.geometry import BoundingBox
from pcbrouter.domain.units import Nm
from pcbrouter.drc import DRCResult, run_geometry_check
from pcbrouter.geometry import GEOMETRY_ENGINE_VERSION
from pcbrouter.geometry.board import BoardGeometry, CopperItem
from pcbrouter.geometry.extract import build_board_geometry
from pcbrouter.kicad.rule_adapter import ProjectRuleData
from pcbrouter.routing.congestion import CongestionMap, build_congestion
from pcbrouter.routing.connectivity import BoardConnectivity, analyse_connectivity
from pcbrouter.routing.escape import PinEscape, analyse_pin_escape
from pcbrouter.routing.occupancy import OccupancyMap, build_occupancy
from pcbrouter.routing.validator import RouteValidator
from pcbrouter.rules.model import ItemType
from pcbrouter.rules.overrides import RuleOverrides
from pcbrouter.rules.resolver import RuleResolver
from pcbrouter.rules.ruleset import RULE_ENGINE_VERSION, RuleSet, build_ruleset

log = logging.getLogger(__name__)

_T = TypeVar("_T")


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
    def _locked(self, name: str, build: Callable[[], _T]) -> _T:
        """Thread-safe lazy build: ``cached_property`` alone can build twice
        when two routing threads first touch the same analysis together."""
        cached = self.__dict__.get(name)
        if cached is not None:
            return cast("_T", cached)
        with self._lock:
            cached = self.__dict__.get(name)
            if cached is not None:
                return cast("_T", cached)
            value = build()
            self.__dict__[name] = value
            return value

    @property
    def geometry(self) -> BoardGeometry:
        def _build() -> BoardGeometry:
            geo = build_board_geometry(self.board)
            self.bind_refill(geo)
            return geo

        return self._locked("geometry", _build)

    def bind_refill(self, geo: BoardGeometry) -> None:
        """Connectivity on ``geo`` judges stale zone fills with these rules."""
        from pcbrouter.routing.refill import attach_refill

        key = (self.ruleset.digest, repr(self.overrides.to_dict()), self.config.conservative)
        attach_refill(geo, self._zone_clearance, key)

    def _zone_clearance(self, fill: CopperItem, other: CopperItem, layer: str) -> tuple[Nm, Nm]:
        """Clearance a refill keeps between ``fill``'s zone and ``other``, with
        KiCad 8's precedence (``DRC_ENGINE::EvalRules``): a pad/footprint local
        clearance is an override and wins (floored at the board minimum); else a
        custom rule wins outright; else the net-class value maxed with the zone's
        own clearance (not an override). Returns (certain value, value including
        rules that may apply): the first judges whether a stored fill is stale
        (a correct fill must never look stale), the second what a refill cuts."""
        from pcbrouter.geometry.board import ItemKind
        from pcbrouter.routing.collision import item_type_of
        from pcbrouter.rules.model import RuleSourceKind

        resolver = self.resolver
        board_min = resolver.ruleset.board.min_clearance or 0
        if other.kind is ItemKind.PAD and other.local_clearance:
            value = max(other.local_clearance, board_min)
            return value, value
        ctx = resolver.ruleset.uses_context
        geo = self.geometry
        r = resolver.resolve_clearance(
            fill.net, other.net, ItemType.ZONE, item_type_of(other.kind), layer,
            ctx_a=geo.context(fill) if ctx else None, ctx_b=geo.context(other) if ctx else None,
        )  # fmt: skip
        exact = r.value or 0
        if r.source.kind is not RuleSourceKind.CUSTOM_RULE:
            exact = max(exact, fill.local_clearance or 0)
        return exact, max(exact, r.possibly_stricter or 0)

    @property
    def ruleset(self) -> RuleSet:
        def _build() -> RuleSet:
            rs = build_ruleset(self.board, self.project_rules)
            log.info(
                "rules.loaded classes=%d custom=%d unsupported=%d (critical %d) digest=%s",
                len(rs.classes.classes), len(rs.custom_rules), len(rs.unsupported),
                len(rs.critical_unsupported), rs.digest,
            )  # fmt: skip
            for u in rs.unsupported:
                log.warning("rules.unsupported %s critical=%s", u.describe(), u.critical)
            return rs

        return self._locked("ruleset", _build)

    @property
    def resolver(self) -> RuleResolver:
        return self._locked(
            "resolver",
            lambda: RuleResolver(
                self.ruleset, self.overrides, conservative=self.config.conservative
            ),
        )

    @property
    def validator(self) -> RouteValidator:
        return self._locked("validator", lambda: RouteValidator(self.geometry, self.resolver))

    @property
    def connectivity(self) -> BoardConnectivity:
        return self._locked("connectivity", lambda: analyse_connectivity(self.geometry))

    @property
    def cache_key(self) -> tuple[str, str, str]:
        return (self.board.fingerprint, self.ruleset.digest, repr(self.overrides.to_dict()))

    # ------------------------------------------------------------ analyses
    def run_drc(self) -> DRCResult:
        result = run_geometry_check(self.geometry, self.resolver, self.connectivity)
        self.last_drc = result
        return result

    def occupancy(
        self,
        layer: str,
        net: str | None,
        width: Nm,
        cell: Nm,
        bounds: BoundingBox | None = None,
        item: ItemType = ItemType.TRACK,
    ) -> OccupancyMap:
        key = (*self.cache_key, layer, net, width, cell, bounds, item)
        with self._lock:
            cached = self._occupancy.get(key)
            if cached is None:
                geometry, resolver = self.geometry, self.resolver
            else:
                return cached
        # Built outside the lock so concurrent layers build in parallel (R5);
        # a duplicate build loses the race harmlessly below.
        built = build_occupancy(geometry, resolver, layer, net, width, cell, bounds, item)
        with self._lock:
            existing = self._occupancy.get(key)
            if existing is not None:
                return existing
            if len(self._occupancy) > 16:
                self._occupancy.pop(next(iter(self._occupancy)))
            self._occupancy[key] = built
            return built

    def congestion(self, layer: str) -> CongestionMap:
        key = (*self.cache_key, layer)
        with self._lock:
            cached = self._congestion.get(key)
            if cached is None:
                geometry, resolver = self.geometry, self.resolver
            else:
                return cached
        built = build_congestion(geometry, resolver, layer)
        with self._lock:
            return self._congestion.setdefault(key, built)

    def pin_escape(self, pad_uid: str) -> PinEscape:
        return analyse_pin_escape(
            self.geometry, self.validator.engine, self.geometry.copper[pad_uid]
        )

    def with_overrides(
        self, overrides: RuleOverrides, config: EngineConfig | None = None
    ) -> BoardEngine:
        """A new engine sharing this board's geometry (rules re-resolved)."""
        engine = BoardEngine(self.board, self.project_rules, overrides, config or self.config)
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
