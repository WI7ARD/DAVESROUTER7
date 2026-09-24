"""Bounded per-board conversation.

Only the last ``max_turns`` exchanges are resent, and assistant turns are resent as
their short ``message`` text (not the full JSON), so a long session cannot grow the
request without bound. Durable decisions (approved constraints) travel separately
in the ``<session_state>`` block, so they are not lost when old turns drop off.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from pcbrouter.ai.requests import AIMode, ChatMessage

MAX_TURN_CHARS = 4000


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    role: str  # "user" | "assistant"
    text: str  # as shown to the user (real names)
    mode: AIMode
    request_id: str
    sent_text: str = ""  # as sent to/received from the model (possibly anonymised)
    timestamp: float = field(default_factory=time.time)


class Conversation:
    def __init__(self, max_turns: int = 6) -> None:
        self.max_turns = max_turns
        self._turns: list[ConversationTurn] = []

    @property
    def turns(self) -> list[ConversationTurn]:
        return list(self._turns)

    def add(self, turn: ConversationTurn) -> None:
        self._turns.append(turn)

    def remove_request(self, request_id: str) -> None:
        """Drop every turn of a request (used when a request is cancelled/failed)."""
        self._turns = [t for t in self._turns if t.request_id != request_id]

    def history_messages(self) -> list[ChatMessage]:
        """Recent complete exchanges, alternating user/assistant, oldest first."""
        pairs: list[tuple[ConversationTurn, ConversationTurn]] = []
        pending: ConversationTurn | None = None
        for turn in self._turns:
            if turn.role == "user":
                pending = turn
            elif turn.role == "assistant" and pending is not None:
                if pending.request_id == turn.request_id:
                    pairs.append((pending, turn))
                pending = None
        recent = pairs[-self.max_turns :] if self.max_turns > 0 else []
        out: list[ChatMessage] = []
        for user, assistant in recent:
            out.append(ChatMessage("user", (user.sent_text or user.text)[:MAX_TURN_CHARS]))
            out.append(
                ChatMessage("assistant", (assistant.sent_text or assistant.text)[:MAX_TURN_CHARS])
            )
        return out
