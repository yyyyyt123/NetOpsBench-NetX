"""Shared serialization helpers for session traces."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def duration_seconds(started_at: Any, ended_at: Any) -> float | None:
    if not started_at or not ended_at:
        return None
    try:
        start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(ended_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return max(0.0, (end - start).total_seconds())


def safe_path_part(value: Any) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in str(value))
    return safe[:120] or "unknown"


def isoformat(value: datetime) -> str:
    timestamp = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC).isoformat()


def to_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str)
