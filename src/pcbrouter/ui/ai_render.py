"""HTML rendering of AI interactions and proposals (pure functions, testable).

Claim labels keep speculation distinct from truth:

* **BOARD FACT** — computed deterministically from the loaded board;
* **AI OBSERVATION** — the model's inference (may be wrong);
* **DRC RESULT** — only from the deterministic Internal Geometry Check (never KiCad DRC);
* **USER CONSTRAINT** — values the user set or changed.

Every string is HTML-escaped: model output can never inject markup or links.
"""

from __future__ import annotations

import html
import time
from collections.abc import Sequence

from pcbrouter.ai.board_summary import BoardFactService
from pcbrouter.ai.command_schema import (
    AICommand,
    AreaTarget,
    BoardTarget,
    ComponentTarget,
    NetGroupTarget,
    NetTarget,
)
from pcbrouter.ai.command_validator import RuleCheck, Severity
from pcbrouter.ai.proposals import CommandProposal, CommandState
from pcbrouter.ai.session import Interaction, InteractionKind

LABEL_FACT = "BOARD FACT"
LABEL_AI = "AI OBSERVATION"
LABEL_DRC = "DRC RESULT"
LABEL_USER = "USER CONSTRAINT"
#: Stage 3: verdicts of the deterministic geometry/rule engine — authoritative over the AI.
LABEL_RULE = "DETERMINISTIC RULE CHECK"
_OUTCOME_COLORS = {
    "VALID": "#3fb950",
    "INVALID": "#f85149",
    "RULE_UNKNOWN": "#d29922",
    "FACT": "#58a6ff",
}


def rule_checks_html(checks: Sequence[RuleCheck]) -> str:
    """A visually authoritative block: the engine's verdict, not the model's."""
    if not checks:
        return ""
    rows = []
    for c in checks:
        color = _OUTCOME_COLORS.get(c.outcome, "#8b949e")
        rows.append(
            f"<tr><td><b style='color:{color}'>{esc(c.outcome)}</b>&nbsp;</td>"
            f"<td>{esc(c.label)} — {esc(c.detail)}"
            + (f"<br><span style='color:#8b949e'>Rule: {esc(c.source)}</span>" if c.source else "")
            + "</td></tr>"
        )
    return (
        "<table width='100%' cellpadding=4 style='border:2px solid #58a6ff; margin:4px 0'>"
        f"<tr><td colspan=2><b>{esc(LABEL_RULE)}</b> "
        "<span style='color:#8b949e'>(deterministic engine — overrides any AI statement)</span>"
        "</td></tr>" + "".join(rows) + "</table>"
    )


_COLORS = {LABEL_FACT: "#3fb950", LABEL_AI: "#d29922", LABEL_DRC: "#8b949e", LABEL_USER: "#58a6ff"}
_STATE_COLORS = {
    CommandState.VALID: "#3fb950",
    CommandState.INVALID: "#f85149",
    CommandState.APPROVED: "#58a6ff",
    CommandState.EXECUTED: "#58a6ff",
    CommandState.REJECTED: "#8b949e",
    CommandState.EXPIRED: "#8b949e",
}


def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def badge(label: str) -> str:
    color = _COLORS.get(label, "#8b949e")
    return f'<span style="color:{color}; font-weight:bold; font-size:small">[{label}]</span>'


def describe_target(t: object) -> str:
    match t:
        case NetTarget(name=n):
            return f"net {n}"
        case NetGroupTarget(names=ns):
            return "net group " + ", ".join(ns)
        case ComponentTarget(reference=r):
            return f"component {r}"
        case AreaTarget():
            return f"area ({t.x_min_mm}, {t.y_min_mm})–({t.x_max_mm}, {t.y_max_mm}) mm" + (
                f" on {', '.join(t.layers)}" if t.layers else ""
            )
        case BoardTarget():
            return "whole board"
    return str(t)


