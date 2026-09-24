"""Compute backend abstraction.

The router (Stage 4+) will ask a :class:`ComputeBackend` to run heavy kernels
(grid expansion, cost maps, batch path search). The UI and command layer only see
this interface, so a GPU implementation can be added later without touching
callers. Stage 1 ships a working :class:`~pcbrouter.compute.cpu_backend.CPUBackend`
and a GPU *placeholder* that is never available.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class BackendKind(Enum):
    CPU = "cpu"
    GPU = "gpu"


class Capability(Enum):
    """Features a backend may offer. Routing capabilities arrive in later stages."""

    GEOMETRY = "geometry"  # basic geometry / statistics on the host
    MULTITHREADING = "multithreading"
    GRID_ROUTING = "grid_routing"  # later stage
    BATCH_PATHFINDING = "batch_pathfinding"  # later stage (GPU candidate)


class BackendUnavailableError(RuntimeError):
    """Raised when initialising a backend that cannot run on this machine."""


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """Human-readable description of the hardware behind a backend."""

    backend: BackendKind
    name: str
    details: dict[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()


class ComputeBackend(ABC):
    """Base class for compute backends."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def kind(self) -> BackendKind: ...

    @property
    @abstractmethod
    def available(self) -> bool:
        """Whether :meth:`initialize` can succeed on this machine."""

    @property
    @abstractmethod
    def capabilities(self) -> frozenset[Capability]: ...

    @property
    @abstractmethod
    def initialized(self) -> bool: ...

    @abstractmethod
    def device_info(self) -> DeviceInfo: ...

    @abstractmethod
    def initialize(self) -> None:
        """Acquire resources. Raises :class:`BackendUnavailableError` if unavailable."""

    @abstractmethod
    def shutdown(self) -> None:
        """Release resources. Must be safe to call more than once."""
