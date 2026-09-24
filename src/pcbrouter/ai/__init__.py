"""AI integration boundary.

Stage 1 contains interfaces and the validated command schema only — no provider
implementations and no network calls. The LLM never edits board geometry: its
output must become a validated :data:`~pcbrouter.ai.command_schema.PCBCommand`
that the deterministic router executes.
"""

from __future__ import annotations

from pcbrouter.ai.command_schema import (
    MODIFYING_OPERATIONS,
    CommandValidationError,
    PCBCommand,
    RoutingConstraints,
    command_json_schema,
    parse_command,
    validate_against_board,
)
from pcbrouter.ai.provider import (
    STAGE_UNAVAILABLE_MESSAGE,
    AIProvider,
    AIProviderError,
    AIRequest,
    AIResponse,
    BoardContext,
    ContextPolicy,
    ProviderConfig,
    ProviderKind,
)

__all__ = [
    "MODIFYING_OPERATIONS",
    "STAGE_UNAVAILABLE_MESSAGE",
    "AIProvider",
    "AIProviderError",
    "AIRequest",
    "AIResponse",
    "BoardContext",
    "CommandValidationError",
    "ContextPolicy",
    "PCBCommand",
    "ProviderConfig",
    "ProviderKind",
    "RoutingConstraints",
    "command_json_schema",
    "parse_command",
    "validate_against_board",
]
