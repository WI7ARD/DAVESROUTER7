"""Optional, reversible anonymisation of project-identifying names.

When enabled, the model sees ``NET_17`` instead of ``CAN_H`` or ``VALUE_4`` instead
of ``ESP32-S3``. The mapping is per board session and never leaves this machine.

Guarantees:

* **Bijective**: every real name gets exactly one token and vice versa.
* **Collision-free**: a token is never equal to any real name of the same kind, so
  a returned identifier can never be ambiguous.
* **No guessing**: identifiers the model returns are resolved by exact lookup only;
  anything else is reported as unknown to the validator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from pcbrouter.domain.board import Board


class EntityKind(StrEnum):
    NET = "net"
    REFERENCE = "reference"
    VALUE = "value"


@dataclass(frozen=True, slots=True)
class AnonymizationOptions:
    net_names: bool = False
    component_values: bool = False
    references: bool = False
    board_filename: bool = False

    @property
    def any(self) -> bool:
        return self.net_names or self.component_values or self.references or self.board_filename

    def enabled_labels(self) -> list[str]:
        return [
            label
            for flag, label in (
                (self.net_names, "net names"),
                (self.component_values, "component values and footprint names"),
                (self.references, "reference designators"),
                (self.board_filename, "board filename"),
            )
            if flag
        ]


@dataclass(frozen=True, slots=True)
class Resolution:
    real: str | None
    problem: str | None = None
    note: str | None = None


_PREFIX = {EntityKind.NET: "NET_", EntityKind.REFERENCE: "PART_", EntityKind.VALUE: "VALUE_"}


class Anonymizer:
    def __init__(self, board: Board, options: AnonymizationOptions) -> None:
        self.options = options
        self._enabled = {
            EntityKind.NET: options.net_names,
            EntityKind.REFERENCE: options.references,
            EntityKind.VALUE: options.component_values,
        }
        names = {
            EntityKind.NET: sorted(n.name for n in board.nets),
            EntityKind.REFERENCE: sorted({c.reference for c in board.components}),
            EntityKind.VALUE: sorted({c.value for c in board.components if c.value}),
        }
        self._real: dict[EntityKind, set[str]] = {k: set(v) for k, v in names.items()}
        self._fwd: dict[EntityKind, dict[str, str]] = {k: {} for k in EntityKind}
        self._rev: dict[EntityKind, dict[str, str]] = {k: {} for k in EntityKind}
        all_real = set().union(*self._real.values())
        for kind, values in names.items():
            if not self._enabled[kind]:
                continue
            n = 0
            for real in values:
                n += 1
                token = f"{_PREFIX[kind]}{n}"
                while token in all_real:  # never collide with any real identifier
                    n += 1
                    token = f"{_PREFIX[kind]}{n}"
                self._fwd[kind][real] = token
                self._rev[kind][token] = real
        self._board_name = board.metadata.source_path.name if board.metadata.source_path else None
        # Footprint names often embed the part number ("…:ESP32-S3-MINI"), so they are
        # hidden whenever component values are. Outbound only: never resolved back.
        self._footprints = {
            lib: f"FOOTPRINT_{i}"
            for i, lib in enumerate(sorted({c.footprint.lib_id for c in board.components}), 1)
        }

    # ------------------------------------------------------------ outbound
    def out(self, kind: EntityKind, real: str) -> str:
        return self._fwd[kind].get(real, real) if self._enabled[kind] else real

    def footprint(self, lib_id: str) -> str:
        if not self.options.component_values:
            return lib_id
        return self._footprints.get(lib_id, "FOOTPRINT_?")

    def board_name(self) -> str:
        if self.options.board_filename or not self._board_name:
            return "board.kicad_pcb"
        return self._board_name

    def anonymize_text(self, text: str) -> str:
        """Replace whole-word occurrences of real names in free text (the user's prompt)."""
        pairs: list[tuple[str, str]] = []
        for kind in EntityKind:
            if self._enabled[kind]:
                pairs.extend(self._fwd[kind].items())
        if self.options.board_filename and self._board_name:
            pairs.append((self._board_name, "board.kicad_pcb"))
            stem = self._board_name.rsplit(".", 1)[0]
            if len(stem) >= 3:
                pairs.append((stem, "board"))
        return _replace_words(text, pairs)

    # ------------------------------------------------------------ inbound
    def resolve(self, kind: EntityKind, identifier: str) -> Resolution:
        """Map an identifier returned by the model back to a real name (exact only)."""
        if not self._enabled[kind]:
            return Resolution(identifier)
        in_tokens = identifier in self._rev[kind]
        in_real = identifier in self._real[kind]
        if in_tokens and in_real:  # impossible by construction; checked anyway
            return Resolution(None, problem=f"identifier {identifier!r} is ambiguous")
        if in_tokens:
            return Resolution(self._rev[kind][identifier])
        if in_real:
            return Resolution(identifier, note=f"{identifier!r} was returned un-anonymised")
        return Resolution(identifier)  # unknown: the semantic validator reports it

    def deanonymize_text(self, text: str) -> str:
        """Restore real names in model text shown to the user (display only)."""
        pairs: list[tuple[str, str]] = []
        for kind in EntityKind:
            if self._enabled[kind]:
                pairs.extend(self._rev[kind].items())
        return _replace_words(text, pairs)

    def mapping(self) -> dict[str, dict[str, str]]:
        """Real → token mapping (kept locally; exported only on explicit request)."""
        return {k.value: dict(v) for k, v in self._fwd.items() if v}


def _replace_words(text: str, pairs: list[tuple[str, str]]) -> str:
    if not pairs or not text:
        return text
    lookup = dict(pairs)
    alternation = "|".join(re.escape(k) for k in sorted(lookup, key=len, reverse=True) if k)
    pattern = re.compile(rf"(?<![A-Za-z0-9_])(?:{alternation})(?![A-Za-z0-9_])")
    return pattern.sub(lambda m: lookup[m.group(0)], text)
