"""Tests for the public scenario authoring API."""

import pytest

from netopsbench.platform.faults.specs import FaultSpec
from netopsbench.platform.scenario.models import Episode, Scenario
from netopsbench.platform.scenario.validator import validate_scenario


def test_scenario_manager_can_create_and_roundtrip_yaml(tmp_path):
    from netopsbench.sdk.scenarios import ScenarioManager

    manager = ScenarioManager(workspace=tmp_path)
    scenario = manager.create(
        id="scenario_x",
        name="Scenario X",
        description="desc",
        scale="small",
        traffic_profile="standard",
        episodes=[
            {
                "episode_id": "ep001",
                "description": "baseline",
                "fault_type": "none",
            },
            {
                "episode_id": "ep002",
                "description": "inject",
                "fault_type": "static_route_misconfig",
                "target_device": "leaf1",
                "parameters": {"target_ip": "auto", "wrong_nexthop": "auto"},
            },
        ],
        metadata={"difficulty": "medium", "expected_diagnosis": "static_route_misconfig"},
    )

    out = tmp_path / "scenario_x.yaml"
    saved_path = manager.save(scenario, out)
    loaded = manager.load(saved_path)

    assert saved_path == out
    assert loaded.id == "scenario_x"
    assert loaded.scale == "small"
    assert loaded.episodes[1]["fault_type"] == "static_route_misconfig"
    assert loaded.metadata["expected_diagnosis"] == "static_route_misconfig"


@pytest.mark.parametrize("profile", ["light", "stress"])
def test_scenario_manager_rejects_nonstandard_traffic_profile(tmp_path, profile):
    from netopsbench.sdk.scenarios import ScenarioManager

    manager = ScenarioManager(workspace=tmp_path)

    with pytest.raises(ValueError, match="Only the standard traffic profile is supported"):
        manager.create(id="legacy_profile", name="Legacy Profile", traffic_profile=profile)


def test_supported_scales_are_available_from_public_sdk():
    from netopsbench.sdk import supported_scales

    assert supported_scales() == ("xs", "small", "medium", "large", "xlarge", "fat-tree-k8", "fat-tree-k12")


def test_scenario_validation_uses_fault_registry(tmp_path):
    from netopsbench.sdk import NetOpsBench

    bench = NetOpsBench(workspace=str(tmp_path))
    bench.faults.register(
        spec=FaultSpec(name="public_registry_fault", required_parameters=("probe",)),
        executor=type(
            "Executor",
            (),
            {"inject": lambda self, context: {}, "recover": lambda self, context: {}},
        )(),
    )
    scenario = bench.scenarios.create(
        id="registry_case",
        name="Registry Case",
        description="desc",
        scale="xs",
        traffic_profile="standard",
        episodes=[
            {
                "episode_id": "ep001",
                "description": "fault",
                "fault_type": "public_registry_fault",
                "target_device": "leaf1",
                "parameters": {"probe": "icmp"},
            }
        ],
        metadata={"difficulty": "easy", "expected_diagnosis": "public_registry_fault"},
    )

    assert bench.scenarios.validate(scenario) == []


def test_validate_scenario_does_not_mutate_episode_fault_type():
    scenario = Scenario(
        scenario_id="alias_case",
        name="Alias Case",
        description="desc",
        topology_scale="xs",
        traffic_profile="standard",
        metadata={"difficulty": "easy", "expected_diagnosis": "static_route_misconfiguration"},
        episodes=[
            Episode(
                episode_id="ep001",
                description="fault",
                fault_type="static_route_misconfiguration",
                target_device="leaf1",
            )
        ],
    )

    original_fault_type = scenario.episodes[0].fault_type
    errors = validate_scenario(scenario)

    assert errors == []
    assert scenario.episodes[0].fault_type == original_fault_type


def test_scenario_handle_keeps_public_state_independent_from_internal_scenario_objects(tmp_path):
    from netopsbench.sdk.scenarios import ScenarioManager

    manager = ScenarioManager(workspace=tmp_path)
    handle = manager.create(
        id="public_state_case",
        name="Public State Case",
        description="desc",
        scale="xs",
        traffic_profile="standard",
        episodes=[
            {
                "episode_id": "ep001",
                "description": "baseline",
                "fault_type": "none",
            }
        ],
    )

    assert "scenario" not in vars(handle)

    internal = handle.to_scenario()
    internal.name = "Mutated Internal Name"
    internal.episodes[0].description = "mutated"

    assert handle.name == "Public State Case"
    assert handle.episodes[0]["description"] == "baseline"
