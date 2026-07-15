#!/usr/bin/env python3
"""Pingmesh probe agent library.

Defines :class:`PingmeshAgent`. The CLI entrypoint lives in
:mod:`netopsbench.platform.pingmesh.cli` (also staged and bind-mounted into
each client container by :mod:`netopsbench.platform.pingmesh.deploy`).
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time

import requests

from netopsbench.config import config
from netopsbench.logging_utils import get_logger
from netopsbench.platform.pingmesh._agent_influx import PingInfluxMixin
from netopsbench.platform.pingmesh._agent_probe import UdpProbeMixin
from netopsbench.platform.pingmesh._agent_responder import UdpEchoResponder
from netopsbench.platform.pingmesh._agent_runtime import PingRuntimeMixin

logger = get_logger(__name__)


def _stable_host_seed(hostname: str) -> int:
    digest = hashlib.blake2s(str(hostname or "").encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def _deterministic_startup_jitter_seconds(hostname: str, interval: float) -> float:
    try:
        interval_value = float(interval)
    except (TypeError, ValueError):
        interval_value = 0.0
    if interval_value <= 0:
        return 0.0
    seed = _stable_host_seed(hostname)
    return (seed % 10_000) / 10_000.0 * interval_value


def _deterministic_phase(hostname: str, modulus: int) -> int:
    if modulus <= 1:
        return 0
    return _stable_host_seed(hostname) % modulus


def _rotating_destination_batch(
    tasks: list[dict],
    batch_size: int,
    batch_index: int,
    phase_offset: int = 0,
) -> list[dict]:
    if not tasks:
        return []
    size = max(1, min(int(batch_size), len(tasks)))
    batch_count = max(1, (len(tasks) + size - 1) // size)
    phase = int(phase_offset) % len(tasks)
    ordered = tasks[phase:] + tasks[:phase]
    return ordered[int(batch_index) % batch_count :: batch_count]


def _probe_batch_indices(
    cycle: int,
    destination_batch_count: int,
    port_batch_count: int,
    port_phase: int = 0,
) -> tuple[int, int]:
    """Return one deterministic destination/port-batch Cartesian pair."""
    destination_count = max(1, int(destination_batch_count))
    port_count = max(1, int(port_batch_count))
    epoch_cycle = int(cycle) % (destination_count * port_count)
    destination_batch = epoch_cycle % destination_count
    port_round = epoch_cycle // destination_count
    return destination_batch, (port_round + int(port_phase)) % port_count


class PingmeshAgent(UdpProbeMixin, PingInfluxMixin, PingRuntimeMixin):
    """Pingmesh probe agent that runs periodic ping tests."""

    def __init__(
        self,
        pinglist_file: str,
        influxdb_url: str | None = None,
        influxdb_token: str | None = None,
        influxdb_org: str | None = None,
        influxdb_bucket: str | None = None,
        udp_dst_port: int = 33434,
        rtt_src_port_base: int = 33000,
        enable_df_probe: bool = True,
        batch_size: int = 50,
        batch_timeout: float = 2.0,
        max_retries: int = 3,
        retry_backoff_base: float = 0.5,
    ):
        self.my_name = os.environ.get("HOSTNAME", "unknown")
        self.udp_dst_port = udp_dst_port
        self.rtt_src_port_base = rtt_src_port_base
        self.enable_df_probe = enable_df_probe
        self.influxdb_url = influxdb_url or config.influxdb_url
        self.influxdb_token = influxdb_token or config.influxdb_token
        self.influxdb_org = influxdb_org or config.influxdb_org
        self.influxdb_bucket = influxdb_bucket or config.influxdb_bucket
        self.use_influxdb = True
        self.batch_size = batch_size
        self.batch_timeout = batch_timeout
        self.max_retries = max_retries
        self.retry_backoff_base = retry_backoff_base

        with open(pinglist_file, encoding="utf-8") as f:
            data = json.load(f)
        policy = dict(data.get("pingmesh_policy") or {})
        required_policy = {
            "rtt_port_pool_size",
            "rtt_ports_per_cycle",
            "cycle_interval_seconds",
            "destination_batch_count",
            "port_batch_count",
            "coverage_epoch_cycles",
            "coverage_epoch_seconds",
            "df_payload_size",
        }
        missing_policy = sorted(required_policy - policy.keys())
        if missing_policy:
            raise ValueError(
                "Pingmesh policy is incomplete; regenerate the topology. "
                f"Missing fields: {', '.join(missing_policy)}"
            )
        configured_destination_batch_size = policy.get("destination_batch_size")
        self.n_rtt_ports = int(policy["rtt_port_pool_size"])
        self.rtt_ports_per_cycle = max(1, min(int(policy["rtt_ports_per_cycle"]), self.n_rtt_ports))
        self.interval = float(policy["cycle_interval_seconds"])
        self.min_interval = self.interval
        self.max_interval = self.interval
        self.coverage_epoch_cycles = max(1, int(policy["coverage_epoch_cycles"]))
        self.coverage_epoch_seconds = max(1, int(policy["coverage_epoch_seconds"]))
        self.df_payload_size = int(policy["df_payload_size"])
        self.topology_id = data.get("topology_id") or ""
        self.startup_jitter_s = _deterministic_startup_jitter_seconds(self.my_name, self.interval)

        self.tasks = [probe for probe in data["probes"] if probe["src_name"] == self.my_name]
        self.destination_batch_size = int(configured_destination_batch_size or len(self.tasks) or 1)
        self.destination_batch_count = max(
            1, (len(self.tasks) + self.destination_batch_size - 1) // self.destination_batch_size
        )
        source_names = list(dict.fromkeys(str(probe.get("src_name")) for probe in data["probes"]))
        self.destination_phase = source_names.index(self.my_name) if self.my_name in source_names else 0
        self.rtt_port_batch_count = max(
            1, (self.n_rtt_ports + self.rtt_ports_per_cycle - 1) // self.rtt_ports_per_cycle
        )
        if int(policy["destination_batch_count"]) != self.destination_batch_count:
            raise ValueError("Pingmesh destination batch count is stale; regenerate the topology")
        if int(policy["port_batch_count"]) != self.rtt_port_batch_count:
            raise ValueError("Pingmesh port batch count is stale; regenerate the topology")
        if self.coverage_epoch_cycles != self.destination_batch_count * self.rtt_port_batch_count:
            raise ValueError("Pingmesh coverage epoch is stale; regenerate the topology")
        self.rtt_port_phase = _deterministic_phase(self.my_name, self.rtt_port_batch_count)
        self.probe_cycle = 0
        # Discover this host's data-plane IP from the pinglist so the UDP
        # responder binds to the fabric-facing interface (eth1), not mgmt.
        own_data_ip = next(
            (p["src_ip"] for p in data["probes"] if p["src_name"] == self.my_name),
            "",
        )
        self.responder = UdpEchoResponder(bind_ip=own_data_ip, port=self.udp_dst_port)
        try:
            self.responder.start()
        except Exception as exc:
            logger.error("Failed to start UDP echo responder: %s", exc)
            self.responder = None
        self.write_queue = queue.Queue()
        self.batch_lock = threading.Lock()
        self.batch_buffer = []
        self.batch_last_flush = time.time()
        self.shutdown_event = threading.Event()
        if self.use_influxdb:
            self.session = requests.Session()
            self.session.headers.update(
                {
                    "Authorization": f"Token {self.influxdb_token}",
                    "Content-Type": "text/plain; charset=utf-8",
                }
            )
            self.batch_writer_thread = threading.Thread(target=self._batch_writer_worker, daemon=True)
            self.batch_writer_thread.start()
        else:
            self.session = None

        logger.info("Pingmesh Agent started: %s", self.my_name)
        logger.info("  Probe tasks: %s", len(self.tasks))
        logger.info(
            "  Destination rotation: %s active, %s batches, phase=%s",
            min(self.destination_batch_size, len(self.tasks)),
            self.destination_batch_count,
            self.destination_phase,
        )
        logger.info("  Coverage epoch: %s cycles (%ss)", self.coverage_epoch_cycles, self.coverage_epoch_seconds)
        active_port_count = self.rtt_ports_per_cycle
        logger.info("  Probe worker: 1")
        active_destinations = min(self.destination_batch_size, len(self.tasks))
        logger.info("  Concurrent flows: %s", active_destinations * active_port_count)
        logger.info(
            "  Shared RTT/DF port pool: %s ports (src %s-%s)",
            self.n_rtt_ports,
            self.rtt_src_port_base,
            self.rtt_src_port_base + self.n_rtt_ports - 1,
        )
        logger.info(
            "  Active ports/cycle: RTT %s/%s; DF reuses one matching tuple per destination",
            self.rtt_ports_per_cycle,
            self.n_rtt_ports,
        )
        logger.info("  UDP destination: :%s", self.udp_dst_port)
        if self.enable_df_probe:
            logger.info(
                "  DF payload: %s bytes",
                self.df_payload_size,
            )
        else:
            logger.info("  DF probe: disabled")
        logger.info("  Startup jitter: %.3fs", self.startup_jitter_s)
        if self.min_interval == self.max_interval:
            logger.info("  Fixed cycle interval: %ss", self.min_interval)
        else:
            logger.info("  Interval range: %s-%ss", self.min_interval, self.max_interval)
        logger.info("  Batch size: %s, timeout: %ss", self.batch_size, self.batch_timeout)
        logger.info("  InfluxDB mode: %s", "enabled" if self.use_influxdb else "disabled")

    def next_probe_batch(self) -> tuple[list[dict], dict[str, int]]:
        cycle = self.probe_cycle
        destination_batch_index, port_batch_index = _probe_batch_indices(
            cycle,
            self.destination_batch_count,
            self.rtt_port_batch_count,
            self.rtt_port_phase,
        )
        tasks = _rotating_destination_batch(
            self.tasks,
            self.destination_batch_size,
            destination_batch_index,
            self.destination_phase,
        )
        metadata = {
            "probe_cycle": cycle,
            "destination_batch_index": destination_batch_index,
            "port_batch_index": port_batch_index,
            "coverage_epoch": cycle // self.coverage_epoch_cycles,
            "coverage_epoch_cycles": self.coverage_epoch_cycles,
        }
        self.probe_cycle += 1
        return tasks, metadata
