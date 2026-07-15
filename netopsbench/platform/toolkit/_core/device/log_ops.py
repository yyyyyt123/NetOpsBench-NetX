"""Log-oriented device toolkit operations."""

from __future__ import annotations

from netopsbench.platform.observability.influxdb import query_flux

from ..common import ToolResult
from .log_parsers import get_device_logs_fallback, parse_influx_syslog_rows


class LogOpsMixin:
    def get_device_logs(
        self, device: str, time_range_minutes: int = 30, severity=None, include_raw: bool = False
    ) -> ToolResult:
        try:
            safe_device = self._validate_device_name(device)
            safe_minutes = max(1, min(int(time_range_minutes), 24 * 60))
            severity_filter = ""
            if severity:
                safe_severity = severity.lower()
                if safe_severity not in self._SEVERITY_OPTIONS:
                    return ToolResult(success=False, data=None, error=f"Invalid severity: {severity}")
                severity_filter = f'|> filter(fn: (r) => r.severity == "{safe_severity}")'
            else:
                safe_severity = None
            query = f"""\nfrom(bucket: "{self.influxdb_bucket}")\n  |> range(start: -{safe_minutes}m)\n  |> filter(fn: (r) => r._measurement == "syslog")\n  |> filter(fn: (r) => r.source == "{safe_device}")\n  |> filter(fn: (r) => r._field == "message")\n  {severity_filter}\n  |> sort(columns: ["_time"], desc: true)\n  |> limit(n: 100)\n"""
            result = query_flux(self.influxdb_url, self.influxdb_token, self.influxdb_org, query)
            if result.status != "ok":
                return ToolResult(success=False, data=None, error=f"InfluxDB query failed: {result.error}")
            structured_logs = parse_influx_syslog_rows(result.text)
            if structured_logs:
                data = {
                    "device": safe_device,
                    "time_range_minutes": safe_minutes,
                    "severity": safe_severity,
                    "source": "influxdb",
                    "logs": structured_logs,
                }
                if include_raw:
                    data["raw_csv"] = result.text
                return ToolResult(success=True, data=data)
            fallback_logs = get_device_logs_fallback(
                self, safe_device, time_range_minutes=safe_minutes, severity=safe_severity
            )
            warning = None
            source = "influxdb"
            if fallback_logs:
                warning = "No syslog entries were available in InfluxDB for this device/time window; returning a live container log fallback."
                source = "container_logs_fallback"
            data = {
                "device": safe_device,
                "time_range_minutes": safe_minutes,
                "severity": safe_severity,
                "source": source,
                "logs": fallback_logs,
                "warning": warning,
            }
            if include_raw:
                data["raw_csv"] = result.text
            return ToolResult(success=True, data=data)
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))
