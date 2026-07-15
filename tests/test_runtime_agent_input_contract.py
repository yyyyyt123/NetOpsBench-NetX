from netopsbench.platform.session.context import (
    build_public_case_id,
    build_public_symptoms,
)


def test_build_public_symptoms_strips_fault_injection_labels():
    episode_result = {
        "episode": {
            "episode_id": "ep002_fault",
            "fault_type": "link_down",
            "target_device": "leaf1",
            "target_interface": "Ethernet8",
            "duration_seconds": 30,
            "stabilization_time": 5,
        },
        "observations": {"pingmesh_metrics": {"summary": {"total_anomalies": 3}}},
    }

    payload = build_public_symptoms(
        episode_result=episode_result,
        pingmesh_query_window={"start_time": "2026-01-01T00:00:00Z", "end_time": "2026-01-01T00:01:00Z"},
    )

    episode = payload["episode"]
    assert episode["episode_id"] == "ep002_fault"
    assert episode["duration_seconds"] == 30
    assert episode["stabilization_time"] == 5
    assert "fault_type" not in episode
    assert "target_device" not in episode
    assert "target_interface" not in episode


def test_build_public_case_id_is_non_semantic_and_stable():
    episode_result = {"episode": {"episode_id": "ep002_fault"}}

    case_a = build_public_case_id(scenario_id="generated_link_down_xs_001", episode_result=episode_result)
    case_b = build_public_case_id(scenario_id="generated_link_down_xs_001", episode_result=episode_result)

    assert case_a == case_b
    assert case_a.startswith("case-")
    assert "link_down" not in case_a


def test_build_public_symptoms_bounds_anomalies_without_mutating_raw_result():
    anomalies = [
        {
            "type": "packet_loss",
            "src_ip": f"192.0.2.{index}",
            "dst_ip": "198.51.100.1",
            "src_leaf": f"leaf{index % 4}",
            "dst_leaf": "leaf9",
            "severity": "high",
            "value": float(index),
        }
        for index in range(150)
    ]
    episode_result = {
        "episode": {"episode_id": "ep002", "duration_seconds": 72, "stabilization_time": 5},
        "observations": {
            "pingmesh_metrics": {
                "summary": {"total_anomalies": 150},
                "anomalies": anomalies,
                "aggregated_anomalies": {"by_src_leaf": {}},
            }
        },
    }

    payload = build_public_symptoms(episode_result=episode_result, pingmesh_query_window={})
    metrics = payload["observations"]["pingmesh_metrics"]

    assert len(metrics["anomalies"]) == 100
    assert metrics["returned_anomalies"] == 100
    assert metrics["truncated"] is True
    assert metrics["summary"]["total_anomalies"] == 150
    assert len(episode_result["observations"]["pingmesh_metrics"]["anomalies"]) == 150
