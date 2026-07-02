"""
Topology Generator - Generate DCN network topologies for Containerlab.

Supports generating 2-tier CLOS topologies with configurable scale.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import shutil
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_SONIC_VS_IMAGE = "yyyyyt123/netopsbench-sonic-vs-202505-telemetry:202505-telemetry"
SONIC_PLATFORM = "x86_64-kvm_x86_64-r0"
SONIC_HWSKU = "Force10-S6000"
SONIC_HWSKU_PATH = f"/usr/share/sonic/device/{SONIC_PLATFORM}/{SONIC_HWSKU}"
SONIC_PORT_CONFIG_PATH = f"{SONIC_HWSKU_PATH}/port_config.ini"
SONIC_LANEMAP_PATH = f"{SONIC_HWSKU_PATH}/lanemap.ini"
SONIC_BASE_CONFIG_DB = Path(__file__).with_name("sonic_vs_base_config_db.json")
SONIC_START_WRAPPER_SOURCE = Path(__file__).with_name("sonic_start.sh")


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class TopologyConfig:
    """Configuration for topology generation."""

    name: str = "dcn"
    num_spines: int = 2
    num_leafs: int = 2
    clients_per_leaf: int = 1
    nos_kind: str = "sonic-vs"
    nos_image: str = DEFAULT_SONIC_VS_IMAGE
    client_image: str = "yyyyyt123/netopsbench-client:python3"
    mgmt_ipv4_subnet: str = "172.20.20.0/24"
    mgmt_network_name: str | None = None
    collector_ip: str | None = None
    spine_asn: int = 65001
    leaf_asn_start: int = 65011
    scale_name: str | None = None


# Predefined topology scales
TOPOLOGY_SCALES = {
    "xs": TopologyConfig(num_spines=2, num_leafs=2, clients_per_leaf=1, scale_name="xs"),
    "small": TopologyConfig(num_spines=2, num_leafs=4, clients_per_leaf=2, scale_name="small"),
    "medium": TopologyConfig(num_spines=4, num_leafs=8, clients_per_leaf=2, scale_name="medium"),
    "large": TopologyConfig(num_spines=4, num_leafs=16, clients_per_leaf=4, scale_name="large"),
    "xlarge": TopologyConfig(
        num_spines=16,
        num_leafs=128,
        clients_per_leaf=1,
        mgmt_ipv4_subnet="172.20.20.0/23",
        scale_name="xlarge",
    ),
}


class TopologyGenerator:
    """
    Generates DCN network topologies for Containerlab.

    Creates CLOS topology YAML files and device configurations.
    """

    def __init__(self, config: TopologyConfig | None = None, output_dir: str | None = None):
        self.config = config or TopologyConfig()
        self.output_dir = output_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
            "generated_topology",
        )
        self.mgmt_network = ipaddress.ip_network(self.config.mgmt_ipv4_subnet, strict=False)
        if self.mgmt_network.version != 4:
            raise ValueError(f"Only IPv4 management subnets are supported: {self.config.mgmt_ipv4_subnet}")
        self.mgmt_network_name = self.config.mgmt_network_name or f"clab-mgmt-{self.config.name}"

        # Calculate IP offsets to avoid collisions
        self.spine_mgmt_offset = 10
        self.leaf_mgmt_offset = self.spine_mgmt_offset + self.config.num_spines
        self.client_mgmt_offset = self.leaf_mgmt_offset + self.config.num_leafs
        self._validate_address_space()
        self.collector_ip = self.config.collector_ip or self._default_collector_ip()
        self._validate_collector_ip(self.collector_ip)

        self.syslog_collector = os.getenv("NETOPSBENCH_SYSLOG_COLLECTOR", self.collector_ip)
        self.sflow_collector = os.getenv("NETOPSBENCH_SFLOW_COLLECTOR", self.syslog_collector)
        self.sflow_port = int(os.getenv("NETOPSBENCH_SFLOW_PORT", "6343"))
        self.sflow_polling_interval = int(os.getenv("NETOPSBENCH_SFLOW_POLLING_INTERVAL", "20"))
        self.sflow_sample_rate = int(os.getenv("NETOPSBENCH_SFLOW_SAMPLE_RATE", "1000"))
        self.sflow_sample_direction = os.getenv("NETOPSBENCH_SFLOW_SAMPLE_DIRECTION", "ingress")
        self.enable_interface_sflow = _parse_bool_env(
            "NETOPSBENCH_ENABLE_INTERFACE_SFLOW",
            default=self.config.scale_name != "xlarge",
        )
        self.snmp_community = os.getenv("NETOPSBENCH_SNMP_COMMUNITY", "public")
        self._validate_collector_ip(self.syslog_collector)

    def _mgmt_host_ip(self, host_offset: int) -> str:
        """Return a deterministic management IPv4 address from the configured subnet."""
        if host_offset <= 0:
            raise ValueError(f"Management host offset must be positive, got {host_offset}")

        candidate = ipaddress.ip_address(int(self.mgmt_network.network_address) + host_offset)
        if candidate not in self.mgmt_network:
            raise ValueError(f"Management host offset {host_offset} falls outside subnet {self.mgmt_network}")
        if candidate in {self.mgmt_network.network_address, self.mgmt_network.broadcast_address}:
            raise ValueError(f"Management host offset {host_offset} resolves to reserved address {candidate}")
        return str(candidate)

    def _default_collector_ip(self) -> str:
        """Pick a collector IP outside allocated device management addresses."""
        last_device_offset = self.client_mgmt_offset + (self.config.num_leafs * self.config.clients_per_leaf)
        preferred = int(self.mgmt_network.network_address) + 200
        preferred_ip = ipaddress.ip_address(preferred)
        if last_device_offset < 200 and preferred_ip in self.mgmt_network and preferred_ip not in {
            self.mgmt_network.network_address,
            self.mgmt_network.broadcast_address,
        }:
            return str(preferred_ip)

        fallback = ipaddress.ip_address(int(self.mgmt_network.network_address) + last_device_offset + 1)
        if fallback not in self.mgmt_network or fallback in {
            self.mgmt_network.network_address,
            self.mgmt_network.broadcast_address,
        }:
            fallback = ipaddress.ip_address(int(self.mgmt_network.broadcast_address) - 1)
        if fallback in {self.mgmt_network.network_address, self.mgmt_network.broadcast_address}:
            raise ValueError(f"Management subnet too small for collector IP: {self.mgmt_network}")
        return str(fallback)

    def _validate_address_space(self) -> None:
        """Validate deterministic IPv4 schemes before writing generated files."""
        if not 1 <= self.config.num_spines <= 255:
            raise ValueError(f"num_spines must fit in one IPv4 octet (1-255), got {self.config.num_spines}")
        if not 1 <= self.config.num_leafs <= 155:
            raise ValueError(
                "num_leafs must fit the client subnet scheme 192.168.(100+leaf).0/24 "
                f"(1-155), got {self.config.num_leafs}"
            )
        if not 1 <= self.config.clients_per_leaf <= 64:
            raise ValueError(
                "clients_per_leaf must fit the per-leaf /30 allocation (1-64), "
                f"got {self.config.clients_per_leaf}"
            )

        last_device_offset = self.client_mgmt_offset + (self.config.num_leafs * self.config.clients_per_leaf)
        self._mgmt_host_ip(last_device_offset)

    def _allocated_mgmt_ips(self) -> set[ipaddress.IPv4Address]:
        offsets = set()
        offsets.update(self.spine_mgmt_offset + i for i in range(1, self.config.num_spines + 1))
        offsets.update(self.leaf_mgmt_offset + i for i in range(1, self.config.num_leafs + 1))
        offsets.update(
            self.client_mgmt_offset + i
            for i in range(1, (self.config.num_leafs * self.config.clients_per_leaf) + 1)
        )
        return {ipaddress.ip_address(int(self.mgmt_network.network_address) + offset) for offset in offsets}

    def _validate_collector_ip(self, collector_ip: str) -> None:
        candidate = ipaddress.ip_address(collector_ip)
        if candidate not in self.mgmt_network:
            raise ValueError(f"Collector IP {collector_ip} falls outside management subnet {self.mgmt_network}")
        if candidate in {self.mgmt_network.network_address, self.mgmt_network.broadcast_address}:
            raise ValueError(f"Collector IP {collector_ip} is reserved in management subnet {self.mgmt_network}")
        if candidate in self._allocated_mgmt_ips():
            raise ValueError(f"Collector IP {collector_ip} overlaps a generated device management address")

    def _spine_leaf_ips(self, spine_idx: int, leaf_idx: int) -> tuple[str, str]:
        """Return deterministic underlay IPs for one spine-leaf /30."""
        return f"10.{spine_idx}.{leaf_idx}.1", f"10.{spine_idx}.{leaf_idx}.2"

    def generate(self) -> dict:
        """
        Generate complete topology including YAML and metadata.

        Returns:
            Dictionary containing topology metadata and file paths
        """
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(os.path.join(self.output_dir, "configs"), exist_ok=True)
        os.makedirs(os.path.join(self.output_dir, "configs", "sonic"), exist_ok=True)
        os.makedirs(os.path.join(self.output_dir, "configs", "frr"), exist_ok=True)
        os.makedirs(os.path.join(self.output_dir, "configs", "pingmesh"), exist_ok=True)
        self._generated_frr_paths: list[str] = []
        sonic_start_wrapper = self._write_sonic_start_wrapper()

        # Generate topology structure
        topology = self._generate_topology_structure()

        # Generate YAML file
        yaml_path = self._write_yaml(topology)

        # Generate device configs
        config_paths = self._generate_device_configs()

        # Generate metadata
        metadata = self._generate_metadata()
        metadata_path = os.path.join(self.output_dir, "topology.json")
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)

        return {
            "yaml_file": yaml_path,
            "metadata_file": metadata_path,
            "config_files": config_paths,
            "startup_config_files": config_paths,
            "sonic_start_wrapper_file": sonic_start_wrapper,
            "frr_config_files": list(self._generated_frr_paths),
            "metadata": metadata,
        }

    def _generate_topology_structure(self) -> dict:
        """Generate Containerlab topology YAML structure."""
        topology = {
            "name": self.config.name,
            "mgmt": {
                "network": self.mgmt_network_name,
                "ipv4-subnet": str(self.mgmt_network),
            },
            "topology": {
                "kinds": {
                    self.config.nos_kind: {
                        "image": self.config.nos_image,
                        "binds": [
                            "configs/sonic/__clabNodeName__/config_db.json:/etc/sonic/config_db.json:rw",
                            f"configs/sonic/__clabNodeName__/port_config.ini:{SONIC_PORT_CONFIG_PATH}:rw",
                            f"configs/sonic/__clabNodeName__/lanemap.ini:{SONIC_LANEMAP_PATH}:rw",
                            "configs/sonic/start.sh:/usr/bin/start.sh:ro",
                            "configs/frr/__clabNodeName__.conf:/etc/frr/frr.conf:rw",
                        ],
                    },
                    "linux": {
                        "image": self.config.client_image,
                        "binds": ["configs/pingmesh:/tmp/pingmesh:ro"],
                    },
                },
                "nodes": {},
                "links": [],
            },
        }

        nodes = topology["topology"]["nodes"]
        links = topology["topology"]["links"]

        # Generate spine nodes
        for i in range(1, self.config.num_spines + 1):
            spine_name = f"spine{i}"
            nodes[spine_name] = {
                "kind": self.config.nos_kind,
                "group": "spine",
                "mgmt-ipv4": self._mgmt_host_ip(self.spine_mgmt_offset + i),
            }

        # Generate leaf nodes and clients
        for i in range(1, self.config.num_leafs + 1):
            leaf_name = f"leaf{i}"
            nodes[leaf_name] = {
                "kind": self.config.nos_kind,
                "group": "leaf",
                "mgmt-ipv4": self._mgmt_host_ip(self.leaf_mgmt_offset + i),
            }

            # Generate clients for this leaf
            for j in range(1, self.config.clients_per_leaf + 1):
                client_idx = (i - 1) * self.config.clients_per_leaf + j
                client_name = f"client{client_idx}"
                # Point-to-point /30 subnet addressing
                # Each client gets: 192.168.{octet}.{(j-1)*4 + 2}/30
                # Gateway is:       192.168.{octet}.{(j-1)*4 + 1}
                octet = self._client_subnet_octet(i)
                subnet_base = (j - 1) * 4
                client_ip = f"192.168.{octet}.{subnet_base + 2}"
                gateway = f"192.168.{octet}.{subnet_base + 1}"

                nodes[client_name] = {
                    "kind": "linux",
                    "group": "client",
                    "mgmt-ipv4": self._mgmt_host_ip(self.client_mgmt_offset + client_idx),
                    "exec": [
                        "ip link set dev eth1 mtu 9232",
                        f"ip addr add {client_ip}/30 dev eth1",
                        f"ip route add 192.168.0.0/16 via {gateway}",
                        "mkdir -p /var/log/pingmesh",
                        "iperf3 -s -D",
                        "ethtool -K eth1 rx off tx off tso off gso off gro off sg off tx-udp-segmentation off",
                    ],
                }

        # Generate spine-leaf links
        interface_counter = {}
        for spine_idx in range(1, self.config.num_spines + 1):
            spine_name = f"spine{spine_idx}"
            for leaf_idx in range(1, self.config.num_leafs + 1):
                leaf_name = f"leaf{leaf_idx}"

                # Get next available interface for each device
                spine_if = interface_counter.get(spine_name, 0) + 1
                leaf_if = interface_counter.get(leaf_name, 0) + 1
                interface_counter[spine_name] = spine_if
                interface_counter[leaf_name] = leaf_if

                links.append({"endpoints": [f"{spine_name}:eth{spine_if}", f"{leaf_name}:eth{leaf_if}"], "mtu": 9232})

        # Generate leaf-client links
        for i in range(1, self.config.num_leafs + 1):
            leaf_name = f"leaf{i}"
            for j in range(1, self.config.clients_per_leaf + 1):
                client_idx = (i - 1) * self.config.clients_per_leaf + j
                client_name = f"client{client_idx}"

                leaf_if = interface_counter.get(leaf_name, 0) + 1
                interface_counter[leaf_name] = leaf_if

                links.append({"endpoints": [f"{leaf_name}:eth{leaf_if}", f"{client_name}:eth1"], "mtu": 9232})

        return topology

    def _write_yaml(self, topology: dict) -> str:
        """Write topology YAML file."""
        yaml_path = os.path.join(self.output_dir, f"{self.config.name}.clab.yaml")

        # Add header comment
        header = f"""# {self.config.name.upper()} Topology Configuration