def _fmt(value: object) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(_fmt(v) for v in value) or "—"
    if isinstance(value, dict):
        return ", ".join(f"{k}={_fmt(v)}" for k, v in value.items())
    return str(value)


def constraint_rows(cmd: AICommand) -> list[tuple[str, str]]:
    c = cmd.effective_constraints
    rows = [(k.replace("_", " ").capitalize(), _fmt(v)) for k, v in c.specified_fields().items()]
    if c.preserve_existing_routes is None:
        rows.append(("Preserve existing routes", "Yes (safe default)"))
    if c.allow_component_movement is None:
        rows.append(("Allow component movement", "No (safe default)"))
    if c.allow_ripup is None:
        rows.append(("Allow rip-up", "No (safe default)"))
    return rows


def _net_names(cmd: AICommand) -> list[str]:
    out: list[str] = []
    for t in cmd.targets:
        if isinstance(t, NetTarget):
            out.append(t.name)
        elif isinstance(t, NetGroupTarget):
            out.extend(t.names)
    return out


def proposal_html(p: CommandProposal, facts: BoardFactService | None) -> str:
    cmd = p.current
    color = _STATE_COLORS.get(p.state, "#d29922")
    parts = [
        f"<h3 style='margin:0'>{esc(cmd.operation.label)}</h3>",
        f"<p><b style='color:{color}'>{esc(p.state.value.upper())}</b> — {esc(p.status_note)}"
        + (" <b>(stale)</b>" if p.stale else "")
        + "</p>",
        "<p><b>Targets</b><br>"
        + ("<br>".join(esc(describe_target(t)) for t in cmd.targets) or "none")
        + "</p>",
        "<p><b>Constraints</b></p><table cellspacing=2>"
        + "".join(
            f"<tr><td>{esc(k)}:&nbsp;</td><td><b>{esc(v)}</b></td></tr>"
            for k, v in constraint_rows(cmd)
        )
        + "</table>",
    ]
    if cmd.reasoning_summary:
        parts.append(
            f"<p>{badge(LABEL_AI)} <b>AI engineering summary</b><br>"
            f"<i>“{esc(cmd.reasoning_summary)}”</i>"
            + (
                f"<br>Model confidence: {esc(cmd.confidence)} (does not affect validation)"
                if cmd.confidence
                else ""
            )
            + "</p>"
        )
    for w in cmd.warnings or []:
        parts.append(f"<p>{badge(LABEL_AI)} ⚠ {esc(w)}</p>")
    if facts is not None:
        fact_lines = []
        for name in dict.fromkeys(_net_names(cmd)):
            nf = facts.net_facts(name)
            if nf is not None:
                fact_lines.append(
                    f"{esc(name)}: {nf.pad_count} pads, {nf.track_count} tracks, {nf.via_count} "
                    f"vias, {nf.routed_length_mm} mm routed"
                    + (f", {nf.locked_track_count} locked tracks" if nf.locked_track_count else "")
                )
        for t in cmd.targets:
            if isinstance(t, ComponentTarget) and (cf := facts.component_facts(t.reference)):
                fact_lines.append(
                    f"{esc(cf.reference)}: {esc(cf.value or '?')}, {cf.pad_count} pads, "
                    f"{'locked' if cf.locked else 'not locked'}"
                )
        if fact_lines:
            parts.append(f"<p>{badge(LABEL_FACT)}<br>" + "<br>".join(fact_lines) + "</p>")
    if p.validation is not None and p.validation.rule_checks:
        parts.append(rule_checks_html(p.validation.rule_checks))
    parts.append(
        f"<p>{badge(LABEL_DRC)} Not applicable — a proposal has no copper yet. Nothing here "
        "is a DRC result. The Internal Geometry Check (Tools menu) covers existing copper; "
        "it is not KiCad DRC.</p>"
    )
    if p.user_changes:
        parts.append(
            f"<p>{badge(LABEL_USER)} Edited by you (original AI proposal kept for "
            "traceability):<br>" + "<br>".join(esc(c) for c in p.user_changes) + "</p>"
        )
    v = p.validation
    if v is not None:
        checks = "<br>".join(("✓ " if c.passed else "✗ ") + esc(c.label) for c in v.checks)
        issues = []
        for i in v.issues:
            mark = {Severity.ERROR: "✗", Severity.WARNING: "⚠", Severity.INFO: "ℹ"}[i.severity]
            issues.append(f"{mark} {esc(i.text())}")
        parts.append(f"<p><b>Validation: {esc(v.status.label)}</b><br>{checks}</p>")
        if issues:
            parts.append("<p>" + "<br>".join(issues) + "</p>")
    return "".join(parts)


