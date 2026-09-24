"""Provider-neutral request model built by the PromptBuilder."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class AIMode(StrEnum):
    ANALYZE = "analyze"
    PLAN = "plan"
    COMMAND = "command"
    EXPLAIN = "explain"

    @property
    def label(self) -> str:
        return self.value.capitalize()


def new_request_id() -> str:
    return f"req-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class AIRequest:
    """Everything an adapter needs, and nothing provider-specific.

    ``response_schema`` is the provider-friendly *wire* schema (see
    :mod:`pcbrouter.ai.wire_schema`). Adapters use it for native structured output
    when available; the application always re-validates locally regardless.
    """

    request_id: str
    mode: AIMode
    model: str
    system_prompt: str
    messages: tuple[ChatMessage, ...]
    response_schema: dict[str, Any] | None
    schema_name: str = "pcb_planner_response"
    timeout_s: float = 90.0
    max_output_tokens: int = 4096
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def prompt_characters(self) -> int:
        return len(self.system_prompt) + sum(len(m.content) for m in self.messages)
