"""Compile a user request into a provider-independent :class:`AIRequest`.

Message layout (identical for every provider)::

    system:    PLANNER_SYSTEM_PROMPT + mode instructions          (static, no PCB data)
    user/asst: bounded recent conversation
    user:      <pcb_context>  compact board context (untrusted data) </pcb_context>
               <session_state> approved constraints, selection     </session_state>
               <user_request mode="...">  the user's words        </user_request>
"""

from __future__ import annotations

from dataclasses import dataclass

from pcbrouter.ai.context_builder import BoardContext
from pcbrouter.ai.conversation import Conversation
from pcbrouter.ai.requests import AIMode, AIRequest, ChatMessage, new_request_id
from pcbrouter.ai.system_prompts import (
    MODE_INSTRUCTIONS,
    PLANNER_SYSTEM_PROMPT,
    SCHEMA_FALLBACK_INSTRUCTION,
)
from pcbrouter.ai.wire_schema import compact_schema_text, planner_wire_schema

MAX_USER_PROMPT_CHARS = 8000
_TAGS = ("pcb_context", "session_state", "user_request")


def neutralise_tags(text: str) -> str:
    """Stop text from opening/closing our delimiter tags."""
    for tag in _TAGS:
        text = text.replace(f"<{tag}", f"‹{tag}").replace(f"</{tag}", f"‹/{tag}")
    return text


def schema_instruction() -> str:
    """Appended to the system prompt by adapters that cannot enforce a schema natively."""
    return SCHEMA_FALLBACK_INSTRUCTION + compact_schema_text()


@dataclass(frozen=True, slots=True)
class PromptInputs:
    mode: AIMode
    user_prompt: str  # already anonymised if anonymisation is on
    context: BoardContext
    model: str
    session_state_lines: tuple[str, ...] = ()
    timeout_s: float = 90.0
    max_output_tokens: int = 4096


class PromptBuilder:
    def build(self, inputs: PromptInputs, conversation: Conversation | None = None) -> AIRequest:
        prompt = inputs.user_prompt.strip()
        if not prompt:
            raise ValueError("the prompt is empty")
        if len(prompt) > MAX_USER_PROMPT_CHARS:
            raise ValueError(f"the prompt is longer than {MAX_USER_PROMPT_CHARS} characters")
        system = f"{PLANNER_SYSTEM_PROMPT}\n{MODE_INSTRUCTIONS[inputs.mode]}\n"
        state = "\n".join(inputs.session_state_lines) or "none"
        final = (
            "<pcb_context>\n"
            f"{neutralise_tags(inputs.context.text)}\n"
            "</pcb_context>\n\n"
            "<session_state>\n"
            f"{neutralise_tags(state)}\n"
            "</session_state>\n\n"
            f'<user_request mode="{inputs.mode.value}">\n'
            f"{neutralise_tags(prompt)}\n"
            "</user_request>"
        )
        history = conversation.history_messages() if conversation is not None else []
        return AIRequest(
            request_id=new_request_id(),
            mode=inputs.mode,
            model=inputs.model,
            system_prompt=system,
            messages=(*history, ChatMessage("user", final)),
            response_schema=planner_wire_schema(),
            timeout_s=inputs.timeout_s,
            max_output_tokens=inputs.max_output_tokens,
            metadata={
                "board_fingerprint": inputs.context.board_fingerprint,
                "session_id": inputs.context.session_id,
                "context_level": inputs.context.level.value,
            },
        )
