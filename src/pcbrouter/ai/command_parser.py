"""Deserialise model output into schema objects — safely.

* JSON only (``json.loads``). Never ``eval``, never pickle, never YAML object tags.
* ``null`` values are stripped before validation so they mean "not specified"
  (the wire schema makes optional fields required-but-nullable).
* Strict Pydantic validation with bounds; any failure is an
  :class:`AISchemaValidationError` listing every problem.
* Markdown code fences are *not* relied upon. As a documented courtesy to local
  models, a response that is exactly one fenced JSON block is unwrapped, with a note.
* Stage 1's compact command form is converted to the v2 envelope.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from pcbrouter.ai.command_schema import AICommand, PlannerResponse
from pcbrouter.ai.exceptions import AIInvalidResponseError, AISchemaValidationError

MAX_RESPONSE_CHARS = 200_000
_FENCE_RE = re.compile(r"^```(?:json|JSON)?[ \t]*\n(.*)\n```$", re.DOTALL)


class CommandValidationError(ValueError):
    """A single command payload (CLI / Stage 1 API) is not valid."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True, slots=True)
class ParsedResponse:
    response: PlannerResponse
    notes: list[str] = field(default_factory=list)


def strip_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value]
    return value


def _errors(exc: ValidationError) -> list[str]:
    out = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "<root>"
        out.append(f"{loc}: {e['msg']}")
    return out


def load_json_object(text: str) -> tuple[dict[str, Any], list[str]]:
    notes: list[str] = []
    if len(text) > MAX_RESPONSE_CHARS:
        raise AIInvalidResponseError(f"response is too large ({len(text)} characters)")
    body = text.strip()
    if not body:
        raise AIInvalidResponseError("the response was empty")
    fence = _FENCE_RE.match(body)
    if fence:
        body = fence.group(1).strip()
        notes.append("response was wrapped in a Markdown code fence; the JSON inside was used")
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AIInvalidResponseError(
            f"response is not valid JSON ({exc.msg} at line {exc.lineno})",
            user_message="The AI response was not valid JSON, so it was discarded.",
        ) from exc
    if not isinstance(obj, dict):
        raise AIInvalidResponseError("response JSON is not an object")
    return obj, notes


def parse_planner_response(text: str) -> ParsedResponse:
    obj, notes = load_json_object(text)
    try:
        response = PlannerResponse.model_validate(strip_nulls(obj))
    except ValidationError as exc:
        raise AISchemaValidationError(_errors(exc), raw_excerpt=text) from exc
    return ParsedResponse(response, notes)


# ------------------------------------------------------------------ single commands
_V1_OPERATIONS = {"set_constraint": "set_net_constraint", "optimize_route": "optimize_net"}
_V1_CONSTRAINTS = {
    "avoid_layers": "forbidden_layers",
    "move_components": "allow_component_movement",
    "track_width_mm": "preferred_trace_width_mm",
    "clearance_mm": "min_clearance_mm",
}
_V1_KEYS = {
    "target",
    "reference",
    "exclude_nets",
    "focus",
    "goals",
    "max_vias_per_net",
    "area",
    "net",
    "track_ids",
    "reason",
    "layers",
}


def _is_legacy(obj: Mapping[str, Any]) -> bool:
    if "schema_version" in obj:
        return False
    targets = obj.get("targets")
    constraints = obj.get("constraints")
    return (
        bool(_V1_KEYS & set(obj))
        or (isinstance(targets, list) and any(isinstance(t, str) for t in targets))
        or (isinstance(constraints, dict) and bool(set(_V1_CONSTRAINTS) & set(constraints)))
    )


def convert_legacy_command(obj: Mapping[str, Any]) -> dict[str, Any]:
    """Convert the Stage 1 compact form (and plain-string targets) to the v2 envelope."""
    src = dict(obj)
    op = str(src.pop("operation", ""))
    op = _V1_OPERATIONS.get(op, op)
    out: dict[str, Any] = {"operation": op}
    constraints = dict(src.pop("constraints", None) or {})
    for old, new in _V1_CONSTRAINTS.items():
        if old in constraints:
            constraints[new] = constraints.pop(old)
    targets: list[dict[str, Any]] = []
    raw_targets = src.pop("targets", None)
    target = src.pop("target", None)
    if isinstance(raw_targets, list) and all(isinstance(t, str) for t in raw_targets):
        if op == "route_group" and len(raw_targets) >= 2:
            targets.append({"type": "net_group", "names": raw_targets})
        else:
            targets.extend({"type": "net", "name": t} for t in raw_targets)
    elif raw_targets is not None:
        targets.extend(raw_targets)
    if target is not None:
        targets.append({"type": "net", "name": target})
    if (ref := src.pop("reference", None)) is not None:
        targets.append({"type": "component", "reference": ref})
    if (net := src.pop("net", None)) is not None:
        targets.append({"type": "net", "name": net})
    if (area := src.pop("area", None)) is not None:
        area = dict(area) if isinstance(area, dict) else area
        if isinstance(area, dict):
            area["type"] = "area"
            if "layers" in src:
                area["layers"] = src.pop("layers")
        targets.append(area)
    if op in ("route_board", "analyze_board") and not targets:
        targets.append({"type": "board"})
    if exclude := src.pop("exclude_nets", None):
        constraints["avoid_nets"] = exclude
    if (mv := src.pop("max_vias_per_net", None)) is not None:
        constraints["max_vias"] = mv
    if src.pop("goals", None) and op == "optimize_net" and not targets:
        targets.append({"type": "board"})
    if src.pop("track_ids", None):
        raise CommandValidationError(["track ids cannot be targeted in schema v2; target the net"])
    src.pop("focus", None)
    if reason := src.pop("reason", None):
        constraints["additional_notes"] = str(reason)[:600]
    out["targets"] = targets
    if constraints:
        out["constraints"] = constraints
    out.update(src)  # anything left is validated (and rejected if unknown)
    return out


def parse_command_payload(data: str | bytes | Mapping[str, Any]) -> AICommand:
    """Parse one command (CLI ``--check-command``, :class:`ValidateAICommand`)."""
    if isinstance(data, str | bytes):
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        try:
            obj, _ = load_json_object(text)
        except AIInvalidResponseError as exc:
            raise CommandValidationError([str(exc)]) from exc
    else:
        obj = dict(data)
    obj = strip_nulls(obj)
    if _is_legacy(obj):
        obj = convert_legacy_command(obj)
    try:
        return AICommand.model_validate(obj)
    except ValidationError as exc:
        raise CommandValidationError(_errors(exc)) from exc
