import tempfile

from netopsbench.evaluator.scorer import AgentOutput, Evaluator
from netopsbench.platform.scenario.executor import ScenarioExecutor
from netopsbench.platform.scenario.models import Episode, Scenario
from netopsbench.platform.session.scoring import score_scenario_fault_episodes
from netopsbench.platform.topology.generator import generate_topology


def _metadata() -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        return generate_topology("xs", tmpdir)["metadata"]


def _none_episode(episode_id="ep001"):
    return Episode(
        episode_id=episode_id,
        description="healthy observation",
        fault_type="none",
        target_device="leaf1",
        duration_seconds=20,
        stabilization_time=5,
    )


def test_negative_sample_none_episode_observes_and_diagnoses(monkeypatch):
    runner = ScenarioExecutor(
        topology_metadata=_metadata(),
        skip_none_episodes=True,
        sleep_fn=lambda seconds: None,
        persist_results=False,
    )
    monkeypatch.setattr(runner, "_setup_traffic", lambda scale, profile: {"ok": True})
    monkeypatch.setattr(runner, "_stop_traffic", lambda: None)
    monkeypatch.setattr(runner, "_recover_fault", lambda: [])
    monkeypatch.setattr(
        runner,
        "_wait_and_observe",
        lambda duration, baseline_end_time=None: {
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-01-01T00:01:00Z",
            "duration_seconds": duration,
            "pingmesh_metrics": {"summary": {"total_anomalies": 0}, "anomalies": []},
            "anomalies_detected": False,
            "data_source_status": "ok",
        },
    )
    calls = []

    def diagnose(episode_result):
        calls.append(episode_result)
        return {"verdict": "network_healthy", "confidence": 1.0}

    scenario = Scenario(
        scenario_id="healthy_001",
        name="healthy",
        description="healthy",
        topology_scale="xs",
        traffic_profile="standard",
        metadata={"negative_sample": True},
        episodes=[_none_episode()],
    )

    result = runner.run_scenario(scenario, diagnosis_callback=diagnose)

    assert result["success"] is True
    assert len(calls) == 1
    episode_result = result["episodes"][0]
    assert episode_result["observations"]["data_source_status"] == "ok"
    assert episode_result["diagnosis"]["verdict"] == "network_healthy"


def test_regular_skipped_none_episode_does_not_observe_or_diagnose(monkeypatch):
    runner = ScenarioExecutor(
        topology_metadata=_metadata(),
        skip_none_episodes=True,
        sleep_fn=lambda seconds: None,
        persist_results=False,
    )
    monkeypatch.setattr(runner, "_wait_and_observe", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()))

    result = runner.run_episode(_none_episode(), diagnosis_callback=lambda _: {"verdict": "network_healthy"})

    assert result["success"] is True
    assert result["skip_reason"] == "fault_type_none_in_benchmark"
    assert "observations" not in result
    assert "diagnosis" not in result


def test_negative_sample_scenario_scores_network_healthy_as_correct():
    scenario = Scenario(
        scenario_id="healthy_001",
        name="healthy",
        description="healthy",
        topology_scale="xs",
        traffic_profile="standard",
        metadata={"negative_sample": True},
        episodes=[_none_episode()],
    )
    scenario_result = {
        "episodes": [
            {
                "episode": {"episode_id": "ep001", "fault_type": "none"},
                "diagnosis": {"verdict": "network_healthy", "confidence": 1.0},
            }
        ]
    }

    scored = score_scenario_fault_episodes(scenario, scenario_result, Evaluator())

    assert len(scored) == 1
    assert scored[0].correct_verdict is True
    assert scored[0].score == 1.0
    assert scored[0].details["negative_sample"] is True


def test_negative_sample_scenario_scores_fault_detected_as_false_positive():
    scenario = Scenario(
        scenario_id="healthy_001",
        name="healthy",
        description="healthy",
        topology_scale="xs",
        traffic_profile="standard",
        metadata={"negative_sample": True},
        episodes=[_none_episode()],
    )
    scenario_result = {
        "episodes": [
            {
                "episode": {"episode_id": "ep001", "fault_type": "none"},
                "diagnosis": {"verdict": "fault_detected", "fault_type": "link_down"},
            }
        ]
    }

    scored = score_scenario_fault_episodes(scenario, scenario_result, Evaluator())

    assert len(scored) == 1
    assert scored[0].correct_verdict is False
    assert scored[0].score == 0.0
    assert scored[0].details["false_positive"] is True


def test_report_localization_ignores_negative_samples():
    evaluator = Evaluator()
    positive = evaluator.evaluate(
        agent_output=AgentOutput(
            verdict="fault_detected",
            fault_type="link_down",
            location={"device": "wrong"},
        ),
        ground_truth={"fault_type": "link_down", "location": {"device": "leaf1"}},
        testcase_id="fault",
    )
    healthy = evaluator.evaluate(
        agent_output=AgentOutput(
            verdict="network_healthy",
        ),
        ground_truth={},
        testcase_id="healthy",
    )

    report = evaluator.generate_report([positive, healthy])

    assert report["summary"]["positive_sample_cases"] == 1
    assert report["summary"]["negative_sample_cases"] == 1
    assert report["summary"]["correct_device"] == 0
    assert report["summary"]["device_localization_rate"] == 0.0
