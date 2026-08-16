"""Pydantic -> plain JSON Schema for backends that can't follow $ref.

Ollama compiles the schema down to a grammar and does not resolve `$defs`/`$ref`,
so nested models have to be inlined before they are sent.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def _resolve(node: Any, defs: dict[str, Any], seen: tuple[str, ...] = ()) -> Any:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.split("/")[-1]
            if name in seen:
                # Recursive model - grammars can't express this. Degrade to a
                # free-form object rather than recursing forever.
                return {"type": "object"}
            target = _resolve(defs.get(name, {}), defs, seen + (name,))
            extra = {k: v for k, v in node.items() if k != "$ref"}
            return {**target, **extra} if extra else target
        return {k: _resolve(v, defs, seen) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_resolve(item, defs, seen) for item in node]
    return node


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Fully inlined JSON Schema for `model`."""
    raw = model.model_json_schema()
    defs = raw.get("$defs", {})
    schema = _resolve(raw, defs)
    schema.pop("$defs", None)
    return schema


def cap_strings(schema: dict[str, Any], max_length: int) -> dict[str, Any]:
    """Add `maxLength` to every string in the schema that has none.

    A grammar-constrained local model that falls into a repetition loop inside a
    free-text field keeps going until num_predict, then hands back an
    unterminated string. llama.cpp compiles maxLength into the grammar, so
    the loop is cut at the cap and the JSON still closes. The value is only a
    ceiling; well-behaved output is unaffected.
    """
    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: walk(v) for k, v in node.items()}
            if out.get("type") == "string" and "enum" not in out and "maxLength" not in out:
                out["maxLength"] = max_length
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(schema)
