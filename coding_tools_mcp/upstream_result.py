from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .json_utils import strict_json_bytes


RESULT_INLINE_MAX = 128_000
_TRUNCATION_MARKER = "\n…[truncated]"
_BINARY_TYPES = frozenset({"image", "audio", "blob"})


@dataclass(frozen=True)
class _BudgetProfile:
    content_text_bytes: int
    structured_text_bytes: int
    max_content_blocks: int
    max_structured_items: int
    max_depth: int
    max_string_bytes: int
    max_unknown_block_bytes: int


_PROFILES = (
    _BudgetProfile(64_000, 40_000, 64, 512, 8, 8_192, 4_096),
    _BudgetProfile(32_000, 20_000, 32, 256, 6, 4_096, 2_048),
    _BudgetProfile(12_000, 8_000, 16, 128, 5, 2_048, 1_024),
    _BudgetProfile(2_048, 1_024, 8, 32, 4, 512, 256),
)


class _TextBudget:
    def __init__(self, remaining_bytes: int) -> None:
        self.remaining_bytes = max(0, remaining_bytes)

    def take(self, text: str, *, max_bytes: int | None = None) -> str:
        allowance = self.remaining_bytes
        if max_bytes is not None:
            allowance = min(allowance, max(0, max_bytes))
        truncated = _truncate_utf8(text, allowance)
        self.remaining_bytes = max(
            0,
            self.remaining_bytes - _text_budget_bytes(truncated),
        )
        return truncated


class _DeepBudget:
    def __init__(self, profile: _BudgetProfile) -> None:
        self.profile = profile
        self.remaining_items = profile.max_structured_items
        self.text = _TextBudget(profile.structured_text_bytes)


