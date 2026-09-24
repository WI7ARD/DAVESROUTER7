"""Session usage accounting and approximate token estimation.

Costs are **not** computed: provider pricing changes and is not shipped with the
application, so estimated cost is reported as unavailable rather than invented.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

#: Rough characters-per-token for English/JSON-like text. Estimates only.
_CHARS_PER_TOKEN = 4.0


def estimate_tokens(text: str) -> int:
    """Approximate token count (±30 % is typical). Never presented as exact."""
    if not text:
        return 0
    try:  # use a real tokenizer if the user happens to have one installed
        import tiktoken  # type: ignore[import-not-found]

        return len(tiktoken.get_encoding("o200k_base").encode(text))
    except Exception:
        return math.ceil(len(text) / _CHARS_PER_TOKEN)


def format_token_estimate(n: int) -> str:
    return f"~{n:,} tokens"


@dataclass(frozen=True, slots=True)
class UsageRecord:
    request_id: str
    provider: str
    profile_name: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_s: float | None
    succeeded: bool
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True, slots=True)
class UsageSummary:
    requests: int
    failed_requests: int
    input_tokens: int
    output_tokens: int
    requests_without_usage: int
    by_model: dict[str, int]
    estimated_cost: str = "Unavailable (pricing is not tracked)"


class UsageTracker:
    def __init__(self) -> None:
        self._records: list[UsageRecord] = []

    def add(self, record: UsageRecord) -> None:
        self._records.append(record)

    @property
    def records(self) -> list[UsageRecord]:
        return list(self._records)

    def summary(self) -> UsageSummary:
        ok = [r for r in self._records if r.succeeded]
        by_model: dict[str, int] = {}
        for r in self._records:
            key = f"{r.profile_name} · {r.model}"
            by_model[key] = by_model.get(key, 0) + 1
        return UsageSummary(
            requests=len(self._records),
            failed_requests=len(self._records) - len(ok),
            input_tokens=sum(r.input_tokens or 0 for r in ok),
            output_tokens=sum(r.output_tokens or 0 for r in ok),
            requests_without_usage=sum(
                1 for r in ok if r.input_tokens is None or r.output_tokens is None
            ),
            by_model=by_model,
        )
