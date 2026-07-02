"""
Pingmesh Pinglist Generator
Generates probe task lists from topology metadata
"""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from netopsbench.logging_utils import get_logger

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

    def generate(self, topology_metadata: dict, max_dests_per_client: int | None = None) -> list[ProbeTask]:
        """
        Generate probe tasks for client pairs.

        Args:
            topology_metadata: Topology metadata dict with devices.clients
            max_dests_per_client: Optional deterministic cap per source client.
                ``None`` preserves the default all-pairs behavior.

        Returns:
            List of ProbeTask objects.
        """
        clients = topology_metadata["devices"]["clients"]
        tasks = []

        for src_idx, src in enumerate(clients):
            for dst in self._destination_clients(clients, src_idx, max_dests_per_client):
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
        max_dests_per_client: int | None,
    ) -> list[dict]:
        total_clients = len(clients)
        if total_clients <= 1:
            return []

        available = total_clients - 1
        if max_dests_per_client is None or max_dests_per_client >= available:
            return [clients[(src_idx + offset) % total_clients] for offset in range(1, total_clients)]
        if max_dests_per_client <= 0:
            return []

        offsets = []
        seen = set()
        for slot in range(max_dests_per_client):
            # Evenly spread each source's sampled destinations around the ring.
            offset = 1 + ((slot * available) // max_dests_per_client)
            if offset not in seen:
                offsets.append(offset)
                seen.add(offset)

        return [clients[(src_idx + offset) % total_clients] for offset in offsets]

    def save_pinglist(
        self,
        tasks: list[ProbeTask],
        output_file: str,
        topology_id: str | None = None,
        max_dests_per_client: int | None = None,
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
        if max_dests_per_client is not None:
            data["max_dests_per_client"] = max_dests_per_client

        with open(output_file, "w") as f:
            json.dump(data, f, indent=2)

        logger.info("Saved %s probe tasks to %s", len(tasks), output_file)


def _infer_topology_id(topology_file: str) -> str:
    return Path(topology_file).resolve().parent.name


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring invalid Pingmesh destination cap %r; expected integer", raw)
        return None


def _resolve_max_dests_per_client(topology_metadata: dict, explicit: int | None = None) -> int | None:
    if explicit is not None:
        return explicit
    for env_name in ("NETOPSBENCH_PINGMESH_MAX_DESTS_PER_CLIENT", "PINGMESH_MAX_DESTS_PER_CLIENT"):
        value = _optional_int(os.environ.get(env_name))
        if value is not None:
            return value
    pingmesh_config = topology_metadata.get("pingmesh", {}) or {}
    return _optional_int(pingmesh_config.get("max_dests_per_client"))


def generate_pinglist_from_topology(
    topology_file: str,
    output_file: str = "pinglist.json",
    topology_id: str | None = None,
    max_dests_per_client: int | None = None,
):
    """
    Convenience function to generate pinglist from topology file.

    Args:
        topology_file: Path to topology.json
        output_file: Output pinglist.json path
        topology_id: Explicit topology identifier; inferred from directory name if omitted
        max_dests_per_client: Optional deterministic destination cap per source
    """
    with open(topology_file) as f:
        topology = json.load(f)

    generator = PinglistGenerator()
    max_dests_per_client = _resolve_max_dests_per_client(topology, max_dests_per_client)
    tasks = generator.generate(topology, max_dests_per_client=max_dests_per_client)
    topology_id = topology_id or _infer_topology_id(topology_file)
    generator.save_pinglist(
        tasks,
        output_file,
        topology_id=topology_id,
        max_dests_per_client=max_dests_per_client,
    )

    return tasks
