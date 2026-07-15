"""Worker-pool execution for SDK sessions."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from netopsbench.evaluator.fault_type_judge import create_judge_from_env
from netopsbench.evaluator.scorer import Evaluator
from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.platform.runtime.manager import RuntimePool
from netopsbench.platform.scenario.executor import ScenarioExecutor
from netopsbench.platform.session.context import build_worker_execution_context
from netopsbench.platform.session.diagnosis import build_runtime_diagnosis_callback
from netopsbench.platform.session.reporting import load_topology_metadata
from netopsbench.platform.session.scoring import score_scenario_fault_episodes
from netopsbench.platform.session.trace_store import TraceWriter
from netopsbench.platform.session.types import ScenarioExecutionRef, WorkerExecutionContext

logger = logging.getLogger(__name__)

WorkerRunResult = tuple[list[Any], list[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class PoolDispatchResult:
    evaluations: list[Any]
    scenarios: list[dict[str, Any]]
    workers: list[dict[str, Any]]


def assign_scenarios_to_workers(
    scenarios: list[ScenarioExecutionRef],
    workers: Sequence[RuntimeIdentity],
) -> dict[str, list[ScenarioExecutionRef]]:
    if not workers:
        raise ValueError("runtime pool must contain at least one worker")
    assignments: dict[str, list[ScenarioExecutionRef]] = {worker.worker_id: [] for worker in workers}
    for index, scenario in enumerate(scenarios):
        assignments[workers[index % len(workers)].worker_id].append(scenario)
    return assignments


def _dispatch_workers(
    workers: Sequence[RuntimeIdentity],
    scenarios: list[ScenarioExecutionRef],
    run_worker: Callable[[RuntimeIdentity, list[ScenarioExecutionRef]], WorkerRunResult],
) -> PoolDispatchResult:
    if not workers:
        raise ValueError("runtime must contain at least one worker")
    ordered_workers = sorted(workers, key=lambda item: item.worker_index)
    assignments = assign_scenarios_to_workers(scenarios, ordered_workers)

    if len(ordered_workers) == 1:
        worker = ordered_workers[0]
        results = {worker.worker_index: run_worker(worker, assignments[worker.worker_id])}
    else:
        logger.info("Executing %d workers in parallel", len(ordered_workers))
        results = {}
        with ThreadPoolExecutor(max_workers=len(ordered_workers)) as pool:
            futures = {
                pool.submit(run_worker, worker, assignments[worker.worker_id]): worker for worker in ordered_workers
            }
            for future in as_completed(futures):
                worker = futures[future]
                results[worker.worker_index] = future.result()

    evaluations: list[Any] = []
    scenario_summaries: list[dict[str, Any]] = []
    worker_summaries: list[dict[str, Any]] = []
    for worker in ordered_workers:
        worker_evaluations, worker_scenarios, worker_summary = results[worker.worker_index]
        evaluations.extend(worker_evaluations)
        scenario_summaries.extend(worker_scenarios)
        worker_summaries.append(worker_summary)
    return PoolDispatchResult(evaluations, scenario_summaries, worker_summaries)


def _build_scenario_executor(
    worker_context: WorkerExecutionContext,
    worker_raw_dir: Path,
    *,
    fault_registry: Any,
    baseline_wait_seconds: int,
    post_recovery_wait_seconds: int,
    skip_none_episodes: bool,
) -> ScenarioExecutor:
    runner = ScenarioExecutor(
        topology_dir=str(worker_context.topology_dir),
        topology_metadata=load_topology_metadata(worker_context.topology_dir),
        baseline_wait_seconds=baseline_wait_seconds,
        post_recovery_wait_seconds=post_recovery_wait_seconds,
        skip_none_episodes=skip_none_episodes,
        influxdb_bucket=worker_context.influxdb_bucket,
        topology_id=worker_context.topology_id,
        persist_results=False,
        fault_registry=fault_registry,
    )
    runner.results_dir = worker_raw_dir
    return runner


def _create_evaluator() -> Evaluator:
    judge = create_judge_from_env()
    return Evaluator(fault_type_judge=judge) if judge is not None else Evaluator()


def _run_worker(
    *,
    runtime: RuntimePool,
    agent: Any,
    raw_dir: Path,
    trace_writer: TraceWriter | None,
    fault_registry: Any,
    baseline_wait_seconds: int,
    post_recovery_wait_seconds: int,
    skip_none_episodes: bool,
    worker: RuntimeIdentity,
    scenarios: list[ScenarioExecutionRef],
) -> WorkerRunResult:
    worker_context = build_worker_execution_context(worker, worker.topology_dir)
    worker_raw_dir = raw_dir / worker.worker_id
    worker_raw_dir.mkdir(parents=True, exist_ok=True)
    runner = _build_scenario_executor(
        worker_context,
        worker_raw_dir,
        fault_registry=fault_registry,
        baseline_wait_seconds=baseline_wait_seconds,
        post_recovery_wait_seconds=post_recovery_wait_seconds,
        skip_none_episodes=skip_none_episodes,
    )
    evaluator = _create_evaluator()

    evaluations: list[Any] = []
    scenario_summaries: list[dict[str, Any]] = []
    worker_success = True

    for scenario in scenarios:
        callback = build_runtime_diagnosis_callback(
            agent,
            str(worker_context.topology_dir),
            scenario.id,
            worker_context,
            trace_writer,
            worker.worker_id,
            runtime.id,
            scenario.scale,
        )
        parsed_scenario = scenario.to_scenario()
        scenario_result = runner.run_scenario(parsed_scenario, diagnosis_callback=callback)
        raw_result_path = _persist_raw_scenario_result(worker_raw_dir, scenario.id, scenario_result)
        try:
            scored = score_scenario_fault_episodes(
                parsed_scenario,
                scenario_result,
                evaluator,
                topology_dir=str(worker_context.topology_dir),
            )
        except Exception as exc:
            if trace_writer is not None:
                try:
                    trace_writer.write_failure_result(
                        scenario_id=scenario.id,
                        scenario_result=scenario_result,
                        stage="evaluator",
                        error=exc,
                    )
                except Exception:
                    logger.debug("failed to persist evaluator failure trace result", exc_info=True)
            raise
        if trace_writer is not None:
            try:
                trace_writer.write_evaluation_results(
                    evaluation_results=scored,
                    scenario_result=scenario_result,
                )
            except Exception:
                logger.debug("failed to persist trace evaluation results", exc_info=True)
        evaluations.extend(scored)
        success = bool(scenario_result.get("success"))
        scenario_summaries.append(
            {
                "scenario_id": scenario.id,
                "status": "completed" if success else "failed",
                "scale": scenario.scale,
                "worker": worker.worker_id,
                "raw_result_path": raw_result_path,
            }
        )
        worker_success &= success

    worker_summary = {
        "worker_id": worker.worker_id,
        "worker_name": worker.worker_id,
        "lab_name": worker.lab_name,
        "scenario_count": len(scenarios),
        "executed_count": len(scenarios),
        "success": worker_success,
    }
    return evaluations, scenario_summaries, worker_summary


def _persist_raw_scenario_result(
    worker_raw_dir: Path,
    scenario_id: str,
    scenario_result: dict[str, Any],
) -> str:
    safe_id = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in str(scenario_id))
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    result_path = worker_raw_dir / f"{safe_id}_{timestamp}.json"
    result_path.write_text(json.dumps(scenario_result, indent=2, default=str), encoding="utf-8")
    scenario_result["result_file"] = str(result_path)
    return str(result_path)


def execute_on_runtime_pool(
    *,
    runtime: RuntimePool,
    scenarios: list[ScenarioExecutionRef],
    agent: Any,
    raw_dir: Path,
    trace_writer: TraceWriter | None = None,
    fault_registry: Any = None,
    baseline_wait_seconds: int = 60,
    post_recovery_wait_seconds: int = 2,
    skip_none_episodes: bool = True,
) -> PoolDispatchResult:
    """Run scenarios on an existing runtime and return ordered execution data."""
    return _dispatch_workers(
        runtime.workers,
        scenarios,
        lambda worker, assigned: _run_worker(
            runtime=runtime,
            agent=agent,
            raw_dir=raw_dir,
            trace_writer=trace_writer,
            fault_registry=fault_registry,
            baseline_wait_seconds=baseline_wait_seconds,
            post_recovery_wait_seconds=post_recovery_wait_seconds,
            skip_none_episodes=skip_none_episodes,
            worker=worker,
            scenarios=assigned,
        ),
    )


__all__ = ["PoolDispatchResult", "assign_scenarios_to_workers", "execute_on_runtime_pool"]
