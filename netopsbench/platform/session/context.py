"""Runtime diagnosis context helpers used by session execution."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

from netopsbench.logging_utils import get_logger
from netopsbench.models.runtime import RuntimeIdentity

# Internal platform code can call AgentToolkit directly; external integrations should use SDK MCP helpers.
from netopsbench.platform.session.types import WorkerExecutionContext
from netopsbench.platform.toolkit.toolkit import AgentToolkit
from netopsbench.platform.topology.topology_utils import load_topology_manifest

logger = get_logger(__name__)

_EPISODE_ALLOWED_KEYS = {
    "episode_id",
    "duration_seconds",
    "stabilization_time",
}
_MAX_AGENT_ANOMALIES = 100


def build_topology_snapshot(toolkit: AgentToolkit) -> dict:
    topology_result = toolkit.get_topology()
    if hasattr(topology_result, "success") and topology_result.success:
        return topology_result.data
    return {"devices": {}}


def _build_toolkit_for_topology(topology_dir: str) -> AgentToolkit:
    topology_path = Path(topology_dir).resolve()
    manifest = load_topology_manifest(topology_path)
    return AgentToolkit(topology_dir=topology_path, topology_metadata=manifest.model_dump(mode="json"))


def _extract_episode_pingmesh_query_window(episode_result: dict[str, Any]) -> dict[str, str | None]:
    observations = episode_result.get("observations", {}) if isinstance(episode_result, dict) else {}
    if not isinstance(observations, dict):
        return {"start_time": None, "end_time": None}
    start_time = observations.get("start_time")
    end_time = observations.get("end_time")
    if isinstance(start_time, str) and isinstance(end_time, str) and start_time and end_time:
        return {"start_time": start_time, "end_time": end_time}
    pingmesh_metrics = observations.get("pingmesh_metrics", {}) if isinstance(observations, dict) else {}
    windows = pingmesh_metrics.get("windows", {}) if isinstance(pingmesh_metrics, dict) else {}
    if isinstance(windows, dict) and windows:
        current_starts = []
        current_ends = []
        for window in windows.values():
            if not isinstance(window, dict):
                continue
            current = window.get("current", {})
            if not isinstance(current, dict):
                continue
            start_candidate = current.get("start")
            end_candidate = current.get("end")
            if isinstance(start_candidate, str) and start_candidate:
                current_starts.append(start_candidate)
            if isinstance(end_candidate, str) and end_candidate:
                current_ends.append(end_candidate)
        if current_starts and current_ends:
            return {"start_time": min(current_starts), "end_time": max(current_ends)}
    return {"start_time": None, "end_time": None}


def build_worker_execution_context(worker: RuntimeIdentity, topology_dir: Path) -> WorkerExecutionContext:
    """Build an execution context from the worker's canonical runtime identity."""
    resolved_topology_dir = Path(topology_dir)
    if resolved_topology_dir.resolve() != worker.topology_dir.resolve():
        raise ValueError(
            f"Worker topology directory {resolved_topology_dir} does not match runtime identity "
            f"{worker.topology_dir}"
        )
    return WorkerExecutionContext(
        topology_dir=resolved_topology_dir,
        topology_id=worker.topology_id,
        influxdb_bucket=worker.bucket,
    )


def _bounded_observations(observations: dict[str, Any]) -> dict[str, Any]:
    bounded = copy.deepcopy(observations)
    metrics = bounded.get("pingmesh_metrics")
    if not isinstance(metrics, dict):
        return bounded
    anomalies = metrics.get("anomalies")
    if not isinstance(anomalies, list) or len(anomalies) <= _MAX_AGENT_ANOMALIES:
        if isinstance(anomalies, list):
            metrics["returned_anomalies"] = len(anomalies)
            metrics["truncated"] = False
        return bounded

    severity_rank = {"high": 2, "medium": 1, "low": 0}
    persistence_rank = {"persistent": 3, "steady_only": 2, "early_only": 1, "full_window": 0}

    def rank(item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            -severity_rank.get(str(item.get("severity")), 0),
            -persistence_rank.get(str(item.get("persistence")), 0),
            -float(item.get("value", 0.0) or 0.0),
            str(item.get("type", "")),
            str(item.get("src_ip", "")),
            str(item.get("dst_ip", "")),
        )

    ordered = sorted((item for item in anomalies if isinstance(item, dict)), key=rank)
    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    represented: set[tuple[str, str, str]] = set()
    for item in ordered:
        identity = (str(item.get("type")), str(item.get("src_leaf")), str(item.get("dst_leaf")))
        if identity in represented:
            continue
        represented.add(identity)
        selected.append(item)
        selected_ids.add(id(item))
        if len(selected) == _MAX_AGENT_ANOMALIES:
            break
    if len(selected) < _MAX_AGENT_ANOMALIES:
        for item in ordered:
            if id(item) in selected_ids:
                continue
            selected.append(item)
            if len(selected) == _MAX_AGENT_ANOMALIES:
                break

    metrics["anomalies"] = selected
    metrics["returned_anomalies"] = len(selected)
    metrics["truncated"] = len(selected) < len(anomalies)
    return bounded


def build_public_case_id(*, scenario_id: str, episode_result: dict[str, Any]) -> str:
    """Return a stable, non-semantic case id for agent context."""
    episode = episode_result.get("episode", {}) if isinstance(episode_result, dict) else {}
    episode_id = episode.get("episode_id") if isinstance(episode, dict) else None
    source = f"{scenario_id}:{episode_id or 'unknown'}"
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]
    return f"case-{digest}"


def build_public_symptoms(*, episode_result: dict[str, Any], pingmesh_query_window: dict[str, Any]) -> dict[str, Any]:
    """Build the bounded symptom payload exposed to a diagnosis agent."""
    episode = episode_result.get("episode", {}) if isinstance(episode_result, dict) else {}
    observations = episode_result.get("observations", {}) if isinstance(episode_result, dict) else {}
    safe_episode = (
        {key: episode.get(key) for key in _EPISODE_ALLOWED_KEYS if key in episode} if isinstance(episode, dict) else {}
    )
    return {
        "episode": safe_episode,
        "observations": _bounded_observations(observations) if isinstance(observations, dict) else {},
        "pingmesh_query_window": pingmesh_query_window if isinstance(pingmesh_query_window, dict) else {},
        "observation_type": "scenario_episode",
    }


__all__ = [
    "build_public_case_id",
    "build_public_symptoms",
    "build_topology_snapshot",
    "build_worker_execution_context",
]
