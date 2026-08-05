from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from .json_utils import strict_json_bytes


MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_CHARS = 400
MAX_SCHEMA_DESCRIPTION_CHARS = 200
MAX_SCHEMA_PROPERTIES = 40
MAX_SCHEMA_ENUM_ITEMS = 50
MAX_SCHEMA_BRANCHES = 10
MAX_SCHEMA_DEPTH = 8
MAX_DEFINITION_BYTES = 8_192
MAX_VALUE_DEPTH = 4
MAX_VALUE_STRING_CHARS = 500
MAX_VALUE_ARRAY_ITEMS = 20
MAX_VALUE_DICT_ITEMS = 20
MAX_VALUE_KEY_CHARS = 100

CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_JSON_SCHEMA_TYPES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)
_ANNOTATION_KEYS = frozenset(
    {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
)
_LOOSE_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}
_REF_DEGRADED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "description": "Schema reference omitted by gateway normalization.",
}
_REF_DEGRADED_ANY_SCHEMA: dict[str, Any] = {
    "description": "Schema reference omitted by gateway normalization.",
}
_UNSUPPORTED = object()


def sanitize_text(raw: str, *, max_chars: int) -> str:
    text = CONTROL_CHAR_RE.sub("", raw)
    text = re.sub(r"\s+", " ", text.strip())
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


def sanitize_definition(raw: dict[str, Any]) -> dict[str, Any]:
    """Build a bounded public tool definition from untrusted metadata.

    This is metadata containment, not a claim that prompt injection is solved.
    The returned object is always JSON serializable and strictly smaller than
    ``MAX_DEFINITION_BYTES`` when compactly encoded as UTF-8.
    """

    result: dict[str, Any] = {}

    name = raw.get("name")
    if isinstance(name, str):
        result["name"] = name

    title = raw.get("title")
    if isinstance(title, str):
        result["title"] = sanitize_text(title, max_chars=MAX_TITLE_CHARS)

    description = raw.get("description")
    if isinstance(description, str):
        result["description"] = sanitize_text(
            description,
            max_chars=MAX_DESCRIPTION_CHARS,
        )

    input_schema = raw.get("inputSchema")
    if isinstance(input_schema, dict):
        sanitized_input = _sanitize_schema(
            input_schema,
            depth=0,
            top_level_input=True,
        )
        result["inputSchema"] = _ensure_object_input_schema(sanitized_input)
    else:
        result["inputSchema"] = dict(_LOOSE_OBJECT_SCHEMA)

    output_schema = raw.get("outputSchema")
    if isinstance(output_schema, dict):
        result["outputSchema"] = _sanitize_schema(output_schema, depth=0)

    annotations = raw.get("annotations")
    if isinstance(annotations, dict):
        public_annotations = {
            key: value
            for key, value in annotations.items()
            if key in _ANNOTATION_KEYS and isinstance(value, bool)
        }
        if public_annotations:
            result["annotations"] = public_annotations

    if _definition_size(result) < MAX_DEFINITION_BYTES:
        return result

    result.pop("outputSchema", None)
    if _definition_size(result) < MAX_DEFINITION_BYTES:
        return result

    input_schema = result.get("inputSchema")
    if isinstance(input_schema, dict):
        _strip_schema_descriptions(input_schema)
    result.pop("title", None)
    top_description = result.get("description")
    if isinstance(top_description, str):
        result["description"] = sanitize_text(top_description, max_chars=100)
    if _definition_size(result) < MAX_DEFINITION_BYTES:
        return result

    fallback: dict[str, Any] = {
        "inputSchema": dict(_LOOSE_OBJECT_SCHEMA),
    }
    if isinstance(name, str):
        fallback["name"] = name
    if isinstance(top_description, str):
        fallback["description"] = sanitize_text(top_description, max_chars=100)
    if _definition_size(fallback) < MAX_DEFINITION_BYTES:
        return fallback

    minimal: dict[str, Any] = {"inputSchema": dict(_LOOSE_OBJECT_SCHEMA)}
    if isinstance(name, str):
        minimal["name"] = name[:512]
    return minimal


def schema_digest(definition: dict[str, Any]) -> str:
    schema = definition.get("inputSchema")
    if not isinstance(schema, dict):
        schema = _LOOSE_OBJECT_SCHEMA
    canonical = _canonical_json(schema)
    return hashlib.sha256(canonical).hexdigest()[:32]


def raw_schema_digest(definition: dict[str, Any]) -> str | None:
    schema = definition.get("inputSchema")
    if not isinstance(schema, dict):
        return None
    try:
        canonical = _canonical_json(schema)
    except (RecursionError, TypeError, ValueError):
        return None
    return hashlib.sha256(canonical).hexdigest()[:32]


