"""Narrow tolerance for JSON-stringified structured tool parameters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import copy
import json
from typing import Any


_DECODED_TYPES = {
    "array": list,
    "object": dict,
    "boolean": bool,
}
_INVALID_JSON = object()


def _json_type(value: object) -> str | None:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return None


def _declared_types(schema: Mapping[str, Any]) -> set[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return {item for item in declared if isinstance(item, str)}
    return set()


def _branch_compatible(value: object, schema: Mapping[str, Any]) -> bool:
    """Reject only branch facts that are already unambiguous.

    Required or otherwise invalid fields deliberately do not reject a branch:
    normalization runs before the original validator and must leave it to report
    incomplete values.  Const/enum fields are safe discriminators for operation
    variants and similar unions.
    """

    kind = _json_type(value)
    declared = _declared_types(schema)
    if declared and kind not in declared and not (kind == "integer" and "number" in declared):
        return False
    if "const" in schema and value != schema["const"]:
        return False
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return False
    if isinstance(value, Mapping):
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            for key, child in properties.items():
                if key not in value or not isinstance(child, Mapping):
                    continue
                child_value = value[key]
                if "const" in child and child_value != child["const"]:
                    return False
                child_enum = child.get("enum")
                if isinstance(child_enum, list) and child_value not in child_enum:
                    return False
    return True


def _selected_branches(value: object, choices: object) -> list[Mapping[str, Any]]:
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes, bytearray)):
        return []
    compatible = [choice for choice in choices
                  if isinstance(choice, Mapping) and _branch_compatible(value, choice)]
    if not isinstance(value, Mapping):
        return compatible

    # Required keys distinguish unions such as {plan} versus {operations}.
    # If the value is incomplete, keep all compatible branches so validation,
    # rather than this tolerance layer, remains authoritative.
    satisfied = []
    for choice in compatible:
        required = choice.get("required")
        if isinstance(required, list) and all(isinstance(key, str) and key in value for key in required):
            satisfied.append(choice)
    return satisfied or compatible


def _schema_allows(schema: Mapping[str, Any], kind: str, value: object) -> bool:
    if kind in _declared_types(schema):
        return True
    for keyword in ("oneOf", "anyOf"):
        for branch in _selected_branches(value, schema.get(keyword)):
            if _schema_allows(branch, kind, value):
                return True
    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        return any(isinstance(branch, Mapping) and _schema_allows(branch, kind, value)
                   for branch in all_of)
    return False


def _schema_may_decode(schema: Mapping[str, Any]) -> bool:
    if _declared_types(schema) & _DECODED_TYPES.keys():
        return True
    for keyword in ("oneOf", "anyOf", "allOf"):
        choices = schema.get(keyword)
        if isinstance(choices, list) and any(
            isinstance(branch, Mapping) and _schema_may_decode(branch)
            for branch in choices
        ):
            return True
    return False


def _normalize_children(
    value: object,
    schema: Mapping[str, Any],
    decoded_cache: dict[tuple[tuple[object, ...], str], object],
    path: tuple[object, ...],
) -> object:
    if isinstance(value, Mapping):
        result = {key: copy.deepcopy(child) for key, child in value.items()}
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            for key, child_schema in properties.items():
                if key in result and isinstance(child_schema, Mapping):
                    result[key] = _normalize(
                        result[key], child_schema, decoded_cache, (*path, key)
                    )
        return result
    if isinstance(value, list):
        prefix_items = schema.get("prefixItems")
        items = schema.get("items")
        if isinstance(prefix_items, list):
            return [
                _normalize(
                    child, prefix_items[index], decoded_cache, (*path, index)
                )
                if index < len(prefix_items) and isinstance(prefix_items[index], Mapping)
                else _normalize(child, items, decoded_cache, (*path, index))
                if index >= len(prefix_items) and isinstance(items, Mapping)
                else copy.deepcopy(child)
                for index, child in enumerate(value)
            ]
        if isinstance(items, Mapping):
            return [
                _normalize(child, items, decoded_cache, (*path, index))
                for index, child in enumerate(value)
            ]
        if isinstance(items, list):
            return [
                _normalize(child, items[index], decoded_cache, (*path, index))
                if index < len(items) and isinstance(items[index], Mapping)
                else copy.deepcopy(child)
                for index, child in enumerate(value)
            ]
    return copy.deepcopy(value)


def _normalize_branches(
    value: object,
    schema: Mapping[str, Any],
    decoded_cache: dict[tuple[tuple[object, ...], str], object],
    path: tuple[object, ...],
) -> object:
    result = value
    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for branch in all_of:
            if isinstance(branch, Mapping) and _branch_compatible(result, branch):
                result = _normalize(result, branch, decoded_cache, path)

    for keyword in ("oneOf", "anyOf"):
        branches = _selected_branches(result, schema.get(keyword))
        if not branches:
            continue
        candidates = [
            _normalize(result, branch, decoded_cache, path) for branch in branches
        ]
        first = candidates[0]
        # An unresolved union may describe different fields in each branch.
        # Only keep a mutation shared by every still-possible branch.
        if all(candidate == first for candidate in candidates[1:]):
            result = first
    return result


def _normalize(
    value: object,
    schema: Mapping[str, Any],
    decoded_cache: dict[tuple[tuple[object, ...], str], object],
    path: tuple[object, ...],
) -> object:
    current = copy.deepcopy(value)
    if isinstance(current, str) and _schema_may_decode(schema):
        cache_key = (path, current)
        if cache_key not in decoded_cache:
            try:
                decoded_cache[cache_key] = json.loads(current)
            except (json.JSONDecodeError, TypeError):
                decoded_cache[cache_key] = _INVALID_JSON
        decoded = decoded_cache[cache_key]
        kind = _json_type(decoded)
        if kind in _DECODED_TYPES and isinstance(decoded, _DECODED_TYPES[kind]) \
                and _schema_allows(schema, kind, decoded):
            current = decoded

    current = _normalize_children(current, schema, decoded_cache, path)
    return _normalize_branches(current, schema, decoded_cache, path)


def normalize_stringified_parameters(
    arguments: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> dict[str, Any]:
    """Decode structured JSON strings exactly where the tool schema declares them.

    Object, array and boolean fields are decoded once at their own node.  The
    decoded container is then traversed so independently stringified, explicitly
    declared children receive the same treatment.  Strings, numbers, nulls and
    undeclared fields are never guessed.  Invalid JSON stays unchanged for the
    caller's existing schema or business validation to reject.
    """

    normalized = _normalize(arguments, schema, {}, ())
    if not isinstance(normalized, dict):
        # The public contract accepts an argument object.  Keep failure handling
        # at the existing caller boundary rather than inventing a new exception.
        return dict(arguments)
    return normalized
