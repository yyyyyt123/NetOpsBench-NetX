"""Interface-oriented device toolkit operations."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

from netopsbench.platform.topology.topology_utils import is_network_device_name

from ..common import ToolResult
from .interface_parsers import merge_interface_tables, parse_ip_link_stats
from .text_parsers import parse_table


class InterfaceOpsMixin:
    def get_device_interfaces(
        self, device: str, format: str = "structured", interface: str | None = None, include_tc: bool = False
    ) -> ToolResult:
        if include_tc and not interface:
            return ToolResult(success=False, data=None, error="include_tc requires interface")
        result = self._get_device_interfaces(device, format)
        if not result.success:
            return result
        try:
            container = self._resolve_container(device)
            addresses = self._docker_exec(container, ["ip", "-j", "-4", "addr", "show"], timeout=30)
            if addresses.returncode:
                result.data["address_collection_error"] = addresses.stderr or "Address collection failed"
            else:
                rows = json.loads(addresses.stdout)
                result.data["addresses"] = {
                    row["ifname"]: [f"{a['local']}/{a['prefixlen']}" for a in row.get("addr_info", [])] for row in rows
                }
            if include_tc:
                safe_interface = self._validate_interface_name(interface)
                known = {
                    endpoint.interface
                    for link in self.manifest.links
                    for endpoint in link.endpoints
                    if endpoint.device == device
                }
                from netopsbench.platform.utils.interface_names import to_linux_interface, to_sonic_interface

                if safe_interface not in known and safe_interface not in {to_sonic_interface(name) for name in known}:
                    raise ValueError("Unknown data interface")
                linux_interface = to_linux_interface(safe_interface)
                qdisc = self._docker_exec(
                    container, ["tc", "-s", "-j", "qdisc", "show", "dev", linux_interface], timeout=30
                )
                if qdisc.returncode:
                    raise RuntimeError(qdisc.stderr or "qdisc collection failed")
                result.data["qdisc"] = {
                    "interface": safe_interface,
                    "linux_interface": linux_interface,
                    "rows": json.loads(qdisc.stdout),
                }
            result.data["collected_at"] = datetime.now(UTC).isoformat()
            return result
        except Exception as exc:
            return ToolResult(success=False, data=None, error=str(exc))

    def _get_device_interfaces(self, device: str, format: str = "structured") -> ToolResult:
        try:
            self._validate_device_name(device)
            safe_format = (format or "raw").lower()
            if safe_format not in {"raw", "structured", "summary", "both"}:
                return ToolResult(success=False, data=None, error=f"Invalid format: {format}")
            container = self._resolve_container(device)
            if is_network_device_name(device):
                brief_result = self._docker_exec(container, ["bash", "-lc", "show interfaces status"], timeout=30)
                detail_result = self._docker_exec(container, ["bash", "-lc", "show interfaces counters"], timeout=30)
                if brief_result.returncode != 0 and detail_result.returncode != 0:
                    fallback = self._docker_exec(container, ["ip", "-s", "link", "show"], timeout=30)
                    if fallback.returncode != 0:
                        return ToolResult(success=False, data=None, error=fallback.stderr)
                    raw_data = {
                        "device": device,
                        "brief": "show interfaces status unavailable; using ip link",
                        "detail": fallback.stdout,
                    }
                    structured = parse_ip_link_stats(fallback.stdout)
                    if safe_format == "summary":
                        return ToolResult(success=True, data=self._summarize_interface_data(device, structured))
                    if safe_format == "structured":
                        return ToolResult(success=True, data={"device": device, "interfaces": structured})
                    if safe_format == "both":
                        return ToolResult(
                            success=True, data={"device": device, "raw": raw_data, "structured": structured}
                        )
                    return ToolResult(success=True, data=raw_data)
                raw_data = {
                    "device": device,
                    "brief": brief_result.stdout if brief_result.returncode == 0 else "Unable to get brief",
                    "detail": detail_result.stdout if detail_result.returncode == 0 else "Unable to get details",
                }
                if safe_format in {"structured", "summary", "both"}:
                    status_rows = parse_table(brief_result.stdout if brief_result.returncode == 0 else "")
                    counter_rows = parse_table(detail_result.stdout if detail_result.returncode == 0 else "")
                    merged = merge_interface_tables(status_rows, counter_rows)
                    structured = {"device": device, "interfaces": merged}
                    if not merged:
                        structured["warning"] = "Unable to parse interface tables; check raw output."
                    if safe_format == "summary":
                        return ToolResult(success=True, data=self._summarize_interface_data(device, merged))
                    if safe_format == "structured":
                        return ToolResult(success=True, data=structured)
                    return ToolResult(success=True, data={"device": device, "raw": raw_data, "structured": structured})
                return ToolResult(success=True, data=raw_data)
            result = self._docker_exec(container, ["ip", "-s", "link", "show"], timeout=30)
            if result.returncode != 0:
                return ToolResult(success=False, data=None, error=result.stderr)
            raw_data = {"device": device, "interfaces": result.stdout}
            if safe_format in {"structured", "summary", "both"}:
                structured = parse_ip_link_stats(result.stdout)
                if safe_format == "summary":
                    return ToolResult(success=True, data=self._summarize_interface_data(device, structured))
                if safe_format == "structured":
                    return ToolResult(success=True, data={"device": device, "interfaces": structured})
                return ToolResult(success=True, data={"device": device, "raw": raw_data, "structured": structured})
            return ToolResult(success=True, data=raw_data)
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, data=None, error="Command timed out")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))

    @staticmethod
    def _summarize_interface_data(device: str, interfaces: list[dict]) -> dict:
        status_keys = {"admin", "oper", "status", "protocol", "state", "speed", "mtu"}
        signal_fragments = ("error", "err", "discard", "drop", "drp", "crc", "fcs", "carrier", "coll")
        summarized = []
        for entry in interfaces:
            if not isinstance(entry, dict):
                continue
            item = {"name": entry.get("name")}
            for key, value in entry.items():
                if key == "name":
                    continue
                key_text = str(key).lower()
                include = key in status_keys or any(fragment in key_text for fragment in signal_fragments)
                if include:
                    item[key] = value
            summarized.append({key: value for key, value in item.items() if value not in (None, "")})
        return {
            "device": device,
            "view": "summary",
            "interface_count": len(interfaces),
            "interfaces": summarized,
        }
