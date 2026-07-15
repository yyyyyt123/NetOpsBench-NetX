"""Neutral benchmark scale profile registry.

This module intentionally lives below :mod:`netopsbench.models` so public SDK
code, CLI code, and platform internals can share scale facts without importing
platform implementation modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class ScaleProfile:
    """Resource and topology parameters for one supported benchmark scale."""

    name: str
    family: Literal["clos", "fat-tree"]
    num_spines: int | None
    num_leafs: int | None
    fat_tree_k: int | None
    num_cores: int | None
    num_aggs: int | None
    num_edges: int | None
    clients_per_attached_switch: int
    management_prefix: int
    management_subnet_base: int
    pingmesh_destination_batch_size: int | None
    pingmesh_rtt_port_pool_size: int
    pingmesh_rtt_ports_per_cycle: int
    pingmesh_cycle_interval_seconds: int
    traffic_max_pps_per_client: int
    deploy_timeout_seconds: int
    worker_deploy_parallelism: int
    health_timeout_seconds: int
    containerlab_max_workers: int | None = None

    @property
    def clients_per_leaf(self) -> int | None:
        return self.clients_per_attached_switch if self.family == "clos" else None

    @property
    def clients_per_edge(self) -> int | None:
        return self.clients_per_attached_switch if self.family == "fat-tree" else None

    @property
    def total_clients(self) -> int:
        if self.family == "clos":
            return int(self.num_leafs or 0) * self.clients_per_attached_switch
        return int(self.num_edges or 0) * self.clients_per_attached_switch

    @property
    def total_switches(self) -> int:
        if self.family == "clos":
            return int(self.num_spines or 0) + int(self.num_leafs or 0)
        return int(self.num_cores or 0) + int(self.num_aggs or 0) + int(self.num_edges or 0)


SCALE_PROFILES: dict[str, ScaleProfile] = {
    "xs": ScaleProfile(
        name="xs",
        family="clos",
        num_spines=2,
        num_leafs=2,
        fat_tree_k=None,
        num_cores=None,
        num_aggs=None,
        num_edges=None,
        clients_per_attached_switch=1,
        management_prefix=24,
        management_subnet_base=100,
        pingmesh_destination_batch_size=None,
        pingmesh_rtt_port_pool_size=16,
        pingmesh_rtt_ports_per_cycle=8,
        pingmesh_cycle_interval_seconds=1,
        traffic_max_pps_per_client=250,
        deploy_timeout_seconds=1800,
        worker_deploy_parallelism=2,
        health_timeout_seconds=60,
    ),
    "small": ScaleProfile(
        name="small",
        family="clos",
        num_spines=2,
        num_leafs=4,
        fat_tree_k=None,
        num_cores=None,
        num_aggs=None,
        num_edges=None,
        clients_per_attached_switch=2,
        management_prefix=24,
        management_subnet_base=120,
        pingmesh_destination_batch_size=None,
        pingmesh_rtt_port_pool_size=16,
        pingmesh_rtt_ports_per_cycle=8,
        pingmesh_cycle_interval_seconds=1,
        traffic_max_pps_per_client=250,
        deploy_timeout_seconds=1800,
        worker_deploy_parallelism=2,
        health_timeout_seconds=60,
    ),
    "medium": ScaleProfile(
        name="medium",
        family="clos",
        num_spines=4,
        num_leafs=8,
        fat_tree_k=None,
        num_cores=None,
        num_aggs=None,
        num_edges=None,
        clients_per_attached_switch=2,
        management_prefix=24,
        management_subnet_base=140,
        pingmesh_destination_batch_size=None,
        pingmesh_rtt_port_pool_size=16,
        pingmesh_rtt_ports_per_cycle=6,
        pingmesh_cycle_interval_seconds=1,
        traffic_max_pps_per_client=200,
        deploy_timeout_seconds=1800,
        worker_deploy_parallelism=2,
        health_timeout_seconds=60,
    ),
    "large": ScaleProfile(
        name="large",
        family="clos",
        num_spines=4,
        num_leafs=16,
        fat_tree_k=None,
        num_cores=None,
        num_aggs=None,
        num_edges=None,
        clients_per_attached_switch=4,
        management_prefix=24,
        management_subnet_base=160,
        pingmesh_destination_batch_size=None,
        pingmesh_rtt_port_pool_size=16,
        pingmesh_rtt_ports_per_cycle=4,
        pingmesh_cycle_interval_seconds=1,
        traffic_max_pps_per_client=150,
        deploy_timeout_seconds=2700,
        worker_deploy_parallelism=1,
        health_timeout_seconds=180,
    ),
    "xlarge": ScaleProfile(
        name="xlarge",
        family="clos",
        num_spines=16,
        num_leafs=128,
        fat_tree_k=None,
        num_cores=None,
        num_aggs=None,
        num_edges=None,
        clients_per_attached_switch=1,
        management_prefix=23,
        management_subnet_base=180,
        pingmesh_destination_batch_size=16,
        pingmesh_rtt_port_pool_size=16,
        pingmesh_rtt_ports_per_cycle=4,
        pingmesh_cycle_interval_seconds=2,
        traffic_max_pps_per_client=100,
        deploy_timeout_seconds=3600,
        worker_deploy_parallelism=1,
        health_timeout_seconds=240,
        containerlab_max_workers=16,
    ),
    "fat-tree-k8": ScaleProfile(
        name="fat-tree-k8",
        family="fat-tree",
        num_spines=None,
        num_leafs=None,
        fat_tree_k=8,
        num_cores=16,
        num_aggs=32,
        num_edges=32,
        clients_per_attached_switch=4,
        management_prefix=24,
        management_subnet_base=200,
        pingmesh_destination_batch_size=16,
        pingmesh_rtt_port_pool_size=16,
        pingmesh_rtt_ports_per_cycle=4,
        pingmesh_cycle_interval_seconds=2,
        traffic_max_pps_per_client=100,
        deploy_timeout_seconds=3600,
        worker_deploy_parallelism=1,
        health_timeout_seconds=240,
        containerlab_max_workers=1,
    ),
    "fat-tree-k12": ScaleProfile(
        name="fat-tree-k12",
        family="fat-tree",
        num_spines=None,
        num_leafs=None,
        fat_tree_k=12,
        num_cores=36,
        num_aggs=72,
        num_edges=72,
        clients_per_attached_switch=2,
        management_prefix=23,
        management_subnet_base=220,
        pingmesh_destination_batch_size=16,
        pingmesh_rtt_port_pool_size=16,
        pingmesh_rtt_ports_per_cycle=4,
        pingmesh_cycle_interval_seconds=2,
        traffic_max_pps_per_client=50,
        deploy_timeout_seconds=5400,
        worker_deploy_parallelism=1,
        health_timeout_seconds=300,
        containerlab_max_workers=1,
    ),
}


def supported_scales() -> tuple[str, ...]:
    """Return scale names in their stable CLI and suite order."""
    return tuple(SCALE_PROFILES)


def get_scale_profile(name: str) -> ScaleProfile:
    """Return one profile or raise a focused error for unsupported scales."""
    try:
        return SCALE_PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown scale: {name}") from exc


__all__ = ["SCALE_PROFILES", "ScaleProfile", "get_scale_profile", "supported_scales"]
