"""
Traffic Controller - Manages background iperf3 traffic flows
"""

import os
import shlex
import subprocess
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from netopsbench.logging_utils import get_logger
from netopsbench.platform.utils.events import emit as _emit
from netopsbench.platform.utils.proc import safe_run, sudo_prefix

logger = get_logger(__name__)
_DEFAULT_TRAFFIC_PARALLELISM = 32


def _traffic_parallelism() -> int:
    raw = os.environ.get("NETOPSBENCH_TRAFFIC_PARALLELISM", str(_DEFAULT_TRAFFIC_PARALLELISM))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring invalid NETOPSBENCH_TRAFFIC_PARALLELISM=%r; using %d",
            raw,
            _DEFAULT_TRAFFIC_PARALLELISM,
        )
        value = _DEFAULT_TRAFFIC_PARALLELISM
    return max(1, value)


def _flow_summary(flow: "TrafficFlow") -> str:
    return (
        f"src={flow.src} dst={flow.dst} dst_ip={flow.dst_ip} "
        f"protocol={flow.protocol} port={flow.dst_port} bandwidth={flow.bandwidth}"
    )


def _error_detail(exc: Exception) -> str:
    parts = [str(exc)]
    stderr = getattr(exc, "stderr", None)
    stdout = getattr(exc, "stdout", None)
    if stderr:
        parts.append(f"stderr={str(stderr).strip()}")
    if stdout:
        parts.append(f"stdout={str(stdout).strip()}")
    return "; ".join(part for part in parts if part)


