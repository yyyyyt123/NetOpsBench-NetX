"""Deploy Pingmesh agents to all client containers for a topology.

Replaces ``scripts/runtime/deploy_pingmesh.sh`` with a pure-Python
implementation that stages the agent runtime once on the host, relies on the
Containerlab ``configs/pingmesh:/tmp/pingmesh:ro`` bind for clients, and starts
agents with bounded parallelism.

CLI usage (backward-compatible with the shell script)::

    python -m netopsbench.platform.pingmesh.deploy <pinglist_file> <topology_dir>

Programmatic usage::

    from netopsbench.platform.pingmesh.deploy import deploy_pingmesh
    result = deploy_pingmesh("/path/to/topology_dir")
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import yaml

from netopsbench.config import config, repo_root
from netopsbench.logging_utils import get_logger
from netopsbench.platform.pingmesh.generator import generate_pinglist_from_topology
from netopsbench.platform.topology.topology_utils import clab_container_name
from netopsbench.platform.utils.events import emit as _emit
from netopsbench.platform.utils.proc import safe_run, sudo_prefix

logger = get_logger(__name__)


# Agent files staged once under configs/pingmesh and bind-mounted into clients.
_AGENT_FILES: list[str] = [
    "netopsbench/platform/pingmesh/agent.py",
    "netopsbench/platform/pingmesh/cli.py",
    "netopsbench/platform/pingmesh/_agent_support.py",
    "netopsbench/platform/pingmesh/_agent_probe.py",
    "netopsbench/platform/pingmesh/_agent_responder.py",
    "netopsbench/platform/pingmesh/_agent_influx.py",
    "netopsbench/platform/pingmesh/_agent_runtime.py",
    "scripts/runtime/run_pingmesh_agent.py",
]

_RUNTIME_SUBDIR = os.path.join("configs", "pingmesh")
_CONTAINER_RUNTIME_DIR = "/tmp/pingmesh"
_CONTAINER_PINGLIST = "/tmp/pingmesh/pinglist.json"
_CONTAINER_AGENT = "/tmp/pingmesh/run_pingmesh_agent.py"
_PINGMESH_BIND = "configs/pingmesh:/tmp/pingmesh:ro"
_DEFAULT_DEPLOY_PARALLELISM = 32


@dataclass
class DeployResult:
    """Summary of a Pingmesh deployment."""

    deployed: int = 0
    failed: list[str] = field(default_factory=list)
    verified: dict[str, bool] = field(default_factory=dict)


def _docker(*args: str, check: bool = True, capture: bool = False, **kwargs) -> subprocess.CompletedProcess:
    """Run a docker command, raising on failure when *check* is set."""
    kwargs.setdefault("timeout", 60)
    return safe_run(
        [*sudo_prefix(), "docker", *args],
        check=check,
        capture_output=capture,
        text=True,
        **kwargs,
    )


def _running_containers() -> list[str]:
    result = _docker("ps", "--format", "{{.Names}}", check=False, capture=True)
    return result.stdout.strip().splitlines() if result.returncode == 0 else []


def _load_topology_metadata(topology_dir: str) -> dict:
    metadata_path = os.path.join(topology_dir, "topology.json")
    if not os.path.isfile(metadata_path):
        raise FileNotFoundError(f"Topology metadata not found: {metadata_path}")
    with open(metadata_path, encoding="utf-8") as fh:
        return json.load(fh)


def _runtime_dir(topology_dir: str) -> str:
    return os.path.join(topology_dir, _RUNTIME_SUBDIR)


def _staged_pinglist_path(topology_dir: str) -> str:
    return os.path.join(_runtime_dir(topology_dir), "pinglist.json")


def _deploy_parallelism() -> int:
    raw = os.environ.get("NETOPSBENCH_PINGMESH_DEPLOY_PARALLELISM", str(_DEFAULT_DEPLOY_PARALLELISM))
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Ignoring invalid NETOPSBENCH_PINGMESH_DEPLOY_PARALLELISM=%r; using %d",
            raw,
            _DEFAULT_DEPLOY_PARALLELISM,
        )
        value = _DEFAULT_DEPLOY_PARALLELISM
    return max(1, value)


def _stage_runtime(root: str, topology_dir: str, pinglist_file: str, topology_id: str) -> str:
    runtime_dir = _runtime_dir(topology_dir)
    os.makedirs(runtime_dir, exist_ok=True)

    staged_pinglist = _staged_pinglist_path(topology_dir)
    metadata_file = os.path.join(topology_dir, "topology.json")
    generate_pinglist_from_topology(metadata_file, staged_pinglist, topology_id=topology_id)
    if not os.path.isfile(staged_pinglist):
        raise RuntimeError("Pinglist generation failed")

    requested_pinglist = os.path.abspath(pinglist_file)
    if requested_pinglist != os.path.abspath(staged_pinglist):
        requested_dir = os.path.dirname(requested_pinglist)
        if requested_dir:
            os.makedirs(requested_dir, exist_ok=True)
        shutil.copy2(staged_pinglist, requested_pinglist)

    for rel_path in _AGENT_FILES:
        source = os.path.join(root, rel_path)
        if not os.path.isfile(source):
            raise FileNotFoundError(f"Pingmesh runtime source not found: {source}")
        shutil.copy2(source, os.path.join(runtime_dir, os.path.basename(source)))

    required = [staged_pinglist, os.path.join(runtime_dir, "run_pingmesh_agent.py")]
    missing = [path for path in required if not os.path.isfile(path)]
    if missing:
        raise RuntimeError(f"Pingmesh runtime staging failed: missing {', '.join(missing)}")
    return staged_pinglist


def _topology_yaml_path(topology_dir: str, lab_name: str) -> str:
    candidate = os.path.join(topology_dir, f"{lab_name}.clab.yaml")
    if os.path.isfile(candidate):
        return candidate
    matches = sorted(
        os.path.join(topology_dir, name)
        for name in os.listdir(topology_dir)
        if name.endswith(".clab.yaml")
    )
    return matches[0] if matches else candidate


def _validate_pingmesh_bind(topology_dir: str, lab_name: str) -> None:
    yaml_path = _topology_yaml_path(topology_dir, lab_name)
    if not os.path.isfile(yaml_path):
        raise RuntimeError(
            f"Containerlab topology YAML not found: {yaml_path}. Regenerate topology before deploying Pingmesh."
        )

    with open(yaml_path, encoding="utf-8") as fh:
        topology = yaml.safe_load(fh) or {}
    linux_kind = ((topology.get("topology") or {}).get("kinds") or {}).get("linux") or {}
    binds = linux_kind.get("binds") or []
    if _PINGMESH_BIND not in binds:
        raise RuntimeError(
            f"Pingmesh bind missing from {yaml_path}: expected linux kind bind {_PINGMESH_BIND}. "
            "Regenerate topology before deploying Pingmesh."
        )


def _env_assignment(name: str, value: str | int | float | None) -> str:
    return f"{name}={shlex.quote(str(value or ''))}"


def _start_client_agent(
    *,
    client_name: str,
    container: str,
    cycle_interval: int,
    topology_id: str,
    influxdb_url: str,
    influxdb_token: str,
    influxdb_org: str,
    influxdb_bucket: str,
) -> tuple[str, bool, str]:
    env_values: dict[str, str | int] = {
        "PYTHONPATH": f"{_CONTAINER_RUNTIME_DIR}:/tmp",
        "NETOPSBENCH_TOPOLOGY_ID": topology_id,
        "NETOPSBENCH_INFLUXDB_URL": influxdb_url,
        "NETOPSBENCH_INFLUXDB_TOKEN": influxdb_token,
        "NETOPSBENCH_INFLUXDB_ORG": influxdb_org,
        "NETOPSBENCH_INFLUXDB_BUCKET": influxdb_bucket,
    }
    for env_name in ("PINGMESH_RTT_PORTS_PER_CYCLE", "PINGMESH_DF_PORTS_PER_CYCLE"):
        if os.environ.get(env_name):
            env_values[env_name] = os.environ[env_name]

    env_block = " ".join(_env_assignment(name, value) for name, value in env_values.items())
    command = (
        "set -e; "
        "mkdir -p /var/log/pingmesh; "
        f"test -r {_CONTAINER_AGENT}; "
        f"test -r {_CONTAINER_PINGLIST}; "
        f"for pid in $(pgrep -f {shlex.quote(_CONTAINER_AGENT)} || true); do "
        '[ "$pid" = "$$" ] && continue; '
        'kill "$pid" >/dev/null 2>&1 || true; '
        "done; "
        f"{env_block} nohup python3 {_CONTAINER_AGENT} {_CONTAINER_PINGLIST} {cycle_interval} "
        "> /var/log/pingmesh/agent.log 2>&1 </dev/null &"
    )
    ret = _docker("exec", container, "sh", "-c", command, check=False, capture=True, timeout=30)
    if ret.returncode == 0:
        return client_name, True, ""

    detail = (ret.stderr or ret.stdout or "").strip() or f"docker exec exited with code {ret.returncode}"
    message = (
        "failed to start Pingmesh agent; expected /tmp/pingmesh bind and staged runtime to be readable. "
        "Regenerate the topology if the runtime files are missing."
        f" docker output: {detail}"
    )
    return client_name, False, message


def deploy_pingmesh(
    topology_dir: str,
    pinglist_file: str | None = None,
    cycle_interval: int = 1,
    influxdb_url: str | None = None,
    influxdb_token: str | None = None,
    influxdb_org: str | None = None,
    influxdb_bucket: str | None = None,
    topology_id: str | None = None,
    verify: bool = False,
) -> DeployResult:
    """Deploy Pingmesh agents to every client container in *topology_dir*.

    Returns a :class:`DeployResult` with deployment counts and per-client
    verification status.
    """
    root = str(repo_root())

    # --- resolve parameters from env with fallbacks ---
    pinglist_file = pinglist_file or _staged_pinglist_path(topology_dir)
    cycle_interval = int(os.environ.get("PINGMESH_CYCLE_INTERVAL", cycle_interval))
    topology_id = topology_id or config.topology_id or os.path.basename(topology_dir)
    influxdb_url = influxdb_url or config.pingmesh_influxdb_url
    influxdb_token = influxdb_token or config.influxdb_token
    influxdb_org = influxdb_org or config.influxdb_org
    influxdb_bucket = influxdb_bucket or config.influxdb_bucket

    # --- load topology metadata ---
    topo = _load_topology_metadata(topology_dir)
    lab_name = (topo.get("name") or "dcn").strip()
    clients: list[str] = []
    for client in (topo.get("devices", {}) or {}).get("clients", []):
        name = str(client.get("name") or "").strip()
        if name:
            clients.append(name)

    if not clients:
        raise RuntimeError(f"No clients found in topology metadata: {topology_dir}/topology.json")
    _validate_pingmesh_bind(topology_dir, lab_name)

    # --- validate running containers ---
    _emit("=== Deploying Pingmesh Agents ===")
    _emit(f"Topology: {topology_dir}")
    _emit(f"Topology ID: {topology_id}")
    _emit(f"InfluxDB: {influxdb_url} bucket={influxdb_bucket}")
    _emit(f"Cycle interval: {cycle_interval}s")
    _emit(f"Deploy parallelism: {_deploy_parallelism()}")
    _emit("")

    running = set(_running_containers())
    expected = {clab_container_name(lab_name, c) for c in clients}
    running_clients = expected & running
    if not running_clients:
        raise RuntimeError(f"No client containers running for lab '{lab_name}'. " f"Deploy topology first.")
    if len(running_clients) != len(expected):
        missing = expected - running
        logger.warning(
            "%d client container(s) not running: %s",
            len(missing),
            ", ".join(sorted(missing)),
        )

    _emit(f"[0/3] Validated {len(running_clients)}/{len(expected)} client containers")
    _emit("")

    # --- stage runtime files and pinglist ---
    _emit("[1/3] Staging Pingmesh runtime...")
    stage_started = time.monotonic()
    staged_pinglist = _stage_runtime(root, topology_dir, pinglist_file, topology_id)
    stage_elapsed = time.monotonic() - stage_started
    _emit(f"  Runtime: {_runtime_dir(topology_dir)}")
    _emit(f"  Pinglist: {staged_pinglist}")
    _emit(f"  Staged in {stage_elapsed:.1f}s")
    _emit("")

    # --- start agents in each client ---
    _emit("[2/3] Starting agents in client containers...")
    result = DeployResult()
    start_started = time.monotonic()
    outcomes: dict[str, tuple[bool, str]] = {}
    futures = {}
    deploy_parallelism = _deploy_parallelism()
    with ThreadPoolExecutor(max_workers=deploy_parallelism) as executor:
        for client_name in clients:
            container = clab_container_name(lab_name, client_name)
            if container not in running:
                outcomes[client_name] = (False, "container is not running")
                continue
            futures[
                executor.submit(
                    _start_client_agent,
                    client_name=client_name,
                    container=container,
                    cycle_interval=cycle_interval,
                    topology_id=topology_id,
                    influxdb_url=influxdb_url,
                    influxdb_token=influxdb_token,
                    influxdb_org=influxdb_org,
                    influxdb_bucket=influxdb_bucket,
                )
            ] = client_name

        for future in as_completed(futures):
            client_name, ok, message = future.result()
            outcomes[client_name] = (ok, message)

    for client_name in clients:
        container = clab_container_name(lab_name, client_name)
        ok, message = outcomes.get(client_name, (False, "not scheduled"))
        if ok:
            result.deployed += 1
        else:
            logger.error("%s: %s", container, message)
            result.failed.append(client_name)

    start_elapsed = time.monotonic() - start_started
    _emit(f"  Started {result.deployed}/{len(clients)} clients in {start_elapsed:.1f}s")

    if result.failed:
        logger.warning(
            "%d client(s) failed: %s",
            len(result.failed),
            ", ".join(result.failed),
        )
    if result.deployed == 0:
        raise RuntimeError(
            "Pingmesh deployment failed for all clients; /tmp/pingmesh bind or staged runtime is not readable. "
            "Regenerate the topology before deploying Pingmesh."
        )

    # --- verify ---
    if verify:
        _emit("")
        _emit("[3/3] Verifying deployment...")
        time.sleep(2)
        for client_name in clients[:3]:
            container = clab_container_name(lab_name, client_name)
            ret = _docker(
                "exec",
                container,
                "ps",
                "aux",
                check=False,
                capture=True,
            )
            agent_running = "run_pingmesh_agent.py" in (ret.stdout or "")
            result.verified[client_name] = agent_running
            status = "✓ running" if agent_running else "✗ NOT running"
            _emit(f"  {container}: Agent {status}")

    _emit("")
    _emit("=== Pingmesh Deployment Complete ===")
    _emit(f"  Deployed to {result.deployed} clients")
    _emit(f"  Pinglist: {staged_pinglist}")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point (backward-compatible with shell script args)."""
    args = list(argv if argv is not None else sys.argv[1:])

    pinglist_file = args[0] if len(args) > 0 else None
    topology_dir = args[1] if len(args) > 1 else (config.topology_dir or "generated_topology")

    try:
        deploy_pingmesh(topology_dir=topology_dir, pinglist_file=pinglist_file)
    except (FileNotFoundError, RuntimeError) as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
