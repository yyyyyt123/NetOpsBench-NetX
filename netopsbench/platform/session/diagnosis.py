"""Diagnostic callback helpers for runtime-backed session execution."""

from __future__ import annotations

import asyncio
import inspect
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from netopsbench.agents.base import DiagnosticContext
from netopsbench.agents.tracing import AgentTraceRecorder
from netopsbench.logging_utils import get_logger
from netopsbench.platform.session.context import (
    _build_toolkit_for_topology,
    _extract_episode_pingmesh_query_window,
    build_public_case_id,
    build_public_symptoms,
    build_topology_snapshot,
)
from netopsbench.platform.session.trace_store import TraceWriter
from netopsbench.platform.session.types import WorkerExecutionContext

logger = get_logger(__name__)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass
class AgentHandleAdapter:
    """Lightweight async wrapper for agents without a stable ``diagnose`` protocol."""

    agent: Any
    name: str = "agent"

    def __init__(self, agent: Any):
        self.agent = agent
        derived_name = getattr(agent, "name", None)
        if isinstance(derived_name, str) and derived_name.strip():
            self.name = derived_name.strip()
        else:
            self.name = getattr(agent, "__class__", type(agent)).__name__

    async def diagnose(self, context: DiagnosticContext):
        diagnose_method = getattr(self.agent, "diagnose", None)
        if not callable(diagnose_method):
            raise AttributeError(f"{self.agent.__class__.__name__} must define diagnose()")
        return await _maybe_await(diagnose_method(context))


