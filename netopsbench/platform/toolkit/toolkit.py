"""Agent-facing network troubleshooting toolkit."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from netopsbench.config import config
from netopsbench.models.topology import TopologyManifest
from netopsbench.platform.topology.topology_utils import (
    clab_container_name,
    coerce_topology_manifest,
    load_topology_manifest,
)

from ._core.common import ToolResult
from ._core.device.connectivity_ops import ConnectivityOpsMixin
from ._core.device.interface_ops import InterfaceOpsMixin
from ._core.device.log_ops import LogOpsMixin
from ._core.device.routing_ops import RoutingOpsMixin
from ._core.device.telemetry_parsers import query_influx_rows
from ._core.device.validators import (
    docker_exec,
    resolve_container,
    validate_device_name,
    validate_interface_name,
    validate_ip_address,
    validate_prefix,
)
from ._core.observability.bgp_ops import BgpOpsMixin
from ._core.observability.metrics_ops import MetricsOpsMixin
from ._core.observability.pingmesh_ops import PingmeshOpsMixin
from ._core.observability.pingmesh_scope import resolve_pingmesh_time_scope


class AgentToolkit(
    InterfaceOpsMixin,
    RoutingOpsMixin,
    LogOpsMixin,
    ConnectivityOpsMixin,
    BgpOpsMixin,
    MetricsOpsMixin,
    PingmeshOpsMixin,
):
    """Toolkit providing network troubleshooting capabilities to an agent."""

    _SEVERITY_OPTIONS = {
        "emergency",
        "alert",
        "critical",
        "error",
        "warning",
        "notice",
        "info",
        "debug",
    }
    _PINGMESH_RANGE_ENV_START = "NETOPSBENCH_PINGMESH_START_TIME"
    _PINGMESH_RANGE_ENV_END = "NETOPSBENCH_PINGMESH_END_TIME"

    def __init__(
        self,
        influxdb_url: str | None = None,
        influxdb_token: str | None = None,
        influxdb_org: str | None = None,
        influxdb_bucket: str | None = None,
        topology_dir: str | Path | None = None,
        topology_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.influxdb_url = influxdb_url or config.influxdb_url
        self.influxdb_token = influxdb_token or config.influxdb_token
        self.influxdb_org = influxdb_org or config.influxdb_org
        self.influxdb_bucket = influxdb_bucket or config.influxdb_bucket

        topology_dir_value = topology_dir or config.topology_dir
        resolved_topology_dir = (
            Path(topology_dir_value).expanduser().resolve() if topology_dir_value is not None else None
        )
        if topology_metadata is not None:
            manifest = coerce_topology_manifest(topology_metadata)
        else:
            if resolved_topology_dir is None:
                raise ValueError("Pass topology_metadata or set an explicit topology_dir")
            metadata_file = resolved_topology_dir / "topology.json"
            if not metadata_file.is_file():
                raise FileNotFoundError(
                    f"Topology metadata not found: {metadata_file}. "
                    "Pass topology_metadata or set NETOPSBENCH_TOPOLOGY_DIR."
                )
            manifest = load_topology_manifest(metadata_file)
        self.manifest: TopologyManifest = manifest
        self.topology_dir: Path | None = resolved_topology_dir
        self.topology_metadata = manifest.model_dump(mode="json")
        self.topology_name = manifest.name
        self.topology_id = manifest.topology_id
        self.container_names = {
            device.name: clab_container_name(manifest.name, device.name) for device in manifest.devices
        }
        self._pingmesh_default_start_time: str | None = None
        self._pingmesh_default_end_time: str | None = None

    def get_topology(self) -> ToolResult:
        return ToolResult(success=True, data=self.manifest.to_agent_topology())

    def _validate_device_name(self, device: str, field_name: str = "device") -> str:
        return validate_device_name(device, field_name)

    def _validate_interface_name(self, interface: str) -> str:
        return validate_interface_name(interface)

    def _validate_ip_address(self, ip_value: str, field_name: str = "ip") -> str:
        return validate_ip_address(ip_value, field_name)

    def _validate_prefix(self, prefix: str) -> str:
        return validate_prefix(prefix)

    def _resolve_container(self, device: str, field_name: str = "device") -> str:
        return resolve_container(self, device, field_name)

    def _docker_exec(self, container: str, cmd_args: list[str], timeout: int) -> subprocess.CompletedProcess:
        return docker_exec(container, cmd_args, timeout)

    def _resolve_pingmesh_time_scope(
        self,
        time_range_minutes: int,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, Any]:
        return resolve_pingmesh_time_scope(self, time_range_minutes, start_time=start_time, end_time=end_time)

    def set_pingmesh_time_window(self, start_time: str | None, end_time: str | None) -> None:
        self._pingmesh_default_start_time = start_time
        self._pingmesh_default_end_time = end_time

    def _query_influx_rows(self, query: str, require_value: bool = True) -> list[dict[str, Any]]:
        return query_influx_rows(self, query, require_value=require_value)


__all__ = ["AgentToolkit", "ToolResult"]
