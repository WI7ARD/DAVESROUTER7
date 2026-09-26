"""Provider-friendly ("wire") JSON Schema derived from the Pydantic command models.

Native structured-output features accept only a subset of JSON Schema. OpenAI's
strict mode, for example, requires every property to be listed in ``required`` and
every object to set ``additionalProperties: false``; numeric bounds, patterns and
``$ref`` support vary by provider.

So there are two schemas with different jobs:

* **wire schema** (this module): shape only — types, enums, required keys, nullability.
  Sent to the provider to *guide* generation.
* **Pydantic models** (:mod:`command_schema`): the authority. Every response is
  re-validated locally with all bounds, patterns and cross-field rules, whatever
  the provider claims to have enforced.

Optional fields become "required but nullable" on the wire; the parser strips
``null`` values before local validation, so they fall back to "not specified".
"""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from typing import Any

from pcbrouter.ai.command_schema import PlannerResponse

_KEEP = {
    "type",
    "properties",
    "required",
    "items",
    "enum",
    "anyOf",
    "description",
    "additionalProperties",
}
_MAX_DESCRIPTION = 300


def _resolve(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            merged = {**copy.deepcopy(defs[name]), **{k: v for k, v in node.items() if k != "$ref"}}
            return _resolve(merged, defs)
        return {k: _resolve(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(v, defs) for v in node]
    return node


def _is_nullable(schema: dict[str, Any]) -> bool:
    if schema.get("type") == "null":
        return True
    return any(_is_nullable(s) for s in schema.get("anyOf", []) if isinstance(s, dict))


def _simplify(node: dict[str, Any]) -> dict[str, Any]:
    node = dict(node)
    if "oneOf" in node:  # discriminated unions
        node["anyOf"] = node.pop("oneOf")
    if "const" in node:
        node["enum"] = [node.pop("const")]
        node.setdefault("type", "string")
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key not in _KEEP:
            continue
        if key == "description":
            out[key] = " ".join(str(value).split())[:_MAX_DESCRIPTION]
        elif key == "properties":
            out[key] = {name: _simplify(prop) for name, prop in value.items()}
        elif key == "items":
            out[key] = _simplify(value)
        elif key == "anyOf":
            out[key] = [_simplify(v) for v in value]
        else:
            out[key] = value
    if out.get("type") == "object" or "properties" in out:
        props: dict[str, Any] = out.get("properties", {})
        originally_required = set(node.get("required", []))
        for name, prop in list(props.items()):
            if name not in originally_required and not _is_nullable(prop):
                props[name] = {"anyOf": [prop, {"type": "null"}]}
        out["type"] = "object"
        out["properties"] = props
        out["required"] = list(props)
        out["additionalProperties"] = False
    if "anyOf" in out and "type" in out:
        del out["type"]  # the branches carry their own types
    return out


@lru_cache(maxsize=1)
def _planner_wire_schema_json() -> str:
    raw = PlannerResponse.model_json_schema()
    defs = raw.pop("$defs", {})
    return json.dumps(_simplify(_resolve(raw, defs)), sort_keys=True)


def planner_wire_schema() -> dict[str, Any]:
    """A fresh copy of the wire schema for :class:`PlannerResponse`."""
    result: dict[str, Any] = json.loads(_planner_wire_schema_json())
    return result


def compact_schema_text() -> str:
    """Schema as compact JSON text, for providers without native structured output."""
    return _planner_wire_schema_json()
