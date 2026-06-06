"""Internal helpers shared by trace recorders and artifact writers."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

_SENSITIVE_KEY_PARTS = ("api_key", "apikey", "authorization", "password", "secret", "token")
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.]{12,}\b", re.IGNORECASE),
)
_DEFAULT_MAX_FIELD_CHARS = 200_000
_MAX_FIELD_CHARS_ENV = "NETOPSBENCH_TRACE_MAX_FIELD_CHARS"


def jsonable(value: Any) -> Any:
    """Return a JSON-compatible, redacted, size-bounded representation."""

    return redact(coerce_jsonable(value))


def coerce_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): coerce_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [coerce_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        return coerce_jsonable(value.model_dump())
    if hasattr(value, "__dict__"):
        return coerce_jsonable({key: item for key, item in vars(value).items() if not str(key).startswith("_")})
    return str(value)


def redact(value: Any, key_hint: str = "") -> Any:
    if is_sensitive_key(key_hint):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(key): redact(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, key_hint) for item in value]
    if isinstance(value, str):
        return truncate_text(redact_secret_values(value))
    return value


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered.endswith("tokens") or lowered in {"input_tokens", "output_tokens", "total_tokens"}:
        return False
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def redact_secret_values(value: str) -> str:
    text = value
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub("<redacted>", text)
    return text


def truncate_text(value: str) -> str:
    limit = max_field_chars()
    if limit <= 0 or len(value) <= limit:
        return value
    return value[:limit] + f"...<truncated {len(value) - limit} chars>"


def max_field_chars() -> int:
    try:
        return int(os.environ.get(_MAX_FIELD_CHARS_ENV, str(_DEFAULT_MAX_FIELD_CHARS)))
    except ValueError:
        return _DEFAULT_MAX_FIELD_CHARS


__all__ = ["jsonable"]