# Generated by NetOpsBench Topology Generator
# Scale: {self.config.num_spines} spines, {self.config.num_leafs} leafs, {self.config.clients_per_leaf} clients/leaf
#
# Features:
# - BGP with ECMP
# - Observability: Syslog, gNMI, sFlow

"""
        with open(yaml_path, "w") as f:
            f.write(header)
            yaml.dump(topology, f, default_flow_style=False, sort_keys=False)

        return yaml_path

    def _write_sonic_start_wrapper(self) -> str:
        """Stage the SONiC startup wrapper used by all sonic-vs nodes."""
        target = Path(self.output_dir) / "configs" / "sonic" / "start.sh"
        if not SONIC_START_WRAPPER_SOURCE.is_file():
            raise FileNotFoundError(f"SONiC startup wrapper source not found: {SONIC_START_WRAPPER_SOURCE}")
        shutil.copy2(SONIC_START_WRAPPER_SOURCE, target)
        target.chmod(0o755)
        return str(target)

    def _generate_device_configs(self) -> list[str]:
        """Generate device configuration files."""
        config_paths = []

        # Generate spine configs
        for i in range(1, self.config.num_spines + 1):
            config_path = self._generate_spine_config(i)
            config_paths.append(config_path)

        # Generate leaf configs
        for i in range(1, self.config.num_leafs + 1):
            config_path = self._generate_leaf_config(i)
            config_paths.append(config_path)

        return config_paths

    def _sonic_port_name(self, eth_index: int) -> str:
        """Map Linux eth index (eth1, eth2) to SONiC port name (Ethernet0, Ethernet4, ...)."""
        return f"Ethernet{(eth_index - 1) * 4}"

    def _sonic_port_attrs(self, port_idx: int) -> dict[str, str]:
        """Return CONFIG_DB PORT attributes for zero-based SONiC front-panel port index."""
        eth = port_idx * 4
        first_lane = eth + 1
        lanes = ",".join(str(first_lane + offset) for offset in range(4))
        return {
            "alias": f"fortyGigE0/{eth}",
            "index": str(port_idx),
            "lanes": lanes,
            "speed": "100000",
            "subport": "0",
            "admin_status": "up",
        }

    def _port_entries(self, required_ports: int) -> dict[str, dict[str, str]]:
        """Return complete CONFIG_DB PORT rows for a generated SONiC-VS node."""
        return {f"Ethernet{port_idx * 4}": self._sonic_port_attrs(port_idx) for port_idx in range(required_ports)}

    def _port_config_ini(self, required_ports: int) -> str:
        lines = ["# name          lanes             alias             index       speed"]
        for port_idx in range(required_ports):
            name = f"Ethernet{port_idx * 4}"
            attrs = self._sonic_port_attrs(port_idx)
            lines.append(
                f"{name:<15} {attrs['lanes']:<17} {attrs['alias']:<17} {attrs['index']:<11} {attrs['speed']}"
            )
        return "\n".join(lines) + "\n"

    def _lanemap_ini(self, required_ports: int) -> str:
        lines = []
        for port_idx in range(required_ports):
            linux_if = f"eth{port_idx + 1}"
            attrs = self._sonic_port_attrs(port_idx)
            lines.append(f"{linux_if}:{attrs['lanes']}")
        return "\n".join(lines) + "\n"

    def _device_mac(self, device: str) -> str:
        digest = hashlib.sha256(device.encode("utf-8")).digest()
        return "02:" + ":".join(f"{byte:02x}" for byte in digest[:5])

    def _load_base_config_db(self) -> dict[str, Any]:
        with SONIC_BASE_CONFIG_DB.open(encoding="utf-8") as handle:
            return deepcopy(json.load(handle))

    def _add_configdb_interface(
        self,
        interfaces: dict[str, dict[str, Any]],
        interface: str,
        cidr: str,
    ) -> None:
        entry = interfaces.setdefault(interface, {"admin_status": "up", "ips": []})
        entry["admin_status"] = "up"
        ips = entry.setdefault("ips", [])
        if cidr not in ips:
            ips.append(cidr)

    def _build_config_db(
        self,
        device: str,
        required_ports: int,
        interfaces: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        config_db = self._load_base_config_db()
        metadata = config_db.setdefault("DEVICE_METADATA", {}).setdefault("localhost", {})
        metadata["hostname"] = device
        metadata["hwsku"] = SONIC_HWSKU
        metadata["platform"] = SONIC_PLATFORM
        metadata["mac"] = self._device_mac(device)

        ports = self._port_entries(required_ports)
        config_db["PORT"] = ports
        config_db["CABLE_LENGTH"] = {"AZURE": {name: "0m" for name in ports}}
        config_db["BREAKOUT_CFG"] = {name: {"brkout_mode": "1x100G[40G]"} for name in ports}
        config_db["SYSLOG_SERVER"] = {"telegraf": {"server": self.syslog_collector}}

        interface_table: dict[str, dict[str, str]] = {}
        for interface_name in sorted(interfaces, key=lambda name: int(name[8:]) if name.startswith("Ethernet") else 0):
            interface_table[interface_name] = {}
            for cidr in interfaces[interface_name].get("ips", []):
                interface_table[f"{interface_name}|{cidr}"] = {}
        config_db["INTERFACE"] = interface_table

        if self.enable_interface_sflow:
            config_db["SFLOW"] = {
                "global": {
                    "admin_state": "up",
                    "agent_id": "mgmt0",
                    "polling_interval": str(self.sflow_polling_interval),
                    "sample_direction": self.sflow_sample_direction,
                }
            }
            config_db["SFLOW_COLLECTOR"] = {
                "telegraf": {
                    "collector_ip": self.sflow_collector,
                    "collector_port": str(self.sflow_port),
                }
            }
            config_db["SFLOW_SESSION"] = {
                interface_name: {
                    "admin_state": "up",
                    "sample_rate": str(self.sflow_sample_rate),
                }
                for interface_name in interface_table
                if "|" not in interface_name
            }

        return config_db

    def _frr_config(
        self,
        device: str,
        local_as: int,
        router_id: str,
        neighbors: list[tuple[str, int]],
        networks: list[str],
    ) -> str:
        lines = [
            "frr version 10.3",
            "frr defaults traditional",
            f"hostname {device}",
            "log syslog informational",
            "no ipv6 forwarding",
            "service integrated-vtysh-config",
            "!",
            "route-map RM-ALLOW permit 10",
            "!",
            f"router bgp {local_as}",
            f" bgp router-id {router_id}",
            " no bgp ebgp-requires-policy",
        ]
        for peer_ip, remote_as in neighbors:
            lines.append(f" neighbor {peer_ip} remote-as {remote_as}")
        lines.extend([" !", " address-family ipv4 unicast"])
        for peer_ip, _remote_as in neighbors:
            lines.append(f"  neighbor {peer_ip} activate")
            lines.append(f"  neighbor {peer_ip} route-map RM-ALLOW out")
        for prefix in networks:
            lines.append(f"  network {prefix} route-map RM-ALLOW")
        lines.extend([" exit-address-family", "!", "line vty", "!"])
        return "\n".join(lines) + "\n"

    def _write_device_startup_artifacts(
        self,
        device: str,
        required_ports: int,
        interfaces: dict[str, dict[str, Any]],
        local_as: int,
        router_id: str,
        neighbors: list[tuple[str, int]],
        networks: list[str],
    ) -> str:
        sonic_dir = Path(self.output_dir) / "configs" / "sonic" / device
        sonic_dir.mkdir(parents=True, exist_ok=True)
        config_db_path = sonic_dir / "config_db.json"
        config_db_path.write_text(
            json.dumps(self._build_config_db(device, required_ports, interfaces), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (sonic_dir / "port_config.ini").write_text(self._port_config_ini(required_ports), encoding="utf-8")
        (sonic_dir / "lanemap.ini").write_text(self._lanemap_ini(required_ports), encoding="utf-8")

        frr_path = Path(self.output_dir) / "configs" / "frr" / f"{device}.conf"
        frr_path.write_text(
            self._frr_config(device, local_as, router_id, neighbors, networks),
            encoding="utf-8",
        )
        self._generated_frr_paths.append(str(frr_path))
        return str(config_db_path)

    def _client_subnet_octet(self, leaf_idx: int) -> int:
        """Return client subnet /24 octet for a leaf, avoiding spine-link collisions."""
        return 100 + leaf_idx

    def _generate_spine_config(self, spine_idx: int) -> str:
        """Generate configuration for a spine switch."""
        spine_name = f"spine{spine_idx}"
        router_id = f"10.0.0.{spine_idx}"
        configdb_interfaces: dict[str, dict[str, Any]] = {}
        neighbors: list[tuple[str, int]] = []

        # Add interfaces and BGP neighbors for each leaf
        for leaf_idx in range(1, self.config.num_leafs + 1):
            interface = self._sonic_port_name(leaf_idx)
            spine_ip, leaf_ip = self._spine_leaf_ips(spine_idx, leaf_idx)
            leaf_asn = self.config.leaf_asn_start + leaf_idx - 1

            self._add_configdb_interface(configdb_interfaces, interface, f"{spine_ip}/30")
            neighbors.append((leaf_ip, leaf_asn))

        return self._write_device_startup_artifacts(
            device=spine_name,
            required_ports=self.config.num_leafs,
            interfaces=configdb_interfaces,
            local_as=self.config.spine_asn,
            router_id=router_id,
            neighbors=neighbors,
            networks=[],
        )

    def _generate_leaf_config(self, leaf_idx: int) -> str:
        """Generate configuration for a leaf switch."""
        leaf_name = f"leaf{leaf_idx}"
        router_id = f"10.0.0.{10 + leaf_idx}"
        leaf_asn = self.config.leaf_asn_start + leaf_idx - 1
        configdb_interfaces: dict[str, dict[str, Any]] = {}
        required_ports = self.config.num_spines + self.config.clients_per_leaf
        neighbors: list[tuple[str, int]] = []
        networks: list[str] = []

        # Add interfaces to spines
        for spine_idx in range(1, self.config.num_spines + 1):
            interface = self._sonic_port_name(spine_idx)
            spine_ip, leaf_ip = self._spine_leaf_ips(spine_idx, leaf_idx)

            self._add_configdb_interface(configdb_interfaces, interface, f"{leaf_ip}/30")
            neighbors.append((spine_ip, self.config.spine_asn))

        # Add client-facing interfaces (one per client)
        # Use point-to-point /30 subnets for each client to avoid subnet overlap
        # Each client gets a dedicated /30 subnet from the leaf's /24 address space
        for client_idx in range(1, self.config.clients_per_leaf + 1):
            client_interface_idx = self.config.num_spines + client_idx
            # Calculate /30 subnet: 192.168.{octet}.{(client_idx-1)*4}/30
            # Gateway IP is first usable IP in /30: 192.168.{octet}.{(client_idx-1)*4 + 1}
            octet = self._client_subnet_octet(leaf_idx)
            subnet_base = (client_idx - 1) * 4
            gateway_ip = subnet_base + 1
            interface = self._sonic_port_name(client_interface_idx)
            self._add_configdb_interface(configdb_interfaces, interface, f"192.168.{octet}.{gateway_ip}/30")
            networks.append(f"192.168.{octet}.{subnet_base}/30")

        return self._write_device_startup_artifacts(
            device=leaf_name,
            required_ports=required_ports,
            interfaces=configdb_interfaces,
            local_as=leaf_asn,
            router_id=router_id,
            neighbors=neighbors,
            networks=networks,
        )

    def _generate_metadata(self) -> dict:
        """Generate topology metadata JSON."""
        devices = {"spines": [], "leafs": [], "clients": []}

        # Spines
        for i in range(1, self.config.num_spines + 1):
            devices["spines"].append(
                {
                    "name": f"spine{i}",
                    "mgmt_ip": self._mgmt_host_ip(self.spine_mgmt_offset + i),
                    "router_id": f"10.0.0.{i}",
                    "asn": self.config.spine_asn,
                }
            )

        # Leafs
        for i in range(1, self.config.num_leafs + 1):
            devices["leafs"].append(
                {
                    "name": f"leaf{i}",
                    "mgmt_ip": self._mgmt_host_ip(self.leaf_mgmt_offset + i),
                    "router_id": f"10.0.0.{10 + i}",
                    "asn": self.config.leaf_asn_start + i - 1,
                    "client_subnet": f"192.168.{self._client_subnet_octet(i)}.0/24",
                }
            )

        # Clients
        for i in range(1, self.config.num_leafs + 1):
            for j in range(1, self.config.clients_per_leaf + 1):
                client_idx = (i - 1) * self.config.clients_per_leaf + j
                # Point-to-point /30 subnet addressing
                octet = self._client_subnet_octet(i)
                subnet_base = (j - 1) * 4
                client_ip = f"192.168.{octet}.{subnet_base + 2}"
                devices["clients"].append(
                    {
                        "name": f"client{client_idx}",
                        "mgmt_ip": self._mgmt_host_ip(self.client_mgmt_offset + client_idx),
                        "data_ip": client_ip,
                        "leaf": f"leaf{i}",
                        "rack": f"rack{i}",
                    }
                )

        # Links
        links = []
        for spine_idx in range(1, self.config.num_spines + 1):
            for leaf_idx in range(1, self.config.num_leafs + 1):
                links.append({"endpoints": [f"spine{spine_idx}", f"leaf{leaf_idx}"], "type": "spine-leaf"})

        for i in range(1, self.config.num_leafs + 1):
            for j in range(1, self.config.clients_per_leaf + 1):
                client_idx = (i - 1) * self.config.clients_per_leaf + j
                links.append({"endpoints": [f"leaf{i}", f"client{client_idx}"], "type": "leaf-client"})

        metadata = {
            "name": self.config.name,
            "management": {
                "network": self.mgmt_network_name,
                "ipv4_subnet": str(self.mgmt_network),
            },
            "collector": {
                "ipv4": self.syslog_collector,
                "sflow_port": self.sflow_port,
            },
            "defaults": {
                "link_mtu": 9232,
                "sonic_port_mtu": 9100,
            },
            "mtu_semantics": {
                "link_mtu_scope": "containerlab/client link MTU",
                "sonic_port_mtu_scope": "SONiC front-panel interface MTU",
                "note": "Do not compare link_mtu 9232 directly against healthy SONiC port MTU 9100 when diagnosing faults.",
            },
            "scale": {
                "name": self.config.scale_name,
                "num_spines": self.config.num_spines,
                "num_leafs": self.config.num_leafs,
                "clients_per_leaf": self.config.clients_per_leaf,
                "total_clients": self.config.num_leafs * self.config.clients_per_leaf,
                "total_devices": self.config.num_spines + self.config.num_leafs,
            },
            "devices": devices,
            "links": links,
            "routing": {
                "protocol": "BGP",
                "spine_asn": self.config.spine_asn,
                "leaf_asn_range": f"{self.config.leaf_asn_start}-{self.config.leaf_asn_start + self.config.num_leafs - 1}",
                "ecmp": True,
                "bfd": True,
            },
        }
        if self.config.scale_name:
            metadata["topology_scale"] = self.config.scale_name
        if self.config.scale_name == "xlarge":
            metadata["pingmesh"] = {"max_dests_per_client": 16}
        return metadata


def generate_topology(
    scale: str = "xs",
    output_dir: str | None = None,
    name: str | None = None,
    mgmt_subnet: str | None = None,
    mgmt_network: str | None = None,
    collector_ip: str | None = None,
) -> dict:
    """
    Convenience function to generate a topology of specified scale.

    Args:
        scale: Topology scale ('xs', 'small', 'medium', 'large')
        output_dir: Output directory path

    Returns:
        Dictionary with generated file paths and metadata
    """
    if scale not in TOPOLOGY_SCALES:
        raise ValueError(f"Unknown scale: {scale}. Available: {list(TOPOLOGY_SCALES.keys())}")

    base_config = TOPOLOGY_SCALES[scale]
    config = TopologyConfig(
        name=name or base_config.name,
        num_spines=base_config.num_spines,
        num_leafs=base_config.num_leafs,
        clients_per_leaf=base_config.clients_per_leaf,
        nos_kind=base_config.nos_kind,
        nos_image=base_config.nos_image,
        client_image=base_config.client_image,
        mgmt_ipv4_subnet=mgmt_subnet or base_config.mgmt_ipv4_subnet,
        mgmt_network_name=mgmt_network or base_config.mgmt_network_name,
        collector_ip=collector_ip or base_config.collector_ip,
        spine_asn=base_config.spine_asn,
        leaf_asn_start=base_config.leaf_asn_start,
        scale_name=scale,
    )
    generator = TopologyGenerator(config, output_dir)
    return generator.generate()
