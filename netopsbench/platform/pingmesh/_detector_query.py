"""Influx/topology helpers for Pingmesh anomaly detector."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from io import StringIO

from netopsbench.platform.observability.influxdb import query_flux

_SNAPSHOT_FIELDS = (
    "rtt_min",
    "rtt_avg",
    "rtt_max",
    "packets_sent",
    "packets_lost",
    "df_packets_sent",
    "df_packets_lost",
    "df_mtu_drops",
    "probe_cycle",
    "destination_batch_index",
    "port_batch_index",
    "rtt_ports_active",
    "rtt_ports_total",
)
_SNAPSHOT_QUERY_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class SnapshotQueryResult:
    status: str
    rows: list[dict]
    error: str | None = None


def _endpoint_name(endpoint) -> str | None:
    if isinstance(endpoint, dict):
        raw = endpoint.get("device") or endpoint.get("name")
    else:
        raw = endpoint
    if not isinstance(raw, str):
        return None
    return raw.split(":", 1)[0].strip() or None


class DetectorQueryMixin:
    @staticmethod
    def _safe_flux_string(value: str) -> str:
        return str(value).replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _parse_snapshot_csv(text: str) -> list[dict]:
        data_lines = [line for line in text.splitlines() if line and not line.startswith("#")]
        if not data_lines:
            return []
        rows = []
        for row in csv.DictReader(StringIO("\n".join(data_lines))):
            if row.get("_time") in (None, "", "_time"):
                continue
            parsed = {key: value for key, value in row.items() if key and value not in (None, "")}
            for field in _SNAPSHOT_FIELDS:
                if field not in parsed:
                    continue
                try:
                    parsed[field] = float(parsed[field])
                except (TypeError, ValueError):
                    parsed.pop(field, None)
            rows.append(parsed)
        return rows

    def _query_snapshot(self, start_time: str, end_time: str) -> SnapshotQueryResult:
        bucket = self._safe_flux_string(self.bucket)
        start = self._safe_flux_string(start_time)
        end = self._safe_flux_string(end_time)
        fields = " or ".join(f'r._field == "{field}"' for field in _SNAPSHOT_FIELDS)
        query = (
            f'from(bucket: "{bucket}")\n'
            f'  |> range(start: time(v: "{start}"), stop: time(v: "{end}"))\n'
            '  |> filter(fn: (r) => r._measurement == "pingmesh")\n'
            + self._topology_filter()
            + f"  |> filter(fn: (r) => {fields})\n"
            '  |> keep(columns: ["_time", "_field", "_value", "src_ip", "dst_ip", '
            '"src_name", "dst_name", "src_leaf", "dst_leaf", "path_type"])\n'
            '  |> pivot(rowKey: ["_time", "src_ip", "dst_ip", "src_name", "dst_name", '
            '"src_leaf", "dst_leaf", "path_type"], columnKey: ["_field"], valueColumn: "_value")\n'
        )
        result = query_flux(
            self.influxdb_url,
            self.token,
            self.org,
            query,
            timeout=_SNAPSHOT_QUERY_TIMEOUT_SECONDS,
        )
        if result.status != "ok":
            return SnapshotQueryResult(status="error", rows=[], error=result.error)
        return SnapshotQueryResult(status="ok", rows=self._parse_snapshot_csv(result.text))

    def _topology_filter(self) -> str:
        if not self.topology_id:
            return ""
        safe = str(self.topology_id).replace("\\", "\\\\").replace('"', '\\"')
        return f'  |> filter(fn: (r) => r.topology_id == "{safe}")\n'

    def _load_topology_metadata(self, metadata: dict) -> None:
        devices = metadata.get("devices", {}) if isinstance(metadata, dict) else {}
        clients = devices.get("clients", []) if isinstance(devices, dict) else []
        for client in clients:
            name = client.get("name")
            leaf = client.get("leaf")
            if isinstance(name, str) and isinstance(leaf, str) and name and leaf:
                self.client_to_leaf[name] = leaf

        # Build leaf/edge → core/spine mapping from topology links or role lists.
        spines = devices.get("spines", []) if isinstance(devices, dict) else []
        leafs = devices.get("leafs", []) if isinstance(devices, dict) else []
        cores = devices.get("cores", []) if isinstance(devices, dict) else []
        aggs = devices.get("aggs", []) if isinstance(devices, dict) else []
        edges = devices.get("edges", []) if isinstance(devices, dict) else []
        spine_names = [s.get("name") for s in spines if isinstance(s, dict) and s.get("name")]
        core_names = [c.get("name") for c in cores if isinstance(c, dict) and c.get("name")]
        agg_names = [a.get("name") for a in aggs if isinstance(a, dict) and a.get("name")]
        edge_names = [e.get("name") for e in edges if isinstance(e, dict) and e.get("name")]
        links = metadata.get("links", []) if isinstance(metadata, dict) else []
        if links:
            adjacency: dict[str, set[str]] = {}
            for link in links:
                if not isinstance(link, dict):
                    continue
                endpoints = link.get("endpoints", [])
                if len(endpoints) != 2:
                    continue
                a_name = _endpoint_name(endpoints[0])
                b_name = _endpoint_name(endpoints[1])
                if not a_name or not b_name:
                    continue
                adjacency.setdefault(a_name, set()).add(b_name)
                adjacency.setdefault(b_name, set()).add(a_name)

            if core_names and agg_names and edge_names:
                core_set = set(core_names)
                agg_set = set(agg_names)
                for edge_name in edge_names:
                    reachable_cores: set[str] = set()
                    for agg_name in adjacency.get(edge_name, set()) & agg_set:
                        reachable_cores.update(adjacency.get(agg_name, set()) & core_set)
                    if reachable_cores:
                        self.leaf_to_spines[edge_name] = sorted(reachable_cores)
            else:
                spine_set = set(spine_names)
                for leaf_name in [leaf.get("name") for leaf in leafs if isinstance(leaf, dict) and leaf.get("name")]:
                    peers = sorted(adjacency.get(leaf_name, set()) & spine_set)
                    if peers:
                        self.leaf_to_spines[leaf_name] = peers
