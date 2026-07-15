"""Environment boundary for traffic runtime settings."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_TRAFFIC_PARALLELISM = 32
DEFAULT_SWITCH_PPS_LIMIT = 5000


def parse_parallelism(value: str | None, default: int = DEFAULT_TRAFFIC_PARALLELISM) -> int:
    try:
        return max(1, int(str(value or default).strip()))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class TrafficSettings:
    parallelism: int = DEFAULT_TRAFFIC_PARALLELISM
    switch_pps_limit: int | None = DEFAULT_SWITCH_PPS_LIMIT

    @classmethod
    def from_env(cls) -> TrafficSettings:
        return cls(
            parallelism=parse_parallelism(os.environ.get("NETOPSBENCH_TRAFFIC_PARALLELISM")),
        )


__all__ = [
    "DEFAULT_SWITCH_PPS_LIMIT",
    "DEFAULT_TRAFFIC_PARALLELISM",
    "TrafficSettings",
    "parse_parallelism",
]
