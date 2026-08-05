from __future__ import annotations

import json
import json.encoder as json_encoder
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Callable


MAX_JSON_INTEGER_DIGITS = 4_300


def _reject_nonfinite_constant(raw: str) -> Any:
    raise ValueError(f"Non-finite JSON number {raw!r} is not allowed.")


def _parse_finite_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"Non-finite JSON number {raw!r} is not allowed.")
    return value


def _parse_bounded_int(raw: str) -> int:
    digits = raw[1:] if raw.startswith("-") else raw
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError(
            f"JSON integer exceeds the {MAX_JSON_INTEGER_DIGITS}-digit limit."
        )
    # Decimal parses the already-bounded integer exactly without consulting
    # Python's process-global int_max_str_digits setting.
    return int(Decimal(raw))


def _bounded_int_text(value: int) -> str:
    """Return an exact JSON integer without consulting int_max_str_digits."""

    decimal_value = Decimal(value)
    if len(decimal_value.as_tuple().digits) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError(
            f"JSON integer exceeds the {MAX_JSON_INTEGER_DIGITS}-digit limit."
        )
    return format(decimal_value, "f")


class _StrictJSONEncoder(json.JSONEncoder):
    """JSON encoder with a project-owned integer limit and finite floats."""

    def default(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return dict(value.items())
        if isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray),
        ):
            return list(value)
        return super().default(value)

    def iterencode(self, value: Any, _one_shot: bool = False) -> Any:
        del _one_shot
        markers: dict[int, Any] | None = {} if self.check_circular else None
        encoder = (
            json_encoder.encode_basestring_ascii
            if self.ensure_ascii
            else json_encoder.encode_basestring
        )

        def float_text(number: float) -> str:
            if math.isfinite(number):
                return float.__repr__(number)
            if not self.allow_nan:
                raise ValueError(
                    "Out of range float values are not JSON compliant: "
                    + repr(number)
                )
            if math.isnan(number):
                return "NaN"
            return "Infinity" if number == math.inf else "-Infinity"

        indent = self.indent
        if indent is not None and not isinstance(indent, str):
            indent = " " * indent
        make_iterencode: Callable[..., Any] = getattr(json_encoder, "_make_iterencode")
        iterator = make_iterencode(
            markers,
            self.default,
            encoder,
            indent,
            float_text,
            self.key_separator,
            self.item_separator,
            self.sort_keys,
            self.skipkeys,
            False,
            _intstr=_bounded_int_text,
        )
        return iterator(value, 0)


def strict_json_dumps(
    value: Any,
    *,
    ensure_ascii: bool = False,
    sort_keys: bool = False,
    separators: tuple[str, str] = (",", ":"),
    allow_nan: bool = False,
) -> str:
    """Encode standard JSON independently of Python's global integer limit."""

    return json.dumps(
        value,
        cls=_StrictJSONEncoder,
        ensure_ascii=ensure_ascii,
        sort_keys=sort_keys,
        separators=separators,
        allow_nan=allow_nan,
    )


def strict_json_bytes(
    value: Any,
    *,
    ensure_ascii: bool = False,
    sort_keys: bool = False,
    separators: tuple[str, str] = (",", ":"),
    allow_nan: bool = False,
) -> bytes:
    """Encode JSON as UTF-8, escaping unpaired surrogates when necessary."""

    serialized = strict_json_dumps(
        value,
        ensure_ascii=ensure_ascii,
        sort_keys=sort_keys,
        separators=separators,
        allow_nan=allow_nan,
    )
    encoding = "ascii" if ensure_ascii else "utf-8"
    try:
        return serialized.encode(encoding)
    except UnicodeEncodeError:
        return strict_json_dumps(
            value,
            ensure_ascii=True,
            sort_keys=sort_keys,
            separators=separators,
            allow_nan=allow_nan,
        ).encode("ascii")


def strict_json_loads(
    value: str | bytes | bytearray,
    *,
    object_pairs_hook: Callable[[list[tuple[str, Any]]], Any] | None = None,
) -> Any:
    """Decode standard JSON with bounded integers and finite floating-point values."""

    if isinstance(value, (bytes, bytearray)):
        # MCP and open-system JSON interchange are UTF-8 only.  Passing bytes
        # directly to json.loads() would also accept UTF-16 and UTF-32.
        value = bytes(value).decode("utf-8")

    try:
        return json.loads(
            value,
            parse_constant=_reject_nonfinite_constant,
            parse_float=_parse_finite_float,
            parse_int=_parse_bounded_int,
            object_pairs_hook=object_pairs_hook,
        )
    except RecursionError as exc:
        raise ValueError("JSON nesting exceeds the supported limit.") from exc
