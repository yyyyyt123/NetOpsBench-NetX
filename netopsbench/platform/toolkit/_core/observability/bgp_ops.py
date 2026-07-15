"""Historical BGP event discovery from centralized telemetry."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from ..common import ToolResult
from .pingmesh_scope import parse_iso8601_timestamp

_VALID_STATES = {"non_established", "all", "established"}


def _timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return parse_iso8601_timestamp(str(value), "_time")
    except ValueError:
        return None


def _bool(value: object) -> bool:
    return value is True or str(value).lower() == "true"


class BgpOpsMixin:
    def query_bgp_events(
        self,
        start_time: str | None = None,
        end_time: str | None = None,
        time_range_minutes: int = 10,
        device: str | None = None,
        peer: str | None = None,
        role: str | None = None,
        state: str = "non_established",
        limit: int = 100,
    ) -> ToolResult:
        """Find BGP session transitions without logging in to every router."""
        try:
            if state not in _VALID_STATES:
                raise ValueError(f"Invalid state: {state}. Expected one of {sorted(_VALID_STATES)}")
            safe_device = self._validate_device_name(device) if device else None
            safe_peer = self._validate_ip_address(peer, "peer") if peer else None
            safe_limit = max(1, min(int(limit), 500))
            roles = self._bgp_device_roles()
            if role and role not in set(roles.values()):
                raise ValueError(f"Invalid or unavailable role: {role}")
            if safe_device and safe_device not in roles:
                raise ValueError(f"Unknown routing device: {safe_device}")

            scope = self._resolve_pingmesh_time_scope(time_range_minutes, start_time, end_time)
            filters = self._bgp_flux_filters(safe_device, safe_peer)
            device_filter = self._bgp_flux_filters(safe_device, None)
            neighbor_query = f"""
