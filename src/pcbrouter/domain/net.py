"""Electrical nets."""

from __future__ import annotations

from dataclasses import dataclass

from pcbrouter.domain.units import Nm

#: KiCad reserves net code 0 / empty name for "no net".
UNCONNECTED_NET_NAME = ""


@dataclass(frozen=True, slots=True)
class Net:
    """A named electrical net.

    ``code`` is KiCad's numeric id. It is informational: newer KiCad versions may
    reference nets by name only, so the *name* is the stable key everywhere in this
    application.
    """

    name: str
    code: int | None = None

    @property
    def is_unconnected(self) -> bool:
        return self.name == UNCONNECTED_NET_NAME

    @property
    def display_name(self) -> str:
        return self.name if self.name else "<no net>"


@dataclass(frozen=True, slots=True)
class NetStatistics:
    """Derived per-net counts, computed once per board by :class:`BoardIndex`."""

    net: Net
    pad_count: int
    track_count: int
    via_count: int
    routed_length: Nm  # sum of track centreline lengths (vias excluded)
