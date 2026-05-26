"""Pingmesh time-window helpers extracted from AgentToolkit."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any


def parse_iso8601_timestamp(value: str | None, field_name: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field_name} must be a non-empty ISO-8601 timestamp")
    normalized = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid ISO-8601 timestamp") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt


def resolve_pingmesh_time_scope(
    toolkit,
    time_range_minutes: int,
    start_time: str | None = None,
    end_time: str | None = None,
) -> dict[str, Any]:
    """Resolve Pingmesh queries to either an absolute window or a rolling lookback."""
    explicit_start = str(start_time or "").strip()
    explicit_end = str(end_time or "").strip()
    default_start = str(getattr(toolkit, "_pingmesh_default_start_time", "") or "").strip()
    default_end = str(getattr(toolkit, "_pingmesh_default_end_time", "") or "").strip()
    context_file = str(os.environ.get("NETOPSBENCH_PINGMESH_CONTEXT_FILE", "") or "").strip()
    env_start = str(os.environ.get(toolkit._PINGMESH_RANGE_ENV_START, "") or "").strip()
    env_end = str(os.environ.get(toolkit._PINGMESH_RANGE_ENV_END, "") or "").strip()

    def _absolute_scope(raw_start: str, raw_end: str, source: str, start_name: str, end_name: str) -> dict[str, Any]:
        if not (raw_start and raw_end):
            raise ValueError(f"{start_name} and {end_name} must be provided together")
        start_dt = parse_iso8601_timestamp(raw_start, start_name)
        end_dt = parse_iso8601_timestamp(raw_end, end_name)
        if start_dt >= end_dt:
            raise ValueError(f"{start_name} must be earlier than {end_name}")
        normalized_start = start_dt.isoformat().replace("+00:00", "Z")
        normalized_end = end_dt.isoformat().replace("+00:00", "Z")
        return {
            "mode": "absolute",
            "source": source,
            "start_time": normalized_start,
            "end_time": normalized_end,
            "range_clause": (
                f'  |> range(start: time(v: "{normalized_start}"), ' f'stop: time(v: "{normalized_end}"))\n'
            ),
        }

    if explicit_start or explicit_end:
        return _absolute_scope(explicit_start, explicit_end, "explicit", "start_time", "end_time")

    if default_start or default_end:
        return _absolute_scope(default_start, default_end, "toolkit_default", "default_start_time", "default_end_time")

    if context_file:
        try:
            with open(context_file, encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            payload = {}
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid NETOPSBENCH_PINGMESH_CONTEXT_FILE JSON: {context_file}") from exc
        if isinstance(payload, dict):
            file_start = str(payload.get("start_time") or "").strip()
            file_end = str(payload.get("end_time") or "").strip()
            if file_start or file_end:
                return _absolute_scope(file_start, file_end, "context_file", "start_time", "end_time")

    if env_start or env_end:
        return _absolute_scope(
            env_start,
            env_end,
            "env",
            toolkit._PINGMESH_RANGE_ENV_START,
            toolkit._PINGMESH_RANGE_ENV_END,
        )

    safe_minutes = max(1, min(int(time_range_minutes), 24 * 60))
    return {
        "mode": "rolling",
        "source": "time_range_minutes",
        "time_range_minutes": safe_minutes,
        "range_clause": f"  |> range(start: -{safe_minutes}m)\n",
    }
