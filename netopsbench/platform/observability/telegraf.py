#!/usr/bin/env python3
"""
Dynamic Telegraf Configuration Generator
Generates telegraf.conf from template based on topology metadata
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from netopsbench.config import config, repo_root
from netopsbench.logging_utils import get_logger
from netopsbench.platform.topology.configdb_payload import interface_names_from_configdb

logger = get_logger(__name__)

REPO_ROOT = repo_root()


def _port_key(name: str) -> int:
    return int(name.replace("Ethernet", "")) if name.startswith("Ethernet") else 0


def _port_names_for_count(count: int) -> list[str]:
    return [f"Ethernet{idx * 4}" for idx in range(max(0, int(count)))]


def _device_config_interfaces(topology_dir: Path, device_name: str) -> list[str]:
    config_path = topology_dir / "configs" / "sonic" / device_name / "config_db.json"
    return interface_names_from_configdb(config_path)


def _role_port_names(topology_dir: Path, devices: list[dict], fallback_count: int) -> list[str]:
    port_names: set[str] = set()
    for device in devices:
        port_names.update(_device_config_interfaces(topology_dir, str(device.get("name", ""))))
    if not port_names:
        port_names.update(_port_names_for_count(fallback_count))
    return sorted(port_names, key=_port_key)


def _gnmi_addresses(devices: list[dict]) -> list[str]:
    addresses: list[str] = []
    for device in devices:
        mgmt_ip = str(device["mgmt_ip"]).split("/")[0]
        addresses.append(f'"{mgmt_ip}:{device["gnmi_port"]}"')
    return addresses


def _render_gnmi_subscriptions(
    port_names: list[str],
    gnmi_subscription_mode: str,
    gnmi_sample_interval: str,
) -> str:
    subscriptions = []
    for port in port_names:
        subscription_lines = [
            "  [[inputs.gnmi.subscription]]",
            '    name = "interfaces"',
            f'    path = "COUNTERS/{port}"',
            f'    subscription_mode = "{gnmi_subscription_mode}"',
        ]
        if gnmi_subscription_mode == "sample":
            subscription_lines.append(f'    sample_interval = "{gnmi_sample_interval}"')
        subscriptions.append("\n".join(subscription_lines) + "\n")
    return "\n".join(subscriptions)


def _render_gnmi_input(
    role: str,
    devices: list[dict],
    port_names: list[str],
    *,
    gnmi_username: str,
    gnmi_password: str,
    gnmi_encoding: str,
    gnmi_target: str,
    gnmi_subscription_mode: str,
    gnmi_sample_interval: str,
) -> str:
    if not devices or not port_names:
        return ""
    addresses = ",\n       ".join(_gnmi_addresses(devices))
    subscriptions = _render_gnmi_subscriptions(port_names, gnmi_subscription_mode, gnmi_sample_interval)
    return f'''# gNMI role: {role}
[[inputs.gnmi]]
  addresses = [
       {addresses}
  ]
  username = "{gnmi_username}"
  password = "{gnmi_password}"
  encoding = "{gnmi_encoding}"
  redial = "10s"
  path_guessing_strategy = "subscription"
  tls_enable = false
  insecure_skip_verify = true
  target = "{gnmi_target}"

{subscriptions}'''


def update_telegraf_config(
    topology_file: str,
    output_file: str | None = None,
    influxdb_url: str | None = None,
    influxdb_token: str | None = None,
    influxdb_org: str | None = None,
    influxdb_bucket: str | None = None,
    topology_id: str | None = None,
):
    """
    Generate telegraf.conf from template using topology metadata.

    Args:
        topology_file: Path to topology.json file
        output_file: Optional output path for generated telegraf.conf
        influxdb_url: Optional InfluxDB URL override
        influxdb_token: Optional InfluxDB token override
        influxdb_org: Optional InfluxDB organization override
        influxdb_bucket: Optional InfluxDB bucket override
        topology_id: Optional topology identifier tag override

    Generates telegraf.conf with:
    - {{GNMI_INPUTS}}: Role-scoped SONiC gNMI inputs and subscriptions
    - {{IP_MAPPINGS}}: Processor rules to map IPs to hostnames
    """
    # Read topology metadata
    topology_path = Path(topology_file)
    if not topology_path.exists():
        logger.error("Topology file not found: %s", topology_file)
        sys.exit(1)

    with open(topology_path) as f:
        topo = json.load(f)

    # gNMI settings (override with env vars if needed)
    gnmi_port = int(os.getenv("SONIC_GNMI_PORT", os.getenv("GNMI_PORT", "50051")))
    gnmi_username = os.getenv("SONIC_GNMI_USERNAME", os.getenv("GNMI_USERNAME", "admin"))
    gnmi_password = os.getenv("SONIC_GNMI_PASSWORD", os.getenv("GNMI_PASSWORD", ""))
    if not gnmi_password:
        logger.warning("SONIC_GNMI_PASSWORD is not set; gNMI telemetry collection will likely fail.")
    gnmi_encoding = os.getenv("SONIC_GNMI_ENCODING", os.getenv("GNMI_ENCODING", "json_ietf"))
    gnmi_target = os.getenv("SONIC_GNMI_TARGET", os.getenv("GNMI_TARGET", "COUNTERS_DB"))
    gnmi_subscription_mode = (
        os.getenv(
            "SONIC_GNMI_SUBSCRIPTION_MODE",
            os.getenv("GNMI_SUBSCRIPTION_MODE", "sample"),
        )
        .strip()
        .lower()
    )
    if gnmi_subscription_mode not in {"on_change", "sample", "target_defined"}:
        logger.error(
            "unsupported gNMI subscription mode %r; expected one of on_change, sample, target_defined",
            gnmi_subscription_mode,
        )
        sys.exit(1)
    gnmi_sample_interval = os.getenv("SONIC_GNMI_SAMPLE_INTERVAL", os.getenv("GNMI_SAMPLE_INTERVAL", "10s")).strip()
    resolved_influxdb_url = influxdb_url or os.getenv("NETOPSBENCH_INFLUXDB_URL", "http://influxdb:8086")
    resolved_influxdb_token = influxdb_token or os.getenv(
        "NETOPSBENCH_INFLUXDB_TOKEN",
        config.influxdb_token,
    )
    resolved_influxdb_org = influxdb_org or os.getenv("NETOPSBENCH_INFLUXDB_ORG", config.influxdb_org)
    resolved_influxdb_bucket = influxdb_bucket or os.getenv("NETOPSBENCH_INFLUXDB_BUCKET", config.influxdb_bucket)
    resolved_topology_id = (
        topology_id
        or os.getenv("NETOPSBENCH_TOPOLOGY_ID")
        or str(topo.get("name") or "").strip()
        or topology_path.parent.name
    )

    # sFlow settings (override with env vars if needed)
    sflow_service_address = os.getenv("SONIC_SFLOW_SERVICE_ADDRESS", os.getenv("SFLOW_SERVICE_ADDRESS", "udp://:6343"))
    if "://" not in sflow_service_address:
        sflow_service_address = f"udp://{sflow_service_address}"

    # Extract device information
    spine_devices = []
    for spine in topo["devices"]["spines"]:
        spine_devices.append(
            {
                "name": spine["name"],
                "mgmt_ip": spine["mgmt_ip"].split("/")[0],  # Remove CIDR notation
                "gnmi_port": gnmi_port,
            }
        )

    leaf_devices = []
    for leaf in topo["devices"]["leafs"]:
        leaf_devices.append(
            {
                "name": leaf["name"],
                "mgmt_ip": leaf["mgmt_ip"].split("/")[0],  # Remove CIDR notation
                "gnmi_port": gnmi_port,
            }
        )
    devices = spine_devices + leaf_devices

    logger.info("Found %d network devices:", len(devices))
    for d in devices:
        logger.info("  - %s: %s", d["name"], d["mgmt_ip"])

    # Derive SONiC front-panel ports used by each device role (Ethernet0/4/8...).
    scale = topo.get("scale", {})
    num_spines = int(scale.get("num_spines", 0))
    num_leafs = int(scale.get("num_leafs", 0))
    clients_per_leaf = int(scale.get("clients_per_leaf", 0))
    topology_dir = topology_path.parent
    spine_port_names = _role_port_names(topology_dir, spine_devices, fallback_count=num_leafs)
    leaf_port_names = _role_port_names(
        topology_dir,
        leaf_devices,
        fallback_count=num_spines + clients_per_leaf,
    )

    gnmi_inputs_str = "\n".join(
        block
        for block in [
            _render_gnmi_input(
                "spine",
                spine_devices,
                spine_port_names,
                gnmi_username=gnmi_username,
                gnmi_password=gnmi_password,
                gnmi_encoding=gnmi_encoding,
                gnmi_target=gnmi_target,
                gnmi_subscription_mode=gnmi_subscription_mode,
                gnmi_sample_interval=gnmi_sample_interval,
            ),
            _render_gnmi_input(
                "leaf",
                leaf_devices,
                leaf_port_names,
                gnmi_username=gnmi_username,
                gnmi_password=gnmi_password,
                gnmi_encoding=gnmi_encoding,
                gnmi_target=gnmi_target,
                gnmi_subscription_mode=gnmi_subscription_mode,
                gnmi_sample_interval=gnmi_sample_interval,
            ),
        ]
        if block
    )

    # Generate IP to hostname mappings (processor rules)
    ip_mappings = []
    mapping_keys = ["source", "agent_host", "agent_ip", "agent", "agent_address", "address", "target"]
    for key in mapping_keys:
        for d in devices:
            ip_mappings.append(f'''
  [[processors.regex.tags]]
    key = "{key}"
    pattern = "^{d['mgmt_ip']}$"
    replacement = "{d['name']}"''')
    ip_mappings_str = "".join(ip_mappings)

    # Read template file
    template_file = REPO_ROOT / "observability" / "telegraf.conf.template"

    if not template_file.exists():
        logger.error("Template file not found: %s", template_file)
        logger.error("Please create telegraf.conf.template first")
        sys.exit(1)

    with open(template_file, encoding="utf-8") as f:
        template = f.read()

    # Replace placeholders
    rendered_config = template.replace("{{GNMI_INPUTS}}", gnmi_inputs_str)
    rendered_config = rendered_config.replace("{{SFLOW_SERVICE_ADDRESS}}", sflow_service_address)
    rendered_config = rendered_config.replace("{{IP_MAPPINGS}}", ip_mappings_str)
    rendered_config = rendered_config.replace("{{INFLUXDB_URL}}", resolved_influxdb_url)
    rendered_config = rendered_config.replace("{{INFLUXDB_TOKEN}}", resolved_influxdb_token)
    rendered_config = rendered_config.replace("{{INFLUXDB_ORG}}", resolved_influxdb_org)
    rendered_config = rendered_config.replace("{{INFLUXDB_BUCKET}}", resolved_influxdb_bucket)
    rendered_config = rendered_config.replace("{{TOPOLOGY_ID}}", resolved_topology_id)

    # Validate all placeholders have been replaced
    unreplaced = re.findall(r"\{\{[A-Z_]+\}\}", rendered_config)
    if unreplaced:
        logger.warning(
            "Unreplaced template placeholders: %s",
            ", ".join(sorted(set(unreplaced))),
        )

    # Write output file
    output_path = Path(output_file) if output_file else (REPO_ROOT / "observability" / "telegraf.conf")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(rendered_config)

    logger.info("Telegraf configuration updated: %s", output_path)
    logger.info("  - %d devices configured", len(devices))
    logger.info("  - gNMI targets: %d", len(devices))
    logger.info("  - gNMI spine subscriptions: %d", len(spine_port_names))
    logger.info("  - gNMI leaf subscriptions: %d", len(leaf_port_names))
    logger.info("  - gNMI subscription mode: %s", gnmi_subscription_mode)
    logger.info("  - IP mappings: %d", len(ip_mappings))
    logger.info("  - InfluxDB bucket: %s", resolved_influxdb_bucket)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate telegraf.conf from topology metadata")
    parser.add_argument("topology_file", help="Path to topology.json")
    parser.add_argument("--output", help="Output telegraf config path")
    parser.add_argument("--influxdb-url", help="InfluxDB URL override")
    parser.add_argument("--influxdb-token", help="InfluxDB token override")
    parser.add_argument("--influxdb-org", help="InfluxDB organization override")
    parser.add_argument("--bucket", help="InfluxDB bucket override")
    parser.add_argument("--topology-id", help="Topology identifier override")
    args = parser.parse_args()

    return update_telegraf_config(
        args.topology_file,
        output_file=args.output,
        influxdb_url=args.influxdb_url,
        influxdb_token=args.influxdb_token,
        influxdb_org=args.influxdb_org,
        influxdb_bucket=args.bucket,
        topology_id=args.topology_id,
    )


if __name__ == "__main__":
    raise SystemExit(main())