def _sanitize_schema(
    schema: dict[str, Any],
    *,
    depth: int,
    top_level_input: bool = False,
) -> dict[str, Any]:
    if depth >= MAX_SCHEMA_DEPTH:
        return {}
    if "$ref" in schema:
        return dict(
            _REF_DEGRADED_SCHEMA
            if top_level_input and depth == 0
            else _REF_DEGRADED_ANY_SCHEMA
        )

    result: dict[str, Any] = {}

    schema_type = _sanitize_schema_type(schema.get("type"))
    if schema_type is not None:
        result["type"] = schema_type

    title = schema.get("title")
    if isinstance(title, str):
        result["title"] = sanitize_text(title, max_chars=MAX_TITLE_CHARS)

    description = schema.get("description")
    if isinstance(description, str):
        result["description"] = sanitize_text(
            description,
            max_chars=MAX_SCHEMA_DESCRIPTION_CHARS,
        )

    properties = schema.get("properties")
    properties_incomplete = False
    if isinstance(properties, dict):
        public_properties: dict[str, Any] = {}
        properties_incomplete = len(properties) > MAX_SCHEMA_PROPERTIES
        for raw_name, raw_property in list(properties.items())[:MAX_SCHEMA_PROPERTIES]:
            if not isinstance(raw_name, str):
                properties_incomplete = True
                continue
            name = _sanitize_key(raw_name)
            if not name or name != raw_name or name in public_properties:
                properties_incomplete = True
                continue
            public_properties[name] = (
                _sanitize_schema(raw_property, depth=depth + 1)
                if isinstance(raw_property, dict)
                else {}
            )
        result["properties"] = public_properties

    required = schema.get("required")
    if isinstance(required, list):
        public_required: list[str] = []
        for item in required:
            if not isinstance(item, str):
                continue
            name = _sanitize_key(item)
            if name and name not in public_required:
                public_required.append(name)
        if "properties" in result:
            allowed = set(result["properties"])
            public_required = [name for name in public_required if name in allowed]
        result["required"] = public_required[:MAX_SCHEMA_PROPERTIES]

    items = schema.get("items")
    if isinstance(items, dict):
        result["items"] = _sanitize_schema(items, depth=depth + 1)

    # Once a declared property is omitted by containment limits or key
    # sanitization, retaining a restrictive additionalProperties rule could
    # reject an argument that the original schema explicitly allowed.
    if not properties_incomplete:
        additional = schema.get("additionalProperties")
        if isinstance(additional, bool):
            result["additionalProperties"] = additional
        elif isinstance(additional, dict):
            result["additionalProperties"] = _sanitize_schema(additional, depth=depth + 1)

    negated = schema.get("not")
    if isinstance(negated, dict):
        public_negated = _sanitize_schema(negated, depth=depth + 1)
        if (
            _schema_has_assertion(public_negated)
            and _schema_constraints_preserved(negated, public_negated)
        ):
            result["not"] = public_negated

    enum = schema.get("enum")
    if isinstance(enum, list) and len(enum) <= MAX_SCHEMA_ENUM_ITEMS:
        public_enum = []
        enum_complete = True
        for item in enum:
            sanitized = _sanitize_value(item, depth=0)
            if sanitized is _UNSUPPORTED or not _sanitized_value_is_exact(item, sanitized):
                enum_complete = False
                break
            public_enum.append(sanitized)
        if enum_complete:
            result["enum"] = public_enum

    for key in ("oneOf", "anyOf", "allOf"):
        branches = schema.get(key)
        if not isinstance(branches, list):
            continue
        if key in {"oneOf", "anyOf"} and len(branches) > MAX_SCHEMA_BRANCHES:
            continue
        public_branches: list[dict[str, Any]] = []
        has_unknown_branch = False
        for branch in branches[:MAX_SCHEMA_BRANCHES]:
            if not isinstance(branch, dict):
                has_unknown_branch = True
                if key == "allOf":
                    continue
                break
            public_branch = _sanitize_schema(branch, depth=depth + 1)
            branch_is_unknown = not _schema_has_assertion(public_branch)
            if key == "oneOf" and not _schema_constraints_preserved(
                branch, public_branch
            ):
                branch_is_unknown = True
            if branch_is_unknown:
                has_unknown_branch = True
                if key == "allOf":
                    continue
                break
            public_branches.append(public_branch)
        if key == "allOf":
            if public_branches:
                result[key] = public_branches
        elif not has_unknown_branch and public_branches:
            result[key] = public_branches

    for key in ("default", "const"):
        if key not in schema:
            continue
        sanitized = _sanitize_value(schema[key], depth=0)
        if sanitized is not _UNSUPPORTED and (
            key != "const" or _sanitized_value_is_exact(schema[key], sanitized)
        ):
            result[key] = sanitized

    examples = schema.get("examples")
    if isinstance(examples, list):
        public_examples = []
        for example in examples[:5]:
            sanitized = _sanitize_value(example, depth=0)
            if sanitized is not _UNSUPPORTED:
                public_examples.append(sanitized)
        result["examples"] = public_examples

    for key in ("minimum", "maximum"):
        value = schema.get(key)
        if _is_finite_number(value):
            result[key] = value

    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        value = schema.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[key] = value

    unique_items = schema.get("uniqueItems")
    if isinstance(unique_items, bool):
        result["uniqueItems"] = unique_items

    schema_format = schema.get("format")
    if isinstance(schema_format, str):
        result["format"] = sanitize_text(schema_format, max_chars=100)

    return result


