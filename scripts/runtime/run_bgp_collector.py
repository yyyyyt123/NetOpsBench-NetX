#!/usr/bin/env python3
"""Poll BGP summary from SONiC nodes and emit Influx line protocol snapshots."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from netopsbench.platform.toolkit._core.device.bgp_parsers import parse_bgp_summary  # noqa: E402

DEFAULT_BGP_COLLECTOR_MAX_BYTES = 128 * 1024 * 1024


def _docker_prefix() -> list[str]:
    if os.geteuid() == 0:
        return []
    return ["sudo", "-n"]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _escape_tag(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ").replace("=", "\\=")


def _escape_string_field(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def normalize_bgp_state(value: str | None) -> str:
    if not value:
        return "UNKNOWN"
    return str(value).strip().upper()


def _int_field(name: str, value: object) -> str | None:
    if value is None or value == "":
        return None
    return f"{name}={int(value)}i"


def build_bgp_lines(device: str, rows: Iterable[dict], timestamp_ns: int, topology_id: str = "") -> list[str]:
    lines: list[str] = []
    source = _escape_tag(device)
    topology_tag = _escape_tag(topology_id)
    for row in rows:
        neighbor = row.get("neighbor")
        if not neighbor:
            continue
        tags = [f"source={source}", f"neighbor_address={_escape_tag(str(neighbor))}"]
        if topology_tag:
            tags.append(f"topology_id={topology_tag}")
        fields = [f'session_state="{_escape_string_field(normalize_bgp_state(row.get("state")))}"']
        for key in ("asn", "prefixes_received", "msg_rcvd", "msg_sent", "in_q", "out_q"):
            field = _int_field(key, row.get(key))
            if field:
                fields.append(field)
        up_down = row.get("up_down")
        if up_down:
            fields.append(f'up_down="{_escape_string_field(str(up_down))}"')
        lines.append(f"bgp_neighbors,{','.join(tags)} {','.join(fields)} {timestamp_ns}")
    return lines


def _read_topology(metadata_file: Path) -> tuple[str, list[str]]:
    payload = json.loads(metadata_file.read_text(encoding="utf-8"))
    lab_name = str(payload.get("name") or "dcn").strip()
    devices = payload.get("devices", {}) or {}
    names = [item.get("name") for item in devices.get("spines", []) + devices.get("leafs", []) if item.get("name")]
    return lab_name, names


def _collect_device_bgp(
    lab_name: str,
    device: str,
    docker_prefix: list[str],
    timestamp_ns: int,
    topology_id: str,
) -> list[str]:
    container = f"clab-{lab_name}-{device}"  # matches clab_container_name() convention
    result = subprocess.run(
        [*docker_prefix, "docker", "exec", container, "vtysh", "-c", "show ip bgp summary"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        return []
    rows = parse_bgp_summary(result.stdout)
    return build_bgp_lines(device, rows, timestamp_ns, topology_id=topology_id)


def collect_bgp_lines(metadata_file: Path, timestamp_ns: int | None = None, parallelism: int = 1) -> list[str]:
    lab_name, devices = _read_topology(metadata_file)
    topology_id = os.environ.get("NETOPSBENCH_TOPOLOGY_ID", lab_name)
    docker_prefix = _docker_prefix()
    resolved_timestamp = time.time_ns() if timestamp_ns is None else int(timestamp_ns)
    workers = max(1, min(int(parallelism), len(devices) or 1))

    if workers == 1:
        device_lines = [
            _collect_device_bgp(lab_name, device, docker_prefix, resolved_timestamp, topology_id) for device in devices
        ]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            device_lines = list(
                executor.map(
                    lambda device: _collect_device_bgp(
                        lab_name,
                        device,
                        docker_prefix,
                        resolved_timestamp,
                        topology_id,
                    ),
                    devices,
                )
            )

    lines: list[str] = []
    for entries in device_lines:
        lines.extend(entries)
    return lines


def _write_lines(output_file: Path, lines: list[str], max_bytes: int = DEFAULT_BGP_COLLECTOR_MAX_BYTES) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    rendered = "\n".join(lines)
    if rendered:
        rendered += "\n"
    mode = "a"
    if max_bytes > 0 and output_file.exists():
        current_size = output_file.stat().st_size
        if current_size + len(rendered.encode("utf-8")) > max_bytes:
            mode = "w"
    with output_file.open(mode, encoding="utf-8") as handle:
        if rendered:
            handle.write(rendered)


def run_once(
    metadata_file: Path,
    output_file: Path,
    parallelism: int = 1,
    max_bytes: int = DEFAULT_BGP_COLLECTOR_MAX_BYTES,
) -> int:
    _write_lines(output_file, collect_bgp_lines(metadata_file, parallelism=parallelism), max_bytes=max_bytes)
    return 0


def run_loop(
    metadata_file: Path,
    output_file: Path,
    interval_seconds: float,
    parallelism: int = 1,
    max_bytes: int = DEFAULT_BGP_COLLECTOR_MAX_BYTES,
) -> int:
    stop_event = threading.Event()

    def _stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.touch(exist_ok=True)

    while not stop_event.is_set():
        try:
            _write_lines(
                output_file,
                collect_bgp_lines(metadata_file, parallelism=parallelism),
                max_bytes=max_bytes,
            )
        except Exception as exc:
            print(f"WARN: bgp collector iteration failed: {exc}", file=sys.stderr)
        stop_event.wait(max(1.0, interval_seconds))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Emit BGP neighbor snapshots as Influx line protocol")
    parser.add_argument("metadata_file", help="Path to topology.json")
    parser.add_argument("--output", required=True, help="Output line protocol file")
    parser.add_argument("--interval", type=float, default=10.0, help="Polling interval in seconds")
    parser.add_argument(
        "--parallelism",
        type=int,
        default=_env_int("NETOPSBENCH_BGP_COLLECTOR_PARALLELISM", 16),
        help="Concurrent docker exec workers per polling iteration",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=_env_int("NETOPSBENCH_BGP_COLLECTOR_MAX_BYTES", DEFAULT_BGP_COLLECTOR_MAX_BYTES),
        help="Maximum BGP line protocol file size before truncating to the latest snapshot; <=0 disables.",
    )
    parser.add_argument("--once", action="store_true", help="Collect one snapshot and exit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.once:
        return run_once(Path(args.metadata_file), Path(args.output), parallelism=args.parallelism, max_bytes=args.max_bytes)
    return run_loop(
        Path(args.metadata_file),
        Path(args.output),
        args.interval,
        parallelism=args.parallelism,
        max_bytes=args.max_bytes,
    )


if __name__ == "__main__":
    raise SystemExit(main())
