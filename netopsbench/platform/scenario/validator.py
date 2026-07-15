"""Scenario validation: schema constraints and topology compatibility."""

from __future__ import annotations

import os

from netopsbench.models.profiles import supported_scales
from netopsbench.platform.faults.specs import (
    FaultSpecRegistry,
    create_fault_registry,
)
from netopsbench.platform.topology.configdb_payload import interface_names_for_config
from netopsbench.platform.topology.topology_utils import load_topology_manifest
from netopsbench.platform.utils.interface_names import interface_aliases

from .models import Scenario
from .parser import episode_from_dict, episode_to_dict

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SUPPORTED_TOPOLOGY_SCALES = supported_scales()
_SUPPORTED_TRAFFIC_PROFILES = ("standard",)

NETWORK_DEVICE_PREFIXES = ("spine", "leaf", "core", "agg", "edge")

# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def supported_scenario_faults(fault_registry: FaultSpecRegistry | None = None) -> list[str]:
    registry = fault_registry or create_fault_registry()
    faults = sorted(set(registry.supported_scenario_faults()))
    return ["none", *faults]


def validate_scenario(
    scenario: Scenario,
    fault_registry: FaultSpecRegistry | None = None,
) -> list[str]:
    """Validate a scenario's schema and fault-specific constraints."""
    registry = fault_registry or create_fault_registry()
    errors: list[str] = []

    if not scenario.scenario_id:
        errors.append("Missing scenario_id")
    if not scenario.name:
        errors.append("Missing scenario name")
    if not scenario.episodes:
        errors.append("No episodes defined")
    if scenario.topology_scale not in _SUPPORTED_TOPOLOGY_SCALES:
        errors.append(f"Invalid topology_scale: {scenario.topology_scale}")
    if scenario.traffic_profile not in _SUPPORTED_TRAFFIC_PROFILES:
        errors.append(f"Invalid traffic_profile: {scenario.traffic_profile}; only 'standard' is supported")

    canonical_fault_types = [registry.canonicalize(ep.fault_type) for ep in scenario.episodes]
    has_fault_episode = any(fault_type != "none" for fault_type in canonical_fault_types)
    if has_fault_episode:
        difficulty = (scenario.metadata or {}).get("difficulty")
        if difficulty not in ["easy", "medium", "hard"]:
            errors.append("Scenario metadata requires difficulty in [easy, medium, hard] for benchmark scoring")

        expected_diagnosis = registry.canonicalize((scenario.metadata or {}).get("expected_diagnosis"))
        if not expected_diagnosis:
            errors.append("Scenario metadata missing expected_diagnosis for benchmark scoring")

    supported_faults = supported_scenario_faults(registry)
    for i, episode in enumerate(scenario.episodes):
        canonical_fault_type = registry.canonicalize(episode.fault_type)
        if not episode.episode_id:
            errors.append(f"Episode {i}: Missing episode_id")
        if not canonical_fault_type:
            errors.append(f"Episode {i}: Missing fault_type")
        if canonical_fault_type not in supported_faults:
            errors.append(
                f"Episode {i}: Unsupported fault_type '{canonical_fault_type}'. "
                f"Supported values: {supported_faults}"
            )
        if canonical_fault_type != "none" and not episode.target_device:
            errors.append(f"Episode {i}: Missing target_device")

        spec = registry.get(canonical_fault_type)
        if spec is not None:
            episode_view = episode_from_dict({**episode_to_dict(episode), "fault_type": canonical_fault_type})
            errors.extend(spec.validate_episode(episode_view, episode_index=i))

    return errors


# ---------------------------------------------------------------------------
# Topology validation
# ---------------------------------------------------------------------------


def _parse_config_interfaces(config_path: str) -> set[str]:
    return set(interface_names_for_config(config_path))


def _validate_episode_target_interface(
    topology_dir: str,
    target_device: str,
    target_interface: str,
    scenario_id: str,
    episode_id: str,
) -> list[str]:
    if not target_interface:
        return []
    if not target_device.startswith(NETWORK_DEVICE_PREFIXES):
        return []

    config_path = os.path.join(topology_dir, "configs", "sonic", target_device, "config_db.json")
    if not os.path.isfile(config_path):
        return [f"[scenario={scenario_id} episode={episode_id}] Required ConfigDB artifact is missing: {config_path}"]
    config_interfaces = _parse_config_interfaces(config_path)

    if not config_interfaces:
        return [
            (
                f"[scenario={scenario_id} episode={episode_id}] ConfigDB artifact has no interfaces "
                f"for device '{target_device}': {config_path}"
            )
        ]

    allowed_aliases: set[str] = set()
    for iface in config_interfaces:
        allowed_aliases.update(interface_aliases(iface))

    if target_interface in allowed_aliases:
        return []

    return [
        (
            f"[scenario={scenario_id} episode={episode_id}] target_interface '{target_interface}' "
            f"not found on device '{target_device}' in topology config. "
            f"Examples: {sorted(list(allowed_aliases))[:6]}"
        )
    ]


def validate_scenario_topology(scenario, topology_dir: str) -> dict:
    """Validate scenario topology compatibility and episode target consistency."""
    manifest = load_topology_manifest(os.path.join(topology_dir, "topology.json"))
    actual_scale = manifest.scale
    declared_scale = scenario.topology_scale

    errors: list[str] = []
    warnings: list[str] = []

    if actual_scale == "unknown":
        errors.append(
            f"[scenario={scenario.scenario_id}] Could not infer actual topology scale from {topology_dir}/topology.json"
        )
    elif declared_scale != actual_scale:
        errors.append(
            f"[scenario={scenario.scenario_id}] topology_scale mismatch: declared='{declared_scale}' "
            f"actual='{actual_scale}'. Use matching scenario files or redeploy the topology."
        )

    device_names = {device.name for device in manifest.devices}
    for episode in scenario.episodes:
        if episode.fault_type != "none" and episode.target_device not in device_names:
            errors.append(
                f"[scenario={scenario.scenario_id} episode={episode.episode_id}] target_device "
                f"'{episode.target_device}' not found in topology devices {sorted(device_names)}"
            )
        if episode.target_interface and episode.target_device:
            errors.extend(
                _validate_episode_target_interface(
                    topology_dir=topology_dir,
                    target_device=episode.target_device,
                    target_interface=episode.target_interface,
                    scenario_id=scenario.scenario_id,
                    episode_id=episode.episode_id,
                )
            )

    status = "fail" if errors else "pass"
    return {
        "status": status,
        "declared_scale": declared_scale,
        "actual_scale": actual_scale,
        "errors": errors,
        "warnings": warnings,
        "topology_devices": sorted(device_names),
    }


__all__ = [
    "supported_scenario_faults",
    "validate_scenario",
    "validate_scenario_topology",
]
