"""Logical component (designator + value) bound to its physical footprint."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from pcbrouter.domain.footprint import Footprint


def _empty_props() -> Mapping[str, str]:
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class Component:
    """What a user thinks of as "a part": ``R1 / 10k / R_0603``.

    The split between :class:`Component` (identity, BOM data) and
    :class:`Footprint` (geometry) lets future stages lock or constrain parts by
    designator without touching geometry objects.
    """

    reference: str
    value: str | None
    footprint: Footprint
    properties: Mapping[str, str] = field(default_factory=_empty_props)

    @property
    def id(self) -> str:
        return self.footprint.id