def interaction_html(inter: Interaction, proposals: dict[str, CommandProposal]) -> str:
    when = time.strftime("%H:%M:%S", time.localtime(inter.started_at))
    parts = [
        f"<p><span style='color:#8b949e'>{when}</span> <b>You</b> "
        f"<span style='color:#8b949e'>({esc(inter.mode.label)})</span><br>{esc(inter.prompt)}</p>"
    ]
    who = f"<b>{esc(inter.provider)}</b> <span style='color:#8b949e'>· {esc(inter.model)}</span>"
    if inter.kind is None:
        parts.append(f"<p>{who}<br><i>waiting…</i></p>")
        return "".join(parts)
    if inter.kind in (InteractionKind.ERROR, InteractionKind.CANCELLED):
        color = "#f85149" if inter.kind is InteractionKind.ERROR else "#8b949e"
        parts.append(f"<p>{who}<br><span style='color:{color}'>{esc(inter.message)}</span></p>")
    else:
        parts.append(f"<p>{who} {badge(LABEL_AI)}<br>{esc(inter.message)}</p>")
    parts.append(rule_checks_html(inter.rule_checks))
    a = inter.analysis
    if a is not None:
        body = [f"<i>{esc(a.summary)}</i>"]
        for title, items in (
            ("Observations", a.observations),
            ("Potential concerns (inferred, not verified)", a.potential_issues),
            ("Unknowns", a.unknowns),
            ("Warnings", a.warnings),
        ):
            if items:
                body.append(
                    f"<b>{title}</b><ul>" + "".join(f"<li>{esc(x)}</li>" for x in items) + "</ul>"
                )
        if a.recommended_priorities:
            body.append(
                "<b>Recommended priorities</b><ol>"
                + "".join(
                    f"<li>{esc(pr.item)}"
                    + (f" — <i>{esc(pr.rationale)}</i>" if pr.rationale else "")
                    + "</li>"
                    for pr in a.recommended_priorities
                )
                + "</ol>"
            )
        parts.append(f"<div>{badge(LABEL_AI)} " + "".join(body) + "</div>")
    if inter.plan_steps:
        parts.append(
            f"<p>{badge(LABEL_AI)} <b>Proposed plan</b></p><ol>"
            + "".join(f"<li>{esc(s)}</li>" for s in inter.plan_steps)
            + "</ol>"
        )
    for pid in inter.proposal_ids:
        p = proposals.get(pid)
        if p is not None:
            parts.append(
                f"<p>→ Proposal: <b>{esc(p.current.operation.label)}</b> "
                f"[{esc(p.state.value.upper())}]</p>"
            )
    for note in inter.notes:
        parts.append(f"<p style='color:#8b949e'>ℹ {esc(note)}</p>")
    if inter.usage is not None:
        u = inter.usage
        parts.append(
            f"<p style='color:#8b949e; font-size:small'>tokens in/out: "
            f"{u.input_tokens if u.input_tokens is not None else '?'}/"
            f"{u.output_tokens if u.output_tokens is not None else '?'}"
            + (f" · {inter.latency_s:.1f} s" if inter.latency_s else "")
            + "</p>"
        )
    return "".join(parts) + "<hr>"
