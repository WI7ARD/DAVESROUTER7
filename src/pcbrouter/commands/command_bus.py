"""Single dispatch point for all commands."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace

from pcbrouter.commands.base import BaseCommand, CommandContext, CommandResult
from pcbrouter.kicad.errors import KiCadLoadError

log = logging.getLogger(__name__)

READ_ONLY_MESSAGE = "Board modification is disabled: this build is read-only (Stage 1)."

Listener = Callable[[BaseCommand, CommandResult], None]


class CommandBus:
    def __init__(self, context: CommandContext, *, read_only: bool = True) -> None:
        self.context = context
        self.read_only = read_only
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def dispatch(self, command: BaseCommand) -> CommandResult:
        """Execute ``command``. Never raises for command failures; returns a result."""
        start = time.perf_counter()
        if command.modifies_board and self.read_only:
            result = CommandResult.fail(READ_ONLY_MESSAGE)
            log.warning("command.rejected name=%s reason=read_only", command.name)
        else:
            try:
                result = command.execute(self.context)
            except KiCadLoadError as exc:
                result = CommandResult.fail(exc.user_message, str(exc))
                log.error("command.failed name=%s error=%s", command.name, exc)
            except Exception as exc:
                result = CommandResult.fail(
                    f"Unexpected error while running '{command.describe()}': {exc}",
                    repr(exc),
                )
                log.exception("command.crashed name=%s", command.name)
        result = replace(result, duration_s=time.perf_counter() - start)
        log.info(
            "command.done name=%s success=%s ms=%.1f message=%r",
            command.name, result.success, result.duration_s * 1e3, result.message,
        )  # fmt: skip
        for listener in list(self._listeners):
            listener(command, result)
        return result
