"""Helpers for generated SONiC ``config_db.json`` startup artifacts."""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Any


def _sort_key(interface_name: str) -> tuple[int, int | str]:
    if interface_name.startswith("Ethernet"):
        suffix = interface_name.removeprefix("Ethernet")
        if suffix.isdigit():
            return (0, int(suffix))
    return (1, interface_name)


def load_configdb_payload(path: str | Path) -> dict[str, Any]:
    payload_path = Path(path)
    if not payload_path.exists():
        return {}
    try:
        with payload_path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def interface_names_from_payload(payload: dict[str, Any]) -> list[str]:
    configdb_interfaces = payload.get("INTERFACE")
    if not isinstance(configdb_interfaces, dict):
        return []
    names = {str(name).split("|", 1)[0] for name in configdb_interfaces.keys()}
    return sorted(names, key=_sort_key)


def interface_names_from_configdb(path: str | Path) -> list[str]:
    return interface_names_from_payload(load_configdb_payload(path))


def interface_names_for_config(config_path: str | Path) -> list[str]:
    return interface_names_from_configdb(config_path)


def interface_networks_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    configdb_interfaces = payload.get("INTERFACE")
    networks: dict[str, str] = {}
    if not isinstance(configdb_interfaces, dict):
        return networks
    for key in configdb_interfaces.keys():
        if "|" not in str(key):
            continue
        interface_name, cidr = str(key).split("|", 1)
        try:
            networks[interface_name] = str(ipaddress.ip_interface(cidr).network)
        except ValueError:
            continue
    return networks


def interface_networks_for_config(config_path: str | Path) -> dict[str, str]:
    return interface_networks_from_payload(load_configdb_payload(config_path))
