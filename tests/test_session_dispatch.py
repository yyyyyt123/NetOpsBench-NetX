from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.platform.runtime.manager import RuntimePool
from netopsbench.platform.scenario.models import Episode, Scenario
from netopsbench.platform.session.context import build_worker_execution_context
from netopsbench.platform.session.dispatch import execute_on_runtime_pool
from netopsbench.platform.session.types import ScenarioExecutionRef, WorkerExecutionContext


class _FakeRunner:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.results_dir = Path(".")

    def run_scenario(self, scenario, diagnosis_callback=None):
        return {
            "success": True,
            "scenario_id": scenario.scenario_id,
            "episodes": [],
            "persist_results": self.kwargs.get("persist_results"),
        }


class _FakeEvaluator:
    _next_id = 0

    def __init__(self):
        type(self)._next_id += 1
        self.id = type(self)._next_id

    def generate_report(self, results, agent_name="unknown", topology_scale="unknown"):
        return {
            "summary": {
                "total_cases": len(results),
                "evaluator_id": self.id,
                "agent_name": agent_name,
                "topology_scale": topology_scale,
            },
            "detailed_results": [{"evaluator_id": item.evaluator_id} for item in results],
        }


def _scenario_ref(scenario_id: str) -> ScenarioExecutionRef:
    scenario = Scenario(
        scenario_id=scenario_id,
        name=scenario_id,
        description="test",
        topology_scale="xs",
        traffic_profile="standard",
        episodes=[
            Episode(
                episode_id=f"{scenario_id}-ep1",
                description="episode",
                fault_type="link_down",
                target_device="leaf1",
                target_interface="Ethernet1",
            )
        ],
    )
    return ScenarioExecutionRef.from_scenario(scenario)


def _worker(tmp_path: Path, index: int) -> RuntimeIdentity:
    topology_dir = tmp_path / f"worker-{index}"
    topology_dir.mkdir()
    return RuntimeIdentity.create(
        runtime_id="runtime-1",
        worker_id=f"worker-{index}",
        worker_index=index,
        lab_name=f"lab-{index}",
        topology_dir=topology_dir,
        mgmt_subnet=f"172.31.{index}.0/24",
        mgmt_network=f"clab-mgmt-lab-{index}",
        bucket=f"bucket-{index}",
    )


def test_worker_context_uses_lab_name_for_observability_topology_id(tmp_path):
    worker = _worker(tmp_path, 1)

    context = build_worker_execution_context(worker, Path(worker.topology_dir))

    assert context.topology_id == "lab-1"
    assert context.as_env()["NETOPSBENCH_TOPOLOGY_ID"] == "lab-1"


def test_worker_context_uses_explicit_identity_topology_id(tmp_path):
    worker = _worker(tmp_path, 1)
    worker = worker.model_copy(update={"topology_id": "runtime-observability-id"})

    context = build_worker_execution_context(worker, Path(worker.topology_dir))

    assert context.topology_id == "runtime-observability-id"


def test_worker_context_rejects_topology_directory_mismatch(tmp_path):
    worker = _worker(tmp_path, 1)

    with pytest.raises(ValueError, match="does not match runtime identity"):
        build_worker_execution_context(worker, tmp_path / "other")


def test_execute_on_runtime_pool_uses_per_worker_evaluators_and_session_raw_persistence(tmp_path, monkeypatch):
    import netopsbench.platform.session.dispatch as dispatch

    _FakeEvaluator._next_id = 0
    runtime = RuntimePool(
        id="runtime-1",
        name="runtime-1",
        scale="xs",
        root_dir=tmp_path / "runtime",
        workers=[_worker(tmp_path, 1), _worker(tmp_path, 2)],
    )
    scenarios = [_scenario_ref("scenario-1"), _scenario_ref("scenario-2")]

    def score_fault_episodes(_scenario, scenario_result, evaluator, **_kwargs):
        assert scenario_result["persist_results"] is False
        return [SimpleNamespace(score=1.0, evaluator_id=evaluator.id)]

    monkeypatch.setattr(dispatch, "ScenarioExecutor", _FakeRunner)
    monkeypatch.setattr(dispatch, "_create_evaluator", _FakeEvaluator)
    monkeypatch.setattr(dispatch, "score_scenario_fault_episodes", score_fault_episodes)
    monkeypatch.setattr(dispatch, "build_runtime_diagnosis_callback", lambda *_args: lambda _payload: {})
    monkeypatch.setattr(
        dispatch,
        "build_worker_execution_context",
        lambda worker, topology_dir: WorkerExecutionContext(
            topology_dir=topology_dir,
            topology_id=f"topo-{worker.worker_index}",
            influxdb_bucket=worker.bucket,
        ),
    )
    monkeypatch.setattr(dispatch, "load_topology_metadata", lambda _topology_dir: None)

    result = execute_on_runtime_pool(
        scenarios=scenarios,
        runtime=runtime,
        agent=SimpleNamespace(name="agent"),
        raw_dir=tmp_path / "artifacts" / "raw",
    )

    summaries = result.scenarios
    assert [summary["scenario_id"] for summary in summaries] == ["scenario-1", "scenario-2"]
    assert [Path(summary["raw_result_path"]).exists() for summary in summaries] == [True, True]
    assert {item.evaluator_id for item in result.evaluations} == {1, 2}
    assert [summary["worker_id"] for summary in result.workers] == ["worker-1", "worker-2"]
