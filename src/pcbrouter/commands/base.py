"""Command pattern foundation.

Every user-level operation — from the GUI, the CLI, a future AI pipeline, or a
future scripting API — is expressed as a :class:`BaseCommand` and executed through
the :class:`~pcbrouter.commands.command_bus.CommandBus`. That gives one place to
enforce policy (read-only mode, validation), log, time, and record history.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from pcbrouter.compute.manager import ComputeManager
    from pcbrouter.history.history import HistoryManager
    from pcbrouter.project.manager import ProjectManager


@dataclass(frozen=True, slots=True)
class CommandResult:
    success: bool
    message: str
    data: Any = None
    #: Technical detail for logs; ``message`` is what the user sees.
    error_detail: str | None = None
    duration_s: float = 0.0

    @classmethod
    def ok(cls, message: str, data: Any = None) -> CommandResult:
        return cls(True, message, data)

    @classmethod
    def fail(cls, message: str, error_detail: str | None = None) -> CommandResult:
        return cls(False, message, None, error_detail)


@dataclass(slots=True)
class CommandContext:
    """Services a command may use. Commands never reach into the UI."""

    project: ProjectManager
    history: HistoryManager
    compute: ComputeManager | None = None
    extras: dict[str, Any] = field(default_factory=dict)


class BaseCommand(ABC):
    #: Stable identifier used in logs and (future) scripting/AI mapping.
    name: ClassVar[str] = "command"
    #: True if executing would change board geometry. The bus refuses these while
    #: the application is in read-only mode (all of Stage 1).
    modifies_board: ClassVar[bool] = False

    @abstractmethod
    def execute(self, ctx: CommandContext) -> CommandResult: ...

    def describe(self) -> str:
        """Short human-readable description used in logs and history."""
        return self.name