def run_agent_diagnose(handle: Any, context: DiagnosticContext):
    """Execute a sync or async diagnosis handle from a synchronous session."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(handle.diagnose(context))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: asyncio.run(handle.diagnose(context)))
        return future.result()


def _strip_runtime_trace_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(metadata or {})
    for key in ("messages", "raw_messages", "trace_events", "trace", "trajectory", "conversation"):
        cleaned.pop(key, None)
    return cleaned


def build_runtime_diagnosis_callback(
    agent: Any,
    topology_dir: str,
    scenario_id: str,
    worker_context: WorkerExecutionContext | None = None,
    trace_writer: TraceWriter | None = None,
    worker_name: str | None = None,
    runtime_id: str | None = None,
    scenario_scale: str | None = None,
):
    """Build the episode callback that presents observations to one agent."""
    toolkit = _build_toolkit_for_topology(topology_dir)
    if worker_context is not None:
        toolkit.influxdb_bucket = worker_context.influxdb_bucket
        toolkit.topology_id = worker_context.topology_id
    handle = agent if isinstance(agent, AgentHandleAdapter) else AgentHandleAdapter(agent)
    context_dir = Path(topology_dir) / ".netopsbench"
    context_file = context_dir / "pingmesh_context.json"
    worker_env = worker_context.as_env() if worker_context is not None else {}
    worker_env["NETOPSBENCH_PINGMESH_CONTEXT_FILE"] = str(context_file)

    def callback(episode_result: dict) -> dict:
        start_time = datetime.now(UTC)
        trace_recorder = AgentTraceRecorder(enabled=trace_writer is not None)
        pingmesh_query_window = _extract_episode_pingmesh_query_window(episode_result)
        window_start = pingmesh_query_window.get("start_time")
        window_end = pingmesh_query_window.get("end_time")
        toolkit.set_pingmesh_time_window(window_start, window_end)
        if window_start and window_end:
            try:
                context_dir.mkdir(parents=True, exist_ok=True)
                context_file.write_text(
                    json.dumps({"start_time": window_start, "end_time": window_end}),
                    encoding="utf-8",
                )
            except OSError:
                logger.debug("failed to write pingmesh context file", exc_info=True)

        context = DiagnosticContext(
            scenario_id=build_public_case_id(scenario_id=scenario_id, episode_result=episode_result),
            topology=build_topology_snapshot(toolkit),
            symptoms=build_public_symptoms(
                episode_result=episode_result,
                pingmesh_query_window=pingmesh_query_window,
            ),
            ground_truth=None,
            tools=toolkit,
            trace=trace_recorder,
            metadata={"worker_env": worker_env} if worker_env else {},
        )
        try:
            diagnosis = run_agent_diagnose(handle, context)
        except Exception as exc:
            trace_recorder.record_error(stage="agent", error=exc)
            ended_at = datetime.now(UTC)
            diagnosis_payload: dict[str, Any] = {
                "error": str(exc),
                "success": False,
                "time_taken_seconds": max(0.0, (ended_at - start_time).total_seconds()),
                "metadata": {"agent_failure_stage": "diagnose", "error_type": type(exc).__name__},
            }
            if trace_writer is not None:
                try:
                    trace_result = trace_writer.write_case_trace(
                        case_id=context.scenario_id,
                        scenario_id=scenario_id,
                        episode_result=episode_result,
                        worker=worker_name or "worker",
                        topology_id=(worker_context.topology_id if worker_context is not None else None),
                        topology_scale=scenario_scale,
                        runtime_id=runtime_id or "",
                        agent=agent,
                        diagnostic_context=context,
                        diagnosis=SimpleNamespace(
                            agent_name=getattr(handle, "name", "agent"),
                            success=False,
                            findings={"error": str(exc)},
                            metadata=diagnosis_payload["metadata"],
                        ),
                        diagnosis_payload=diagnosis_payload,
                        started_at=start_time,
                        ended_at=ended_at,
                        pingmesh_window=pingmesh_query_window,
                        error=str(exc),
                        trace_recorder=trace_recorder,
                    )
                    diagnosis_payload["trace"] = {
                        "trace_id": trace_result.trace_id,
                        "case_id": trace_result.case_id,
                        "worker": trace_result.worker,
                        "atif_path": trace_result.atif_path,
                    }
                except Exception:
                    logger.debug("failed to persist failed agent runtime trace", exc_info=True)
            diagnosis_payload["metadata"] = _strip_runtime_trace_metadata(diagnosis_payload["metadata"])
            return diagnosis_payload

        findings = dict(diagnosis.findings or {})
        location = findings.get("location") or {}
        if not isinstance(location, dict):
            location = {}
        ended_at = datetime.now(UTC)
        metadata = dict(diagnosis.metadata or {})
        recorder_metrics = trace_recorder.metrics()
        for key in ("input_tokens", "output_tokens", "total_tokens", "llm_call_count"):
            if recorder_metrics.get(key):
                metadata[key] = recorder_metrics[key]
        recorded_tool_calls = trace_recorder.tool_calls()
        diagnosis_payload = {
            "verdict": diagnosis.verdict,
            "fault_type": findings.get("fault_type") or metadata.get("fault_type"),
            "location": {
                key: value
                for key, value in {
                    "device": location.get("device") or findings.get("device"),
                    "interface": location.get("interface") or findings.get("interface"),
                }.items()
                if value is not None
            },
            "evidence": list(findings.get("evidence") or []),
            "confidence": float(diagnosis.confidence or 0.0),
            "reasoning": diagnosis.reasoning,
            "tool_calls": recorded_tool_calls or list(metadata.get("tool_calls") or []),
            "time_taken_seconds": max(0.0, (ended_at - start_time).total_seconds()),
            "metadata": metadata,
        }
        if trace_writer is not None:
            try:
                trace_result = trace_writer.write_case_trace(
                    case_id=context.scenario_id,
                    scenario_id=scenario_id,
                    episode_result=episode_result,
                    worker=worker_name or "worker",
                    topology_id=(worker_context.topology_id if worker_context is not None else None),
                    topology_scale=scenario_scale,
                    runtime_id=runtime_id or "",
                    agent=agent,
                    diagnostic_context=context,
                    diagnosis=diagnosis,
                    diagnosis_payload=diagnosis_payload,
                    started_at=start_time,
                    ended_at=ended_at,
                    pingmesh_window=pingmesh_query_window,
                    trace_recorder=trace_recorder,
                )
                diagnosis_payload["trace"] = {
                    "trace_id": trace_result.trace_id,
                    "case_id": trace_result.case_id,
                    "worker": trace_result.worker,
                    "atif_path": trace_result.atif_path,
                }
            except Exception:
                logger.debug("failed to persist agent runtime trace", exc_info=True)
        diagnosis_payload["metadata"] = _strip_runtime_trace_metadata(diagnosis_payload["metadata"])
        return diagnosis_payload

    return callback


__all__ = ["AgentHandleAdapter", "build_runtime_diagnosis_callback", "run_agent_diagnose"]
