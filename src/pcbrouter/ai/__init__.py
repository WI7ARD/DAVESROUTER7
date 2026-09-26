"""AI engineering layer (Stage 2).

Natural language is compiled into *validated* structured PCB commands::

    prompt -> BoardContextBuilder -> PromptBuilder -> AIProvider (OpenAI / Anthropic /
    compatible) -> command_parser (schema) -> command_validator (semantics vs. board)
    -> CommandProposal -> user approval -> history

The model is an engineering planner, never the router: it cannot edit geometry,
files or code. Provider SDKs are optional and imported only inside adapters.
"""

from __future__ import annotations

from pcbrouter.ai.command_parser import (
    CommandValidationError,
    parse_command_payload,
    parse_planner_response,
)
from pcbrouter.ai.command_schema import (
    AICommand,
    Operation,
    OperationCategory,
    PlannerResponse,
    RoutingConstraints,
)
from pcbrouter.ai.command_validator import SemanticValidator, ValidationReport, ValidationStatus
from pcbrouter.ai.exceptions import AIProviderError
from pcbrouter.ai.profiles import ProviderKind, ProviderProfile
from pcbrouter.ai.provider import STAGE_UNAVAILABLE_MESSAGE, AIProvider
from pcbrouter.ai.requests import AIMode, AIRequest
from pcbrouter.ai.responses import AIResponse

__all__ = [
    "STAGE_UNAVAILABLE_MESSAGE",
    "AICommand",
    "AIMode",
    "AIProvider",
    "AIProviderError",
    "AIRequest",
    "AIResponse",
    "CommandValidationError",
    "Operation",
    "OperationCategory",
    "PlannerResponse",
    "ProviderKind",
    "ProviderProfile",
    "RoutingConstraints",
    "SemanticValidator",
    "ValidationReport",
    "ValidationStatus",
    "parse_command_payload",
    "parse_planner_response",
]