@dataclass
class TrafficFlow:
    """Single iperf3 traffic flow specification"""

    src: str  # client name (e.g., "client1")
    dst: str  # destination client name
    dst_ip: str  # destination IP address
    dst_port: int = 5201
    protocol: str = "tcp"  # "tcp" or "udp"
    bandwidth: str = "100M"  # "100M", "1G", etc.
    duration: int = 0  # seconds, 0 = continuous
    parallel: int = 1  # parallel streams
    udp_payload_len: int = 1400
    tcp_mss: int = 1360
    flow_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class TrafficController:
    """
    Controls iperf3 traffic flows between client containers.

    Manages starting/stopping background traffic for benchmark realism.
    """

    def __init__(self, container_names: dict[str, str]):
        """
        Initialize traffic controller.

        Args:
            container_names: Dict mapping client names to container names
                            e.g., {"client1": "clab-dcn-client1"}
        """
        self.container_names = container_names
        self.active_flows: dict[str, TrafficFlow] = {}
        self.started_server_ports: dict[str, set] = {}
        # Bound external command latency to avoid benchmark hangs.
        self.command_timeout_seconds = 15
        self.start_retries = 1

    def _ensure_iperf_server(self, dst_client: str, dst_port: int):
        """Ensure destination has an iperf3 server listening on dst_port."""
        dst_container = self.container_names.get(dst_client)
        if not dst_container:
            raise ValueError(f"Unknown destination client: {dst_client}")

        started_ports = self.started_server_ports.setdefault(dst_container, set())
        if dst_port in started_ports:
            return

        check_cmd = f"ss -lnt 2>/dev/null | grep -q ':{dst_port} ' " f"|| iperf3 -s -D -p {dst_port}"
        safe_run(
            [*sudo_prefix(), "docker", "exec", dst_container, "sh", "-lc", check_cmd],
            check=True,
            capture_output=True,
            timeout=self.command_timeout_seconds,
        )
        started_ports.add(dst_port)

    def _ensure_iperf_servers_batch(self, dst_container: str, dst_ports: set[int]) -> None:
        """Ensure a destination container has all required iperf3 server ports."""
        started_ports = self.started_server_ports.setdefault(dst_container, set())
        missing_ports = sorted(port for port in dst_ports if port not in started_ports)
        if not missing_ports:
            return

        listener_checks = [f"ss -lnt 2>/dev/null | grep -q ':{port} '" for port in missing_ports]
        start_commands = []
        for port, check in zip(missing_ports, listener_checks, strict=True):
            start_commands.extend(
                [
                    f"if ! {check}; then iperf3 -s -D -p {port}; fi",
                    f"for _attempt in 1 2 3 4 5; do {check} && break; sleep 0.1; done",
                    check,
                ]
            )
        script = "\n".join(["set -e", *start_commands])
        safe_run(
            [*sudo_prefix(), "docker", "exec", dst_container, "sh", "-lc", script],
            check=True,
            capture_output=True,
            timeout=max(self.command_timeout_seconds, 15),
        )
        started_ports.update(missing_ports)

    def _client_command(self, flow: TrafficFlow) -> str:
        cmd_parts = [
            "iperf3",
            "-c",
            flow.dst_ip,
            "-p",
            str(flow.dst_port),
            "-P",
            str(flow.parallel),
        ]
        if flow.protocol == "udp":
            cmd_parts.extend(["-u", "-b", flow.bandwidth, "-l", str(flow.udp_payload_len)])
        elif flow.bandwidth:
            cmd_parts.extend(["-b", flow.bandwidth])
            if flow.tcp_mss > 0:
                cmd_parts.extend(["-M", str(flow.tcp_mss)])
        cmd_parts.extend(["-t", str(flow.duration)])
        return " ".join(shlex.quote(part) for part in cmd_parts)

    def _start_source_flows_batch(self, src_container: str, flows: list[TrafficFlow]) -> None:
        script = "\n".join(f"nohup {self._client_command(flow)} >/dev/null 2>&1 &" for flow in flows)
        safe_run(
            [*sudo_prefix(), "docker", "exec", src_container, "sh", "-lc", script],
            check=True,
            capture_output=True,
            timeout=max(self.command_timeout_seconds, 15),
        )

    def start_flow(self, flow: TrafficFlow) -> str:
        """
        Start a single iperf3 traffic flow.

        Args:
            flow: TrafficFlow specification

        Returns:
            flow_id of the started flow
        """
        src_container = self.container_names.get(flow.src)
        if not src_container:
            raise ValueError(f"Unknown client: {flow.src}")

        # Build iperf3 command
        cmd_parts = [
            *sudo_prefix(),
            "docker",
            "exec",
            "-d",
            src_container,
            "iperf3",
            "-c",
            flow.dst_ip,
            "-p",
            str(flow.dst_port),
            "-P",
            str(flow.parallel),
        ]

        if flow.protocol == "udp":
            cmd_parts.extend(["-u", "-b", flow.bandwidth, "-l", str(flow.udp_payload_len)])
        elif flow.bandwidth:
            cmd_parts.extend(["-b", flow.bandwidth])
            if flow.tcp_mss > 0:
                cmd_parts.extend(["-M", str(flow.tcp_mss)])

        # Keep continuous semantics explicit
        cmd_parts.extend(["-t", str(flow.duration)])

        # Start flow in background
        last_error: Exception | None = None
        for attempt in range(self.start_retries + 1):
            try:
                self._ensure_iperf_server(flow.dst, flow.dst_port)
                safe_run(
                    cmd_parts,
                    check=True,
                    capture_output=True,
                    timeout=self.command_timeout_seconds,
                )
                self.active_flows[flow.flow_id] = flow
                _emit(f"  Started flow: {flow.src} -> {flow.dst} ({flow.bandwidth})")
                return flow.flow_id
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                last_error = e
                if attempt < self.start_retries:
                    _emit(
                        f"  Warning: flow start retry {attempt + 1}/{self.start_retries} "
                        f"for {flow.src} -> {flow.dst}"
                    )
                    continue
                break

        _emit(f"  Failed to start flow {flow.src} -> {flow.dst}: {last_error}")
        raise RuntimeError(f"Failed to start flow {flow.src}->{flow.dst}: {last_error}")

    def stop_flow(self, flow_id: str):
        """
        Stop a specific traffic flow.

        Args:
            flow_id: ID of flow to stop
        """
        if flow_id not in self.active_flows:
            _emit(f"  Warning: Flow {flow_id} not found")
            return

        flow = self.active_flows[flow_id]
        src_container = self.container_names.get(flow.src)

        if src_container:
            try:
                # Kill iperf3 client processes
                safe_run(
                    [*sudo_prefix(), "docker", "exec", src_container, "pkill", "-f", f"iperf3 -c {flow.dst_ip}"],
                    check=False,  # Don't fail if process already stopped
                    capture_output=True,
                    timeout=15,
                )
                _emit(f"  Stopped flow: {flow.src} -> {flow.dst}")

            except Exception as e:
                _emit(f"  Warning: Failed to stop flow {flow_id}: {e}", level="warning")

        del self.active_flows[flow_id]

    def start_matrix(self, flows: list[TrafficFlow]) -> list[str]:
        """
        Start multiple traffic flows.

        Args:
            flows: List of TrafficFlow specifications

        Returns:
            List of flow_ids
        """
        if not flows:
            _emit("\n  Started 0/0 flows")
            return []

        valid_flows: list[TrafficFlow] = []
        dst_ports_by_container: dict[str, set[int]] = defaultdict(set)
        flows_by_dst_container: dict[str, list[TrafficFlow]] = defaultdict(list)
        for flow in flows:
            src_container = self.container_names.get(flow.src)
            dst_container = self.container_names.get(flow.dst)
            if not src_container:
                _emit(f"  Warning: Skipping flow with unknown source: {_flow_summary(flow)}", level="warning")
                continue
            if not dst_container:
                _emit(f"  Warning: Skipping flow with unknown destination: {_flow_summary(flow)}", level="warning")
                continue
            valid_flows.append(flow)
            dst_ports_by_container[dst_container].add(flow.dst_port)
            flows_by_dst_container[dst_container].append(flow)

        failed_flows: set[str] = set()
        parallelism = min(_traffic_parallelism(), max(len(dst_ports_by_container), 1))
        with ThreadPoolExecutor(max_workers=parallelism) as executor:
            future_map = {
                executor.submit(self._ensure_iperf_servers_batch, dst_container, dst_ports): dst_container
                for dst_container, dst_ports in dst_ports_by_container.items()
            }
            for future in as_completed(future_map):
                dst_container = future_map[future]
                try:
                    future.result()
                except Exception as exc:
                    for flow in flows_by_dst_container.get(dst_container, []):
                        failed_flows.add(flow.flow_id)
                        _emit(
                            f"  Warning: Failed to ensure server for {_flow_summary(flow)}: {_error_detail(exc)}",
                            level="warning",
                        )

        source_groups: dict[str, list[TrafficFlow]] = defaultdict(list)
        for flow in valid_flows:
            if flow.flow_id in failed_flows:
                continue
            source_groups[self.container_names[flow.src]].append(flow)

        parallelism = min(_traffic_parallelism(), max(len(source_groups), 1))
        with ThreadPoolExecutor(max_workers=parallelism) as executor:
            future_map = {
                executor.submit(self._start_source_flows_batch, src_container, src_flows): (src_container, src_flows)
                for src_container, src_flows in source_groups.items()
            }
            for future in as_completed(future_map):
                src_container, src_flows = future_map[future]
                try:
                    future.result()
                except Exception as exc:
                    for flow in src_flows:
                        failed_flows.add(flow.flow_id)
                        _emit(
                            "  Warning: Failed to start flow on "
                            f"{src_container}: {_flow_summary(flow)}: {_error_detail(exc)}",
                            level="warning",
                        )
                    continue
                for flow in src_flows:
                    self.active_flows[flow.flow_id] = flow
                _emit(f"  Started {len(src_flows)} flow(s) from {src_flows[0].src}")

        flow_ids = [flow.flow_id for flow in valid_flows if flow.flow_id in self.active_flows]
        _emit(f"\n  Started {len(flow_ids)}/{len(flows)} flows")
        return flow_ids

    def stop_all(self):
        """Stop all active traffic flows."""
        flow_ids = list(self.active_flows.keys())
        flows_by_src_container: dict[str, list[TrafficFlow]] = defaultdict(list)
        for flow_id in flow_ids:
            flow = self.active_flows[flow_id]
            src_container = self.container_names.get(flow.src)
            if src_container:
                flows_by_src_container[src_container].append(flow)
            else:
                del self.active_flows[flow_id]

        def stop_container(src_container: str) -> None:
            safe_run(
                [*sudo_prefix(), "docker", "exec", src_container, "sh", "-lc", "pkill -f 'iperf3 -c' || true"],
                check=False,
                capture_output=True,
                timeout=15,
            )

        parallelism = min(_traffic_parallelism(), max(len(flows_by_src_container), 1))
        with ThreadPoolExecutor(max_workers=parallelism) as executor:
            future_map = {
                executor.submit(stop_container, src_container): (src_container, src_flows)
                for src_container, src_flows in flows_by_src_container.items()
            }
            for future in as_completed(future_map):
                src_container, src_flows = future_map[future]
                try:
                    future.result()
                    _emit(f"  Stopped {len(src_flows)} flow(s) from {src_flows[0].src}")
                except Exception as exc:
                    _emit(
                        f"  Warning: Failed to stop flows on {src_container}: {_error_detail(exc)}",
                        level="warning",
                    )
                finally:
                    for flow in src_flows:
                        self.active_flows.pop(flow.flow_id, None)

        _emit(f"  Stopped all {len(flow_ids)} flows")

    def get_active_flows(self) -> list[TrafficFlow]:
        """Get list of all active flows."""
        return list(self.active_flows.values())
