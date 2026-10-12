"""Short-lived, single-consumer handoff of a worker's verified healthy window.

This stores window identity, never telemetry rows. Each consumer queries InfluxDB
again so late arrivals remain visible. Ownership ends with the suite's executor.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from netopsbench.platform.utils.proc import docker_prefix, safe_run

MAX_WINDOW_AGE_SECONDS = 120
RECALIBRATE_SECONDS = 600


@dataclass
class BaselineHandoff:
    window: dict | None = None
    scope: str | None = None
    source: str | None = None
    epoch: float | None = None

    def clear(self) -> None:
        self.window = self.scope = self.source = self.epoch = None

    def fresh_epoch(self) -> None:
        self.clear()
        self.epoch = time.monotonic()

    def take(self, scope: str | None, minimum_seconds: int) -> tuple[dict | None, str]:
        window, previous_scope = self.window, self.scope
        self.window = None  # A failed or interrupted consumer cannot pass it on.
        if not scope or scope != previous_scope or window is None:
            return None, "missing_or_changed_scope"
        if self.epoch is None or not 0 <= time.monotonic() - self.epoch <= RECALIBRATE_SECONDS:
            return None, "recalibration_due"
        if not valid_window(window, minimum_seconds):
            return None, "expired_or_short_window"
        return dict(window), "verified_previous_case"

    def publish(self, window: dict, scope: str | None, source: str, minimum_seconds: int) -> bool:
        if not scope or self.epoch is None or not valid_window(window, minimum_seconds):
            self.window = None
            return False
        self.window, self.scope, self.source = dict(window), scope, source
        return True


def valid_window(window: dict, minimum_seconds: int) -> bool:
    try:
        start = datetime.fromisoformat(window["start_time"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(window["end_time"].replace("Z", "+00:00"))
        return (
            start.tzinfo is not None
            and end.tzinfo is not None
            and (end - start).total_seconds() >= minimum_seconds
            and int(window["duration_seconds"]) >= minimum_seconds
            and 0 <= (datetime.now(UTC) - end).total_seconds() <= MAX_WINDOW_AGE_SECONDS
        )
    except (KeyError, TypeError, ValueError):
        return False


def runtime_scope(runner, scenario) -> str | None:
    """Require unchanged runtime instances, generated configs and traffic plans."""
    worker = runner.runtime_worker
    controller = runner.traffic_controller
    if worker is None or controller is None or not controller.generation or not controller.plan_digests:
        return None
    digest = hashlib.sha256()
    root = Path(runner.topology_dir)
    paths = [root / "topology.json", *sorted((root / "configs").rglob("*"))]
    for path in paths:
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    manifest = runner.topology_metadata
    names = sorted(f"clab-{worker.lab_name}-{device['name']}" for device in manifest["devices"])
    inspected = safe_run(
        [
            *docker_prefix(),
            "docker",
            "inspect",
            "--format",
            "{{.Id}} {{.Image}} {{.State.StartedAt}} {{.State.Pid}} {{.State.Running}} {{.State.OOMKilled}}",
            *names,
        ],
        check=True,
        timeout=30,
    )
    rows = sorted(line.split() for line in inspected.stdout.splitlines() if line.strip())
    if len(rows) != len(names) or any(len(row) != 6 or row[4:] != ["true", "false"] for row in rows):
        return None
    digest.update(
        json.dumps(
            {
                "runtime": worker.runtime_id,
                "worker": worker.worker_id,
                "topology": runner.topology_id,
                "pingmesh_plan_id": runner.topology_metadata["pingmesh_plan"]["settings"]["plan_id"],
                "bucket": runner.influxdb_bucket,
                "scale": runner.scale_registry.get(scenario.scale).digest,
                "traffic_profile": scenario.traffic_profile,
                "generation": controller.generation,
                "plans": controller.plan_digests,
                "containers": rows,
            },
            sort_keys=True,
        ).encode()
    )
    return digest.hexdigest()
