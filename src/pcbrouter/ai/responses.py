"""Provider-neutral response model.

Adapters return an :class:`AIResponse` holding only small, safe values: text,
usage and a few identifiers. Full SDK response objects are never stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class FinishStatus(Enum):
    COMPLETE = "complete"
    TRUNCATED = "truncated"  # hit the output-token limit
    REFUSED = "refused"  # the model declined
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int | None
    output_tokens: int | None

    @property
    def total(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class AIResponse:
    request_id: str
    provider: str  # ProviderKind value
    model: str
    content: str  # raw text as returned (expected to be JSON)
    usage: TokenUsage | None
    latency_s: float
    finish_status: FinishStatus = FinishStatus.UNKNOWN
    used_native_schema: bool = False
    #: Small diagnostic values only (response id, stop reason). Never headers.
    raw_metadata: dict[str, str] = field(default_factory=dict)