_SCHEMA_ASSERTION_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "items",
        "additionalProperties",
        "not",
        "enum",
        "const",
        "oneOf",
        "anyOf",
        "allOf",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
    }
)


def _schema_has_assertion(schema: dict[str, Any]) -> bool:
    return any(key in schema for key in _SCHEMA_ASSERTION_KEYS)


_SCHEMA_NON_ASSERTION_KEYS = frozenset(
    {
        "$comment",
        "$defs",
        "$id",
        "$schema",
        "default",
        "definitions",
        "deprecated",
        "description",
        "examples",
        "readOnly",
        "title",
        "writeOnly",
    }
)


def _schema_constraints_preserved(
    original: dict[str, Any],
    sanitized: dict[str, Any],
) -> bool:
    """Check exact assertion preservation for non-monotonic combinators."""

    try:
        return _canonical_json(_schema_validation_projection(original)) == _canonical_json(
            _schema_validation_projection(sanitized)
        )
    except (RecursionError, TypeError, ValueError):
        return False


def _schema_validation_projection(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_SCHEMA_DEPTH:
        raise ValueError("Schema assertion projection exceeded the containment depth.")
    if isinstance(value, dict):
        return {
            key: _schema_validation_projection(item, depth=depth + 1)
            for key, item in value.items()
            if key not in _SCHEMA_NON_ASSERTION_KEYS and not key.startswith("x-")
        }
    if isinstance(value, list):
        return [
            _schema_validation_projection(item, depth=depth + 1)
            for item in value
        ]
    return value


def _sanitize_schema_type(value: Any) -> str | list[str] | None:
    if isinstance(value, str):
        return value if value in _JSON_SCHEMA_TYPES else None
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            if isinstance(item, str) and item in _JSON_SCHEMA_TYPES and item not in result:
                result.append(item)
        return result or None
    return None


def _sanitize_value(value: Any, *, depth: int) -> Any:
    if depth >= MAX_VALUE_DEPTH:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _UNSUPPORTED
    if isinstance(value, str):
        return sanitize_text(value, max_chars=MAX_VALUE_STRING_CHARS)
    if isinstance(value, list):
        list_result: list[Any] = []
        for item in value[:MAX_VALUE_ARRAY_ITEMS]:
            sanitized = _sanitize_value(item, depth=depth + 1)
            if sanitized is not _UNSUPPORTED:
                list_result.append(sanitized)
        return list_result
    if isinstance(value, dict):
        dict_result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:MAX_VALUE_DICT_ITEMS]:
            key = _sanitize_key(str(raw_key))
            if not key or key in dict_result:
                continue
            sanitized = _sanitize_value(raw_value, depth=depth + 1)
            if sanitized is not _UNSUPPORTED:
                dict_result[key] = sanitized
        return dict_result
    return _UNSUPPORTED


def _sanitized_value_is_exact(original: Any, sanitized: Any) -> bool:
    """Return whether containment preserved a JSON value without mutation."""

    try:
        return _canonical_json(original) == _canonical_json(sanitized)
    except (TypeError, ValueError):
        return False


def _ensure_object_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    schema_type = schema.get("type")
    if schema_type is None:
        if not schema:
            return dict(_LOOSE_OBJECT_SCHEMA)
        result = dict(schema)
        result["type"] = "object"
        return result
    if schema_type == "object":
        return schema
    if isinstance(schema_type, list) and "object" in schema_type:
        return schema
    return dict(_LOOSE_OBJECT_SCHEMA)


def _strip_schema_descriptions(schema: dict[str, Any]) -> None:
    schema.pop("description", None)
    schema.pop("title", None)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for value in properties.values():
            if isinstance(value, dict):
                _strip_schema_descriptions(value)
    for key in ("items", "additionalProperties", "not"):
        value = schema.get(key)
        if isinstance(value, dict):
            _strip_schema_descriptions(value)
    for key in ("oneOf", "anyOf", "allOf"):
        values = schema.get(key)
        if isinstance(values, list):
            for value in values:
                if isinstance(value, dict):
                    _strip_schema_descriptions(value)


def _sanitize_key(raw: str) -> str:
    text = CONTROL_CHAR_RE.sub("", raw)
    return text[:MAX_VALUE_KEY_CHARS]


def _definition_size(definition: dict[str, Any]) -> int:
    return len(_canonical_json(definition))


def _canonical_json(value: Any) -> bytes:
    return strict_json_bytes(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (not isinstance(value, float) or math.isfinite(value))
    )
