"""Post-deploy SONiC activation for preseeded startup artifacts.

CLI usage::

    python -m netopsbench.platform.runtime.apply_configs <topology_dir> [max_parallel] [lab_name]

Programmatic usage::

    from netopsbench.platform.runtime.apply_configs import apply_configs
    result = apply_configs("/path/to/topology_dir", max_parallel=32)
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from netopsbench.config import config
from netopsbench.platform.topology.topology_utils import clab_container_name
from netopsbench.platform.utils.proc import safe_run, sudo_prefix


_REQUIRED_SONIC_SERVICES = ("redis-server", "orchagent", "zebra")


@dataclass
class ApplyResult:
    """Summary of configuration application."""

    succeeded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    durations: dict[str, float] = field(default_factory=dict)
    readiness_durations: dict[str, float] = field(default_factory=dict)
    activation_durations: dict[str, float] = field(default_factory=dict)
    elapsed_seconds: float = 0.0


def _resolve_lab_name(topology_dir: str, lab_name: str | None) -> str:
    """Resolve the lab name from argument, topology metadata, or clab YAML."""
    if lab_name:
        return lab_name

    metadata_file = os.path.join(topology_dir, "topology.json")
    if os.path.isfile(metadata_file):
        with open(metadata_file, encoding="utf-8") as fh:
            data = json.load(fh)
        name = (data.get("name") or "").strip()
        if name:
            return name

    # Fall back to parsing the clab YAML header
    clab_files = sorted(glob.glob(os.path.join(topology_dir, "*.clab.y*ml")))
    for clab_file in clab_files:
        with open(clab_file, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip()
                    if name:
                        return name
                    break

    return "dcn"


def _preseed_config_file(topology_dir: str, device: str) -> str:
    return os.path.join(topology_dir, "configs", "sonic", device, "config_db.json")


def _startup_wrapper_file(topology_dir: str) -> str:
    return os.path.join(topology_dir, "configs", "sonic", "start.sh")


def _device_sort_key(device: str) -> tuple[int, int | str]:
    for rank, prefix in enumerate(("spine", "leaf")):
        if device.startswith(prefix):
            suffix = device[len(prefix) :]
            return (rank, int(suffix) if suffix.isdigit() else suffix)
    return (2, device)


def _discover_devices(topology_dir: str) -> list[str]:
    preseed_root = Path(topology_dir) / "configs" / "sonic"
    if not preseed_root.exists():
        return []
    devices = [
        device_dir.name
        for device_dir in preseed_root.iterdir()
        if device_dir.is_dir() and (device_dir / "config_db.json").is_file()
    ]
    return sorted(devices, key=_device_sort_key)


def _configdb_ready(prefix: list[str], container: str) -> bool:
    try:
        configdb_ret = safe_run(
            [
                *prefix,
                "docker",
                "exec",
                container,
                "sonic-cfggen",
                "-d",
                "-v",
                "DEVICE_METADATA.localhost.hostname",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return False
    return configdb_ret.returncode == 0


def _startup_status(prefix: list[str], container: str) -> str | None:
    try:
        ret = safe_run(
            [*prefix, "docker", "exec", container, "supervisorctl", "status", "start.sh"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return None
    parts = str(ret.stdout or ret.stderr or "").split()
    return parts[1].upper() if len(parts) >= 2 else None


def _required_services_ready(prefix: list[str], container: str) -> bool:
    try:
        ret = safe_run(
            [*prefix, "docker", "exec", container, "supervisorctl", "status", *_REQUIRED_SONIC_SERVICES],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return False
    output = str(ret.stdout or ret.stderr or "")
    states: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1].upper()
    return all(states.get(service) == "RUNNING" for service in _REQUIRED_SONIC_SERVICES)


def _wait_for_sonic(device: str, container: str, max_tries: int | None = None, delay: int = 5) -> bool:
    """Block until SONiC control-plane and ConfigDB commands are responsive."""
    max_tries = max_tries or config.sonic_wait_tries
    prefix = sudo_prefix()
    for _ in range(max_tries):
        _startup_status(prefix, container)
        configdb_ready = _configdb_ready(prefix, container)
        if not configdb_ready:
            time.sleep(delay)
            continue

        try:
            vtysh_ret = safe_run(
                [*prefix, "docker", "exec", container, "vtysh", "-c", "show version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            time.sleep(delay)
            continue

        if vtysh_ret.returncode != 0:
            time.sleep(delay)
            continue

        if not _required_services_ready(prefix, container):
            time.sleep(delay)
            continue

        if _configdb_ready(prefix, container):
            return True
        time.sleep(delay)
    return False


def _activate_preseed_device(prefix: list[str], container: str) -> bool:
    command = (
        "set -e; "
        "sysctl -w net.ipv4.fib_multipath_hash_policy=1 >/dev/null || true; "
        "supervisorctl start bgpd >/dev/null 2>&1 || true; "
        "if [ -s /etc/frr/frr.conf ]; then "
        "vtysh -b >/tmp/netopsbench-vtysh-batch.log 2>&1; "
        "fi; "
        "mkdir -p /var/run/telemetry /var/log/telemetry || true; "
        "if ! pgrep -x telemetry >/dev/null 2>&1; then "
        "nohup /usr/sbin/telemetry -port 50051 -noTLS -client_auth none "
        ">/var/log/telemetry/telemetry.log 2>&1 </dev/null & "
        "fi"
    )
    ret = safe_run(
        [*prefix, "docker", "exec", container, "bash", "-lc", command],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return ret.returncode == 0


def _apply_single_device(
    device: str,
    topology_dir: str,
    lab_name: str,
) -> tuple[str, bool, str, float, float, float]:
    """Activate one preseeded device after Containerlab has started it."""
    started_at = time.monotonic()
    container = clab_container_name(lab_name, device)
    prefix = sudo_prefix()
    preseed_config = _preseed_config_file(topology_dir, device)
    startup_wrapper = _startup_wrapper_file(topology_dir)

    if not os.path.isfile(preseed_config):
        return (device, False, f"preseed config not found: {preseed_config}", time.monotonic() - started_at, 0.0, 0.0)

    if not os.path.isfile(startup_wrapper):
        return (
            device,
            False,
            f"startup wrapper not found: {startup_wrapper}; regenerate topology before deploying",
            time.monotonic() - started_at,
            0.0,
            0.0,
        )

    readiness_started = time.monotonic()
    if not _wait_for_sonic(device, container):
        readiness_elapsed = time.monotonic() - readiness_started
        return (
            device,
            False,
            f"SONiC services not ready after {readiness_elapsed:.1f}s",
            time.monotonic() - started_at,
            readiness_elapsed,
            0.0,
        )
    readiness_elapsed = time.monotonic() - readiness_started

    activation_started = time.monotonic()
    if _activate_preseed_device(prefix, container):
        activation_elapsed = time.monotonic() - activation_started
        return (
            device,
            True,
            "post-deploy activation",
            time.monotonic() - started_at,
            readiness_elapsed,
            activation_elapsed,
        )
    activation_elapsed = time.monotonic() - activation_started
    return (
        device,
        False,
        "post-deploy activation failed",
        time.monotonic() - started_at,
        readiness_elapsed,
        activation_elapsed,
    )


def apply_configs(
    topology_dir: str,
    max_parallel: int = 32,
    lab_name: str | None = None,
) -> ApplyResult:
    """Activate all preseeded SONiC devices in *topology_dir*."""
    lab_name = _resolve_lab_name(topology_dir, lab_name)

    # Discover devices
    devices = _discover_devices(topology_dir)

    if not devices:
        raise RuntimeError(f"No preseed config_db.json files found in {topology_dir}/configs/sonic/")

    print("=== SONiC Post-Deploy Activation ===")
    print(f"Topology directory: {topology_dir}")
    print(f"Max parallel: {max_parallel}")
    print(f"Lab name: {lab_name}")
    print()
    print(f"Found {len(devices)} devices to configure: {' '.join(devices)}")
    print()
    print("[1/2] Activating devices in parallel...")

    start = time.monotonic()
    result = ApplyResult()

    with ThreadPoolExecutor(max_workers=max_parallel) as executor:
        futures = {executor.submit(_apply_single_device, device, topology_dir, lab_name): device for device in devices}
        for future in as_completed(futures):
            device, success, msg, elapsed, readiness_elapsed, activation_elapsed = future.result()
            result.durations[device] = elapsed
            result.readiness_durations[device] = readiness_elapsed
            result.activation_durations[device] = activation_elapsed
            if success:
                print(
                    f"  ok [{device}] {msg}; ready={readiness_elapsed:.1f}s "
                    f"activate={activation_elapsed:.1f}s total={elapsed:.1f}s"
                )
                result.succeeded.append(device)
            else:
                print(
                    f"  x  [{device}] {msg}; ready={readiness_elapsed:.1f}s "
                    f"activate={activation_elapsed:.1f}s total={elapsed:.1f}s"
                )
                result.failed.append(device)

    result.elapsed_seconds = time.monotonic() - start
    print()
    print(f"Activated devices in {result.elapsed_seconds:.0f}s")
    if result.durations:
        slowest = sorted(result.durations.items(), key=lambda item: item[1], reverse=True)[:10]
        slowest_text = ", ".join(f"{device}={elapsed:.1f}s" for device, elapsed in slowest)
        print(f"Slowest devices: {slowest_text}")
        slowest_ready = sorted(result.readiness_durations.items(), key=lambda item: item[1], reverse=True)[:10]
        slowest_ready_text = ", ".join(f"{device}={elapsed:.1f}s" for device, elapsed in slowest_ready)
        print(f"Slowest readiness: {slowest_ready_text}")
        slowest_activation = sorted(result.activation_durations.items(), key=lambda item: item[1], reverse=True)[:10]
        slowest_activation_text = ", ".join(f"{device}={elapsed:.1f}s" for device, elapsed in slowest_activation)
        print(f"Slowest activation: {slowest_activation_text}")
    print()

    # [2/2] Quick control-plane verification
    print("[2/2] Quick control-plane verification...")
    prefix = sudo_prefix()
    for role in ("spine", "leaf"):
        candidates = [d for d in devices if d.startswith(role)]
        if candidates:
            container = clab_container_name(lab_name, candidates[0])
            safe_run(
                [*prefix, "docker", "exec", container, "vtysh", "-c", "show ip bgp summary"],
                capture_output=True,
                check=False,
                timeout=30,
            )
            print(f"  checked {candidates[0]}")

    print()
    print("=== SONiC Activation Complete ===")
    if result.failed:
        print(f"WARNING: {len(result.failed)} device(s) failed: {', '.join(result.failed)}")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point (backward-compatible with shell script args)."""
    args = list(argv if argv is not None else sys.argv[1:])

    topology_dir = args[0] if len(args) > 0 else "generated_topology"
    max_parallel_str = args[1] if len(args) > 1 else "32"
    max_parallel = int(max_parallel_str) if max_parallel_str else 32
    lab_name = args[2] if len(args) > 2 else None

    try:
        result = apply_configs(topology_dir, max_parallel, lab_name)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
