"""
Pingmesh Pinglist Generator
Generates probe task lists from topology metadata
"""

import json
from dataclasses import asdict, dataclass

from netopsbench.logging_utils import get_logger
from netopsbench.platform.topology.topology_utils import coerce_topology_manifest, load_topology_manifest

logger = get_logger(__name__)


@dataclass
class ProbeTask:
    """Single probe task specification"""

    src_ip: str
    src_name: str
    src_rack: str
    src_leaf: str
    dst_ip: str
    dst_name: str
    dst_rack: str
    dst_leaf: str
    path_type: str  # "same_rack" | "cross_rack"


class PinglistGenerator:
    """Generates pinglist tasks from client metadata."""

    def generate(self, topology_metadata: dict) -> list[ProbeTask]:
        """
        Generate probe tasks for client pairs.

        Args:
            topology_metadata: Topology metadata dict with devices.clients
        Returns:
            List of ProbeTask objects.
        """
        manifest = coerce_topology_manifest(topology_metadata)
        clients = manifest.to_agent_topology()["devices"]["clients"]
        tasks = []

        for src_idx, src in enumerate(clients):
            for dst in self._destination_clients(clients, src_idx):
                path_type = "same_rack" if src["rack"] == dst["rack"] else "cross_rack"

                task = ProbeTask(
                    src_ip=src["data_ip"],
                    src_name=src["name"],
                    src_rack=src["rack"],
                    src_leaf=src["leaf"],
                    dst_ip=dst["data_ip"],
                    dst_name=dst["name"],
                    dst_rack=dst["rack"],
                    dst_leaf=dst["leaf"],
                    path_type=path_type,
                )
                tasks.append(task)

        return tasks

    def _destination_clients(
        self,
        clients: list[dict],
        src_idx: int,
    ) -> list[dict]:
        total_clients = len(clients)
        if total_clients <= 1:
            return []

        return [clients[(src_idx + offset) % total_clients] for offset in range(1, total_clients)]

    def save_pinglist(
        self,
        tasks: list[ProbeTask],
        output_file: str,
        topology_id: str | None = None,
        pingmesh_policy: dict | None = None,
    ):
        """
        Save pinglist to JSON file.

        Args:
            tasks: List of ProbeTask objects
            output_file: Output JSON file path
        """
        data = {"total_probes": len(tasks), "probes": [asdict(t) for t in tasks]}
        if topology_id:
            data["topology_id"] = topology_id
        if pingmesh_policy:
            data["pingmesh_policy"] = pingmesh_policy

        with open(output_file, "w") as f:
            json.dump(data, f, indent=2)

        logger.info("Saved %s probe tasks to %s", len(tasks), output_file)


def generate_pinglist_from_topology(
    topology_file: str,
    output_file: str = "pinglist.json",
    topology_id: str | None = None,
):
    """
    Convenience function to generate pinglist from topology file.

    Args:
        topology_file: Path to topology.json
        output_file: Output pinglist.json path
        topology_id: Explicit topology identifier; inferred from directory name if omitted
    """
    manifest = load_topology_manifest(topology_file)
    topology = manifest.model_dump(mode="json")

    generator = PinglistGenerator()
    tasks = generator.generate(topology)
    topology_id = topology_id or manifest.topology_id
    policy = {
        **manifest.pingmesh.model_dump(mode="json"),
        "destination_batch_count": manifest.pingmesh.destination_batch_count(len(manifest.clients())),
        "port_batch_count": manifest.pingmesh.port_batch_count(),
        "coverage_epoch_cycles": manifest.pingmesh.coverage_epoch_cycles(len(manifest.clients())),
        "coverage_epoch_seconds": manifest.pingmesh.coverage_epoch_seconds(len(manifest.clients())),
    }
    generator.save_pinglist(
        tasks,
        output_file,
        topology_id=topology_id,
        pingmesh_policy=policy,
    )

    return tasks
