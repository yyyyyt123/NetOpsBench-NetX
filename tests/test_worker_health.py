import json
import subprocess
import time
from pathlib import Path

from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.platform.runtime import health
from netopsbench.platform.topology.topology_utils import coerce_topology_manifest


def test_k18_expectations_distinguish_client_links_and_bgp_fabric():
    from netopsbench.platform.topology.config import config_for_scale
    from netopsbench.platform.topology.fat_tree_builder import build_fat_tree_plan

    topo = build_fat_tree_plan(config_for_scale("fat-tree-k18")).manifest
    counts = health.expected_network_counts(topo)
    assert counts["core81"] == {"active_interfaces": 18, "bgp_neighbors": 18}
    assert counts["agg162"] == {"active_interfaces": 18, "bgp_neighbors": 18}
    assert counts["edge162"] == {"active_interfaces": 11, "bgp_neighbors": 9}
    assert counts["client324"] == {"active_interfaces": 1, "bgp_neighbors": 0}
    assert sum(row["bgp_neighbors"] for row in counts.values()) == 5832
    assert sum(row["active_interfaces"] for row in counts.values()) == 6480


def _canonical_topology_dict(*, scale: str, spines: int, leafs: int, clients: int) -> dict:
    from netopsbench.platform.topology.clos_builder import build_clos_plan
    from netopsbench.platform.topology.config import TopologyConfig

    return build_clos_plan(
        TopologyConfig(
            name=scale,
            scale_name=scale,
            num_spines=spines,
            num_leafs=leafs,
            clients_per_leaf=clients // leafs,
            mgmt_ipv4_subnet="172.20.20.0/23",
        )
    ).manifest.model_dump(mode="json")


def _runtime_identity(topology_dir: Path, name: str) -> RuntimeIdentity:
    return RuntimeIdentity.create(
        runtime_id=name,
        worker_id="worker-1",
        worker_index=1,
        lab_name=name,
        topology_dir=topology_dir,
        mgmt_subnet="172.20.20.0/24",
        mgmt_network=f"clab-mgmt-{name}",
        bucket=f"network_data_{name}",
    )


def test_active_interface_parser_exposes_incomplete_xlarge_spine():
    topo = coerce_topology_manifest(_canonical_topology_dict(scale="xlarge", spines=16, leafs=128, clients=256))
    output = "\n".join(f"Ethernet{idx * 4} 1,2,3,4 100G 9100 N/A up up QSFP" for idx in range(32))

    active = health._parse_active_interfaces(output)
    assert len(active) == 32
    counts = health.expected_network_counts(topo)
    assert counts["spine1"]["active_interfaces"] == 128
    assert counts["leaf128"]["active_interfaces"] == 18


def test_deployment_convergence_checks_every_routing_device():
    topo = coerce_topology_manifest(_canonical_topology_dict(scale="xlarge", spines=16, leafs=128, clients=256))

    assert len(health._convergence_targets(topo, all_routing_devices=True)) == 144
    assert len(health._convergence_targets(topo, all_routing_devices=False)) == 3


def test_fat_tree_sparse_expected_active_interface_counts():
    import tempfile

    from netopsbench.platform.topology.generator import generate_topology
    from netopsbench.platform.topology.topology_utils import load_topology_manifest

    with tempfile.TemporaryDirectory() as raw_dir:
        generate_topology("fat-tree-k12", raw_dir)
        topo = load_topology_manifest(raw_dir)

        counts = health.expected_network_counts(topo)
        assert counts["core1"]["active_interfaces"] == 12
        assert counts["agg1"]["active_interfaces"] == 12
        assert counts["edge72"]["active_interfaces"] == 8


def test_worker_health_retries_observability_until_collector_is_ready(tmp_path, monkeypatch):
    from netopsbench.platform.observability import influxdb, validation
    from netopsbench.platform.topology.generator import generate_topology

    metadata = generate_topology("xs", str(tmp_path), name="health-xs")["metadata"]

    monkeypatch.setattr(
        health,
        "safe_run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="telegraf-health-xs\n", stderr=""),
    )
    monkeypatch.setattr(health, "_running_container_count", lambda _lab_name: 6)

    exec_calls = []

    def fake_docker_exec(_container, *command, **_kwargs):
        exec_calls.append(command)
        joined = " ".join(command)
        if command[:2] == ("python3", "-c"):
            output = json.dumps(
                {
                    "version": 1,
                    "checks": {
                        "bgp": {
                            "returncode": 0,
                            "stdout": fake_docker_exec(_container, "vtysh", "-c", "show ip bgp summary").stdout,
                        },
                        "interfaces": {
                            "returncode": 0,
                            "stdout": fake_docker_exec(_container, "bash", "-lc", "show interfaces status").stdout,
                        },
                    },
                }
            )
        elif "show ip bgp summary" in joined:
            output = "".join(f"10.0.0.{index} 4 65001 0 0 0 0 0 00:10:00 1\n" for index in range(1, 5))
        elif "show interfaces status" in joined:
            output = "".join(f"Ethernet{index * 4} 1,2,3,4 100G 9100 N/A up up QSFP\n" for index in range(4))
        else:
            output = ""
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(health, "_docker_exec", fake_docker_exec)
    monkeypatch.setattr(
        health,
        "request_agent",
        lambda *_args, **_kwargs: {
            "protocol_version": 1,
            "ok": True,
            "status": {
                "ready": True,
                "heartbeat_unix_ns": time.time_ns(),
                "plan_id": metadata["pingmesh_plan"]["settings"]["plan_id"],
                "config_schema_version": 2,
            },
        },
    )
    attempts = []

    def fake_check_observability(*_args, **_kwargs):
        check = next(iter(_kwargs["checks"]))
        attempts.append(check)
        return (
            ["collector has not written its first sample"] if check == "syslog" and attempts.count(check) == 1 else []
        )

    monkeypatch.setattr(validation, "check_observability", fake_check_observability)
    monkeypatch.setattr(
        influxdb, "query_flux", lambda *_args, **_kwargs: type("Result", (), {"status": "ok", "text": ""})()
    )

    errors = health.check_worker_health(
        _runtime_identity(tmp_path, "health-xs"),
        health_retries=2,
        health_delay=0,
    )

    assert errors == []
    assert "logger -n" in " ".join(exec_calls[0])
    assert sum(c == ("bash", "-lc", "show interfaces status") for c in exec_calls) == 3
    assert sorted(attempts) == ["bgp", "interfaces", "pingmesh", "syslog", "syslog"]