from(bucket: "{self.influxdb_bucket}")
{scope["range_clause"]}  |> filter(fn: (r) => r._measurement == "bgp_neighbors")
  |> filter(fn: (r) => r.topology_id == "{self._flux_string(self.topology_id)}")
{filters}  |> pivot(rowKey: ["_time", "_measurement", "source", "neighbor_address"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
"""

            collection_query = f"""
from(bucket: "{self.influxdb_bucket}")
{scope["range_clause"]}  |> filter(fn: (r) => r._measurement == "bgp_collection")
  |> filter(fn: (r) => r.topology_id == "{self._flux_string(self.topology_id)}")
{device_filter}  |> pivot(rowKey: ["_time", "_measurement", "source"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
"""
            window_rows = self._query_influx_rows(neighbor_query, require_value=False)
            window_rows.extend(self._query_influx_rows(collection_query, require_value=False))
            prior_rows: list[dict[str, Any]] = []
            if scope["mode"] == "absolute":
                prior_query = f"""
from(bucket: "{self.influxdb_bucket}")
  |> range(start: -30d, stop: time(v: "{scope["start_time"]}"))
  |> filter(fn: (r) => r._measurement == "bgp_neighbors")
  |> filter(fn: (r) => r.topology_id == "{self._flux_string(self.topology_id)}")
{filters}  |> pivot(rowKey: ["_time", "_measurement", "source", "neighbor_address"], columnKey: ["_field"], valueColumn: "_value")
  |> group(columns: ["source", "neighbor_address"])
  |> last(column: "_time")
"""
                prior_rows = self._query_influx_rows(prior_query, require_value=False)
            rows = [dict(row, _scope_prior=True) for row in prior_rows] + window_rows
            events = self._build_bgp_events(rows, scope, roles, safe_device, safe_peer, role, state)
            truncated = len(events) > safe_limit
            returned = events[:safe_limit]
            freshness = self._bgp_freshness_seconds(rows, scope)
            return ToolResult(
                success=True,
                data={
                    "status": "ok",
                    "time_scope": {
                        key: "episode_context" if key == "source" and value == "toolkit_default" else value
                        for key, value in scope.items()
                        if key != "range_clause"
                    },
                    "events": returned,
                    "returned_events": len(returned),
                    "truncated": truncated,
                    "data_freshness_seconds": freshness,
                },
            )
        except Exception as exc:
            return ToolResult(success=False, data=None, error=str(exc))

    @staticmethod
    def _flux_string(value: object) -> str:
        return str(value or "").replace("\\", "\\\\").replace('"', '\\"')

    def _bgp_flux_filters(self, device: str | None, peer: str | None) -> str:
        rendered = ""
        if device:
            rendered += f'  |> filter(fn: (r) => r.source == "{self._flux_string(device)}")\n'
        if peer:
            rendered += f'  |> filter(fn: (r) => r.neighbor_address == "{self._flux_string(peer)}")\n'
        return rendered

    def _bgp_device_roles(self) -> dict[str, str]:
        devices = self.topology_metadata.get("devices", []) if self.topology_metadata else []
        if isinstance(devices, dict):
            return {
                str(item["name"]): str(role).removesuffix("s")
                for role, entries in devices.items()
                for item in entries
                if role != "clients" and item.get("name")
            }
        return {
            str(item["name"]): str(item.get("role", "unknown"))
            for item in devices
            if item.get("name") and item.get("role") != "client"
        }

    def _build_bgp_events(
        self,
        rows: list[dict[str, Any]],
        scope: dict[str, Any],
        roles: dict[str, str],
        device: str | None,
        peer: str | None,
        role: str | None,
        state_filter: str,
    ) -> list[dict[str, Any]]:
        sessions: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        collections: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            source = str(row.get("source") or "")
            if not source or source not in roles or (role and roles[source] != role):
                continue
            measurement = row.get("_measurement")
            if measurement == "bgp_collection":
                collections[source].append(row)
            elif measurement == "bgp_neighbors" and row.get("neighbor_address"):
                sessions[(source, str(row["neighbor_address"]))].append(row)

        events: list[dict[str, Any]] = []
        for (source, neighbor), samples in sessions.items():
            if device and source != device or peer and neighbor != peer:
                continue
            samples.sort(key=lambda row: _timestamp(row.get("_time")) or datetime.min.replace(tzinfo=UTC))
            prior = [row for row in samples if row.get("_scope_prior")]
            window = [row for row in samples if not row.get("_scope_prior")]
            if not window:
                continue
            states = [str(row.get("session_state") or "UNKNOWN").upper() for row in window]
            previous = str(prior[-1].get("session_state") or "UNKNOWN").upper() if prior else None
            latest = states[-1]
            saw_down = any(value != "ESTABLISHED" for value in states)
            transition_states = ([previous] if previous else []) + states
            down_transition = any(
                before == "ESTABLISHED" and after != "ESTABLISHED"
                for before, after in zip(transition_states, transition_states[1:], strict=False)
            )
            recovery_transition = any(
                before != "ESTABLISHED" and after == "ESTABLISHED"
                for before, after in zip(transition_states, transition_states[1:], strict=False)
            )
            if down_transition and recovery_transition:
                event_type = "session_flap"
            elif down_transition:
                event_type = "session_down"
            elif recovery_transition:
                event_type = "session_recovered"
            elif saw_down:
                event_type = "non_established_observed"
            elif state_filter == "all":
                event_type = "established_observed"
            else:
                continue
            if state_filter == "non_established" and not saw_down:
                continue
            if state_filter == "established" and latest != "ESTABLISHED":
                continue
            unique_states = list(dict.fromkeys(states))
            events.append(
                {
                    "device": source,
                    "role": roles[source],
                    "peer": neighbor,
                    "peer_as": window[-1].get("asn"),
                    "event_type": event_type,
                    "previous_state": previous,
                    "latest_state": latest,
                    "states_observed": unique_states,
                    "first_seen": window[0].get("_time"),
                    "last_seen": window[-1].get("_time"),
                    "sample_count": len(window),
                    "prefixes_before": prior[-1].get("prefixes_received") if prior else None,
                    "prefixes_after": window[-1].get("prefixes_received"),
                }
            )

        for source, samples in collections.items():
            latest = samples[-1]
            if not _bool(latest.get("collection_ok")):
                events.append(
                    {
                        "device": source,
                        "role": roles[source],
                        "peer": None,
                        "event_type": "collection_gap",
                        "previous_state": None,
                        "latest_state": None,
                        "states_observed": [],
                        "first_seen": samples[0].get("_time"),
                        "last_seen": latest.get("_time"),
                        "sample_count": len(samples),
                        "error_type": latest.get("error_type"),
                    }
                )
        selected_devices = {
            name
            for name, device_role in roles.items()
            if (not device or name == device) and (not role or device_role == role)
        }
        missing_devices = selected_devices - collections.keys() if not peer or device else set()
        for source in missing_devices:
            events.append(
                {
                    "device": source,
                    "role": roles[source],
                    "peer": None,
                    "event_type": "collection_gap",
                    "previous_state": None,
                    "latest_state": None,
                    "states_observed": [],
                    "first_seen": None,
                    "last_seen": None,
                    "sample_count": 0,
                    "error_type": "no_collection_samples",
                }
            )
        return sorted(events, key=lambda event: (str(event.get("last_seen") or ""), event["device"]), reverse=True)

    @staticmethod
    def _bgp_freshness_seconds(rows: list[dict[str, Any]], scope: dict[str, Any]) -> int | None:
        timestamps = [_timestamp(row.get("_time")) for row in rows]
        latest = max((value for value in timestamps if value), default=None)
        if latest is None:
            return None
        reference = (
            parse_iso8601_timestamp(scope["end_time"], "end_time") if scope["mode"] == "absolute" else datetime.now(UTC)
        )
        return max(0, round((reference - latest).total_seconds()))
