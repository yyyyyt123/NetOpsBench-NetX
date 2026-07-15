"""Scenario scoring helpers."""

from pathlib import Path
from typing import Any

from netopsbench.evaluator.scorer import AgentOutput, EvaluationResult, Evaluator
from netopsbench.platform.topology.configdb_payload import interface_networks_for_config
from netopsbench.platform.utils.interface_names import are_interfaces_equivalent

# Fault types where the injected interface and its link-peer are both valid answers.
# link_down / link_flapping: the link can be attributed to either endpoint.
# packet_loss / packet_corruption / high_latency: interface-level impairments are
#   observable from both sides of the link, so the peer endpoint is equivalent.
# mtu_mismatch: misconfiguration requires both ends to match; either endpoint is a
#   valid root-cause answer.
_INTERFACE_SYMMETRIC_FAULT_TYPES = {
    "link_down",
    "link_flapping",
    "packet_loss",
    "packet_corruption",
    "high_latency",
    "mtu_mismatch",
}


def _parse_device_interface_networks(config_path: Path) -> dict[str, str]:
    return interface_networks_for_config(config_path)


def _resolve_config_interface(target_interface: str | None, interface_names: list[str]) -> str | None:
    if not target_interface:
        return None
    for interface_name in interface_names:
        if are_interfaces_equivalent(target_interface, interface_name):
            return interface_name
    return None


def _device_config_path(configs_dir: Path, device: str) -> Path:
    return configs_dir / "sonic" / device / "config_db.json"


def _iter_device_config_paths(configs_dir: Path) -> list[Path]:
    preseed_root = configs_dir / "sonic"
    if not preseed_root.exists():
        return []
    return sorted(preseed_root.glob("*/config_db.json"))


def _device_name_for_config_path(config_path: Path) -> str:
    if config_path.name == "config_db.json":
        return config_path.parent.name
    return config_path.stem


def _find_link_peer_locations(
    topology_dir: str | None,
    target_device: str | None,
    target_interface: str | None,
) -> list[dict[str, str]]:
    if not topology_dir or not target_device or not target_interface:
        return []
    configs_dir = Path(topology_dir) / "configs"
    if not configs_dir.exists():
        return []
    target_config = _device_config_path(configs_dir, target_device)
    target_networks = _parse_device_interface_networks(target_config)
    target_config_interface = _resolve_config_interface(target_interface, list(target_networks.keys()))
    if not target_config_interface:
        return []
    target_network = target_networks.get(target_config_interface)
    if not target_network:
        return []
    peers: list[dict[str, str]] = []
    seen = set()
    for config_path in _iter_device_config_paths(configs_dir):
        peer_device = _device_name_for_config_path(config_path)
        if peer_device == target_device:
            continue
        for peer_interface, peer_network in _parse_device_interface_networks(config_path).items():
            if peer_network != target_network:
                continue
            identity = (peer_device, peer_interface)
            if identity in seen:
                continue
            seen.add(identity)
            peers.append({"device": peer_device, "interface": peer_interface})
    return peers


def build_episode_ground_truth(episode_info: dict[str, Any], topology_dir: str | None = None) -> dict[str, Any]:
    location = {"device": episode_info.get("target_device")}
    if episode_info.get("target_interface"):
        location["interface"] = episode_info.get("target_interface")
    ground_truth = {"fault_type": episode_info.get("fault_type"), "location": location}
    if episode_info.get("fault_type") in _INTERFACE_SYMMETRIC_FAULT_TYPES:
        equivalent_locations = _find_link_peer_locations(
            topology_dir=topology_dir,
            target_device=episode_info.get("target_device"),
            target_interface=episode_info.get("target_interface"),
        )
        if equivalent_locations:
            ground_truth["equivalent_locations"] = equivalent_locations
    return ground_truth


def diagnosis_to_agent_output(diagnosis: dict[str, Any] | None) -> AgentOutput:
    if not diagnosis or diagnosis.get("error"):
        error = diagnosis.get("error") if isinstance(diagnosis, dict) else "diagnosis_missing"
        return AgentOutput(
            verdict="inconclusive",
            fault_type=None,
            location={},
            evidence=[f"diagnosis_unavailable: {error}"],
            confidence=0.0,
            reasoning="No valid diagnosis available for this fault episode.",
            tool_calls=[],
            time_taken_seconds=0.0,
            metadata={"final_status": "diagnosis_unavailable", "error": error},
        )
    return AgentOutput(
        verdict=diagnosis.get("verdict", "network_healthy"),
        fault_type=diagnosis.get("fault_type"),
        location=diagnosis.get("location") or {},
        evidence=diagnosis.get("evidence") or [],
        confidence=float(diagnosis.get("confidence", 0.0) or 0.0),
        reasoning=diagnosis.get("reasoning", ""),
        tool_calls=diagnosis.get("tool_calls") or [],
        time_taken_seconds=float(diagnosis.get("time_taken_seconds", 0.0) or 0.0),
        metadata=diagnosis.get("metadata") or {},
    )


def score_scenario_fault_episodes(
    scenario,
    scenario_result: dict[str, Any],
    evaluator: Evaluator,
    topology_dir: str | None = None,
) -> list[EvaluationResult]:
    scored_results: list[EvaluationResult] = []
    scenario_difficulty = (scenario.metadata or {}).get("difficulty", "unknown")
    is_negative_sample = bool((scenario.metadata or {}).get("negative_sample", False))

    if is_negative_sample:
        # For negative (healthy network) scenarios, evaluate the second episode
        # (ep002_observation_2) as the agent's primary observation window.
        episodes = scenario_result.get("episodes", [])
        # Pick the middle episode; fall back to the first if only one exists.
        observation_episode = episodes[1] if len(episodes) > 1 else (episodes[0] if episodes else None)
        if observation_episode:
            episode_info = observation_episode.get("episode", {})
            testcase_id = f"{scenario.scenario_id}:{episode_info.get('episode_id', 'unknown')}"
            agent_output = diagnosis_to_agent_output(observation_episode.get("diagnosis"))
            # Empty ground_truth triggers the evaluator's negative-sample path.
            eval_result = evaluator.evaluate(agent_output, {}, testcase_id)
            eval_result.details["difficulty"] = scenario_difficulty
            eval_result.details["scenario_id"] = scenario.scenario_id
            eval_result.details["episode_id"] = episode_info.get("episode_id")
            eval_result.details["negative_sample"] = True
            scored_results.append(eval_result)
        return scored_results

    for episode_result in scenario_result.get("episodes", []):
        episode_info = episode_result.get("episode", {})
        fault_type = episode_info.get("fault_type")
        if fault_type == "none":
            continue
        testcase_id = f"{scenario.scenario_id}:{episode_info.get('episode_id', 'unknown')}"
        ground_truth = build_episode_ground_truth(episode_info, topology_dir=topology_dir)
        agent_output = diagnosis_to_agent_output(episode_result.get("diagnosis"))
        eval_result = evaluator.evaluate(agent_output, ground_truth, testcase_id)
        eval_result.details["difficulty"] = scenario_difficulty
        eval_result.details["scenario_id"] = scenario.scenario_id
        eval_result.details["episode_id"] = episode_info.get("episode_id")
        scored_results.append(eval_result)
    return scored_results