def result_json_bytes(value: Any) -> bytes:
    return strict_json_bytes(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def budget_tool_result(
    result: dict[str, Any],
    *,
    max_bytes: int = RESULT_INLINE_MAX,
    required_structured_content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a final MCP tools/call result within the UTF-8 envelope budget.

    ``required_structured_content`` is merged before every size check and is
    preserved by all truncation fallbacks. Broker result handles use this path
    so metadata injection cannot push an already-budgeted envelope over the
    hard limit.
    """

    required = dict(required_structured_content or {})
    candidate = _merge_required_structured_content(result, required) if required else result
    serialized = result_json_bytes(candidate)
    if len(serialized) <= max_bytes:
        return candidate

    original_bytes = len(result_json_bytes(result))
    for profile in _PROFILES:
        truncated = _build_truncated_result(
            result,
            original_bytes,
            profile,
            required_structured_content=required,
        )
        if len(result_json_bytes(truncated)) <= max_bytes:
            return truncated

    minimal_structured: dict[str, Any] = {
        "_truncated": True,
        "_original_bytes": original_bytes,
    }
    minimal_structured.update(required)
    minimal = {
        "content": [],
        "structuredContent": minimal_structured,
        "isError": bool(result.get("isError", False)),
    }
    if len(result_json_bytes(minimal)) <= max_bytes:
        return minimal

    if required:
        required_only = {
            "content": [],
            "structuredContent": required,
            "isError": bool(result.get("isError", False)),
        }
        if len(result_json_bytes(required_only)) <= max_bytes:
            return required_only
        raise ValueError("Required result metadata exceeds the final envelope budget.")

    # RESULT_INLINE_MAX is intentionally far above this compact envelope. This
    # branch keeps the helper total for callers that inject an unrealistically
    # tiny test budget.
    return {
        "content": [],
        "isError": bool(result.get("isError", False)),
    }


def _merge_required_structured_content(
    result: dict[str, Any],
    required: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(result)
    structured = result.get("structuredContent")
    merged_structured = dict(structured) if isinstance(structured, dict) else {}
    merged_structured.update(required)
    merged["structuredContent"] = merged_structured
    return merged


def _build_truncated_result(
    result: dict[str, Any],
    original_bytes: int,
    profile: _BudgetProfile,
    *,
    required_structured_content: dict[str, Any],
) -> dict[str, Any]:
    content = _truncate_content(result.get("content"), profile)
    structured = _truncate_structured_content(result.get("structuredContent"), profile)
    structured["_truncated"] = True
    structured["_original_bytes"] = original_bytes
    structured.update(required_structured_content)

    truncated: dict[str, Any] = {
        "content": content,
        "structuredContent": structured,
        "isError": bool(result.get("isError", False)),
    }
    if "_meta" in result:
        meta_budget = _DeepBudget(profile)
        truncated["_meta"] = _deep_limit(result["_meta"], meta_budget, depth=0)
    return truncated


def _truncate_content(value: Any, profile: _BudgetProfile) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    output: list[dict[str, Any]] = []
    text_budget = _TextBudget(profile.content_text_bytes)
    limit = min(len(value), profile.max_content_blocks)
    for block in value[:limit]:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            output.append(
                {
                    "type": "text",
                    "text": text_budget.take(text if isinstance(text, str) else ""),
                }
            )
            continue
        if block_type in _BINARY_TYPES:
            mime_type = block.get("mimeType")
            mime = mime_type if isinstance(mime_type, str) and mime_type else "unknown"
            output.append(
                {
                    "type": "text",
                    "text": f"[{block_type} content omitted — {mime}]",
                }
            )
            continue
        if block_type == "resource":
            output.append(_truncate_resource_block(block, text_budget, profile))
            continue

        if len(result_json_bytes(block)) <= profile.max_unknown_block_bytes:
            block_budget = _DeepBudget(profile)
            limited = _deep_limit(block, block_budget, depth=0)
            if isinstance(limited, dict):
                output.append(limited)
                continue
        label = block_type if isinstance(block_type, str) and block_type else "unknown"
        output.append(
            {
                "type": "text",
                "text": f"[{label} content omitted]",
            }
        )

    omitted = len(value) - limit
    if omitted > 0:
        output.append(
            {
                "type": "text",
                "text": f"[{omitted} additional content blocks omitted]",
            }
        )
    return output


def _truncate_resource_block(
    block: dict[str, Any],
    text_budget: _TextBudget,
    profile: _BudgetProfile,
) -> dict[str, Any]:
    resource = block.get("resource")
    if not isinstance(resource, dict):
        return {"type": "resource", "resource": {}}

    public_resource: dict[str, Any] = {}
    for key in ("uri", "name", "title", "mimeType"):
        value = resource.get(key)
        if isinstance(value, str):
            public_resource[key] = _truncate_utf8(value, profile.max_string_bytes)
    text = resource.get("text")
    if isinstance(text, str):
        public_resource["text"] = text_budget.take(
            text,
            max_bytes=profile.max_string_bytes,
        )
    return {
        "type": "resource",
        "resource": public_resource,
    }


def _truncate_structured_content(
    value: Any,
    profile: _BudgetProfile,
) -> dict[str, Any]:
    if value is None:
        return {}
    budget = _DeepBudget(profile)
    limited = _deep_limit(value, budget, depth=0)
    if isinstance(limited, dict):
        return limited
    return {"value": limited}


def _deep_limit(value: Any, budget: _DeepBudget, *, depth: int) -> Any:
    if depth >= budget.profile.max_depth:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return budget.text.take(value, max_bytes=budget.profile.max_string_bytes)
    if isinstance(value, list):
        output: list[Any] = []
        for item in value:
            if budget.remaining_items <= 0:
                output.append("[additional items omitted]")
                break
            budget.remaining_items -= 1
            output.append(_deep_limit(item, budget, depth=depth + 1))
        return output
    if isinstance(value, dict):
        output_dict: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if budget.remaining_items <= 0:
                output_dict["_omitted"] = True
                break
            budget.remaining_items -= 1
            key = _truncate_utf8(str(raw_key), 128)
            if not key or key in output_dict:
                continue
            output_dict[key] = _deep_limit(raw_value, budget, depth=depth + 1)
        return output_dict
    return f"[{type(value).__name__} omitted]"


def _truncate_utf8(text: str, max_bytes: int) -> str:
    if _text_budget_bytes(text) <= max_bytes:
        return text
    marker_bytes = _TRUNCATION_MARKER.encode("utf-8")
    if max_bytes <= len(marker_bytes):
        return marker_bytes[:max_bytes].decode("utf-8", errors="ignore")

    target = max_bytes - len(marker_bytes)
    low = 0
    high = len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if _text_budget_bytes(text[:middle]) <= target:
            low = middle
        else:
            high = middle - 1
    return text[:low] + _TRUNCATION_MARKER


def _text_budget_bytes(text: str) -> int:
    """Count UTF-8 bytes, representing unpaired surrogates as JSON escapes."""

    total = 0
    for character in text:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            total += 6  # ``\\ud800`` / ``\\udc00`` in ensure_ascii JSON.
        else:
            total += len(character.encode("utf-8"))
    return total
