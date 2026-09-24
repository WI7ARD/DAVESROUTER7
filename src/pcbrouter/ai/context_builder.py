"""Compact, bounded engineering context for the model.

The original ``.kicad_pcb`` is **never** sent. Instead this module renders a short,
deterministic text summary from :class:`BoardFactService` facts.

Prompt-injection defence: every string that originated from the board file (net
names, references, values, footprint names…) is emitted as a JSON string literal
with ``<`` and ``>`` escaped. Board text therefore cannot close the ``<pcb_context>``
delimiter or look like application instructions, and the system prompt tells the
model that everything inside the delimiter is untrusted data.

Size control: rows are added in relevance order (entities the user mentioned or
selected first, then the most connected) until the net/component/character limits
are reached; anything omitted is stated explicitly.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum

from pcbrouter.ai.anonymizer import AnonymizationOptions, Anonymizer, EntityKind
from pcbrouter.ai.board_summary import BoardFactService, NetFacts, NetRoutingStatus
from pcbrouter.ai.usage import estimate_tokens
from pcbrouter.domain.board import Board

CONTEXT_FORMAT_VERSION = 1


class ContextLevel(StrEnum):
    MINIMAL = "minimal"  # statistics + mentioned/selected entities
    STANDARD = "standard"  # + relevant components and nets
    DETAILED = "detailed"  # + per-net components, per-component nets, all net names

    @property
    def label(self) -> str:
        return self.value.capitalize()


@dataclass(frozen=True, slots=True)
class ContextLimits:
    max_chars: int = 60_000
    max_nets: int = 200
    max_components: int = 150


@dataclass(frozen=True, slots=True)
class BoardContext:
    text: str
    board_fingerprint: str
    session_id: str
    board_revision: int
    level: ContextLevel
    included_nets: int
    total_nets: int
    included_components: int
    total_components: int
    truncated: bool
    anonymized: tuple[str, ...]
    disclosure: tuple[str, ...]  # human-readable categories of data included
    format_version: int = CONTEXT_FORMAT_VERSION
    built_at: float = field(default_factory=time.time)

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def token_estimate(self) -> int:
        return estimate_tokens(self.text)


def quote(value: str | None) -> str:
    """JSON string literal safe to embed inside the context delimiters."""
    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")


def mentioned_names(text: str, candidates: list[str]) -> set[str]:
    """Candidates that appear as whole words in ``text`` (case-insensitive)."""
    found = set()
    lowered = text.lower()
    for name in candidates:
        if len(name) < 2:
            continue
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name.lower())}(?![A-Za-z0-9_])", lowered):
            found.add(name)
    return found


class BoardContextBuilder:
    def __init__(self, board: Board, *, session_id: str = "", board_revision: int = 0) -> None:
        self.board = board
        self.facts = BoardFactService(board)
        self.session_id = session_id
        self.board_revision = board_revision

    def build(
        self,
        level: ContextLevel = ContextLevel.STANDARD,
        limits: ContextLimits | None = None,
        *,
        user_prompt: str = "",
        selected_nets: tuple[str, ...] = (),
        selected_components: tuple[str, ...] = (),
        anonymizer: Anonymizer | None = None,
    ) -> BoardContext:
        lim = limits or ContextLimits()
        anon = anonymizer or Anonymizer(self.board, AnonymizationOptions())
        board = self.board
        idx = board.index
        all_nets = [n.name for n in board.nets]
        all_refs = sorted(idx.components_by_ref)

        focus_nets = set(selected_nets) | mentioned_names(user_prompt, all_nets)
        focus_refs = set(selected_components) | mentioned_names(user_prompt, all_refs)

        def net_out(n: str) -> str:
            return quote(anon.out(EntityKind.NET, n))

        def ref_out(r: str) -> str:
            return quote(anon.out(EntityKind.REFERENCE, r))

        def val_out(v: str | None) -> str:
            return quote(anon.out(EntityKind.VALUE, v) if v else None)

        bf = self.facts.board_facts(anon.board_name())
        lines: list[str] = []
        size = (
            f"{bf.width_mm} x {bf.height_mm}"
            if bf.width_mm is not None
            else "unknown (no board outline)"
        )
        lines += [
            "BOARD",
            f"name: {quote(bf.name)}",
            f"outline_size_mm: {size}",
            f"copper_layers: {json.dumps(list(bf.copper_layers))}",
            "net_classes: unknown (not loaded in this version)",
            f"known_min_track_width_mm: {bf.min_track_width_mm or 'unknown'}",
            f"known_min_clearance_mm: {bf.min_clearance_mm or 'unknown'}",
            "",
            "STATISTICS",
            f"components: {bf.components} | pads: {bf.pads} | nets: {bf.nets} | "
            f"tracks: {bf.tracks} | vias: {bf.vias} | "
            f"nets_with_no_tracks: {bf.nets_without_tracks}",
        ]
        disclosure = ["board dimensions and layer names", "board statistics"]
        budget_hit = False

        def room(extra: str) -> bool:
            nonlocal budget_hit
            if sum(len(x) + 1 for x in lines) + len(extra) + 400 > lim.max_chars:
                budget_hit = True
                return False
            return True

        # ---------------------------------------------------------------- components
        if level is ContextLevel.MINIMAL:
            comp_order = sorted(focus_refs)
        else:
            others = sorted(
                (r for r in all_refs if r not in focus_refs),
                key=lambda r: (-len(idx.components_by_ref[r].footprint.pads), r),
            )
            comp_order = sorted(focus_refs) + others
        comp_rows = 0
        if comp_order:
            lines += ["", "COMPONENTS (reference, value, footprint, side, pads, locked)"]
            disclosure += ["component references, values and footprint names"]
            for ref in comp_order[: lim.max_components]:
                cf = self.facts.component_facts(ref)
                if cf is None:
                    continue
                footprint = quote(anon.footprint(cf.footprint))
                row = (
                    f"{ref_out(ref)} value={val_out(cf.value)} footprint={footprint} "
                    f"side={cf.side} pads={cf.pad_count} locked={'yes' if cf.locked else 'no'}"
                )
                if level is ContextLevel.DETAILED:
                    row += " nets=[" + ", ".join(net_out(n) for n in cf.nets) + "]"
                if not room(row):
                    break
                lines.append(row)
                comp_rows += 1

        # ---------------------------------------------------------------- nets
        if level is ContextLevel.MINIMAL:
            net_order = sorted(focus_nets)
        else:

            def rank(name: str) -> tuple[int, int, str]:
                st = idx.net_statistics[name]
                return (0 if st.track_count == 0 and st.pad_count >= 2 else 1, -st.pad_count, name)

            net_order = sorted(focus_nets) + sorted(
                (n for n in all_nets if n not in focus_nets), key=rank
            )
        net_rows = 0
        if net_order:
            lines += ["", "NETS (name, pads, tracks, vias, routed length, widths, layers, status)"]
            disclosure += ["net names and routing statistics"]
            for name in net_order[: lim.max_nets]:
                nf = self.facts.net_facts(name)
                if nf is None:
                    continue
                row = self._net_row(nf, net_out, ref_out, level)
                if not room(row):
                    break
                lines.append(row)
                net_rows += 1

        without = self.facts.nets_without_tracks()
        if without and level is not ContextLevel.MINIMAL:
            row = (
                "NETS_WITH_NO_TRACKS (>=2 pads, no copper; connectivity not analysed): ["
                + ", ".join(net_out(n) for n in without)
                + "]"
            )
            if room(row):
                lines += ["", row]

        omitted_nets = len(all_nets) - net_rows
        names_only = 0
        if level is not ContextLevel.MINIMAL and omitted_nets > 0:
            # Details did not fit: still list every net name, so the model knows what exists.
            names = "ALL_NET_NAMES: [" + ", ".join(net_out(n) for n in all_nets) + "]"
            if room(names):
                lines += ["", names]
                names_only, omitted_nets = omitted_nets, 0
        if focus_nets or focus_refs:
            lines += [
                "",
                "FOCUS (mentioned in the request or selected by the user): ["
                + ", ".join(
                    [net_out(n) for n in sorted(focus_nets)]
                    + [ref_out(r) for r in sorted(focus_refs)]
                )
                + "]",
            ]
        omitted_comps = len(comp_order) - comp_rows if comp_order else 0
        truncated = budget_hit or omitted_nets > 0 or omitted_comps > 0 or names_only > 0
        notes = []
        if level is ContextLevel.MINIMAL:
            notes.append("context level MINIMAL: only statistics and focus entities are listed")
        if names_only:
            notes.append(f"{names_only} net(s) listed by name only (no details)")
        if omitted_nets > 0 and level is not ContextLevel.MINIMAL:
            notes.append(f"{omitted_nets} net(s) not listed")
        if omitted_comps > 0:
            notes.append(f"{omitted_comps} component(s) not listed")
        if budget_hit:
            notes.append("character limit reached")
        if notes:
            lines += [
                "",
                "OMITTED: "
                + "; ".join(notes)
                + ". Do not assume unlisted entities do not exist; ask if needed.",
            ]

        if anon.options.any:
            disclosure.append("anonymised: " + ", ".join(anon.options.enabled_labels()))
        return BoardContext(
            text="\n".join(lines),
            board_fingerprint=board.fingerprint,
            session_id=self.session_id,
            board_revision=self.board_revision,
            level=level,
            included_nets=net_rows,
            total_nets=len(all_nets),
            included_components=comp_rows,
            total_components=len(all_refs),
            truncated=truncated,
            anonymized=tuple(anon.options.enabled_labels()),
            disclosure=tuple(disclosure),
        )

    @staticmethod
    def _net_row(nf: NetFacts, net_out, ref_out, level: ContextLevel) -> str:  # type: ignore[no-untyped-def]
        status = {
            NetRoutingStatus.NO_PADS: "no_pads",
            NetRoutingStatus.SINGLE_PAD: "single_pad",
            NetRoutingStatus.NO_TRACKS: "NO_TRACKS",
            NetRoutingStatus.HAS_TRACKS: "has_tracks",
        }[nf.status]
        row = (
            f"{net_out(nf.name)} pads={nf.pad_count} tracks={nf.track_count} "
            f"vias={nf.via_count} routed_mm={nf.routed_length_mm}"
        )
        if nf.track_widths_mm:
            row += f" widths_mm={list(nf.track_widths_mm)}"
        if nf.layers_used:
            row += f" layers={json.dumps(list(nf.layers_used))}"
        if nf.locked_track_count:
            row += f" locked_tracks={nf.locked_track_count}"
        row += f" status={status}"
        if level is ContextLevel.DETAILED and nf.components:
            row += " parts=[" + ", ".join(ref_out(r) for r in nf.components) + "]"
        return row
