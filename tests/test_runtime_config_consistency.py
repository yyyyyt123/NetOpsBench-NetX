"""Regression tests for streamlined runtime defaults and public examples."""

from pathlib import Path


def test_config_module_no_longer_exports_provider_registry_helpers():
    import netopsbench.config as config_mod

    assert not hasattr(config_mod, "LLM_PROVIDER_DEFAULTS")
    assert not hasattr(config_mod, "get_provider_defaults")
    assert not hasattr(config_mod, "get_openai_base_url")
    assert not hasattr(config_mod.NetOpsBenchConfig, "openai_base_url")


def test_observability_defaults_are_centralized_in_config_source():
    toolkit_text = Path("netopsbench/platform/toolkit/toolkit.py").read_text(encoding="utf-8")
    scenario_executor_text = Path("netopsbench/platform/scenario/executor.py").read_text(encoding="utf-8")
    detector_text = Path("netopsbench/platform/pingmesh/detector.py").read_text(encoding="utf-8")

    assert "my-super-secret-auth-token" not in toolkit_text
    assert '"myorg"' not in toolkit_text
    assert '"network_data"' not in toolkit_text

    assert "my-super-secret-auth-token" not in scenario_executor_text
    assert '"myorg"' not in scenario_executor_text
    assert '"network_data"' not in scenario_executor_text

    assert "my-super-secret-auth-token" not in detector_text
    assert '"myorg"' not in detector_text
    assert '"network_data"' not in detector_text


def test_update_telegraf_config_uses_central_defaults_without_shadowing(tmp_path, monkeypatch):
    import json

    import netopsbench.platform.observability.telegraf as telegraf_mod

    topo = {
        "name": "demo-runtime",
        "devices": {
            "spines": [{"name": "spine1", "mgmt_ip": "172.31.10.11/24"}],
            "leafs": [{"name": "leaf1", "mgmt_ip": "172.31.10.13/24"}],
        },
        "scale": {"num_spines": 1, "num_leafs": 1, "clients_per_leaf": 0},
    }
    topo_path = tmp_path / "topology.json"
    topo_path.write_text(json.dumps(topo), encoding="utf-8")

    repo_root = tmp_path / "repo"
    template_dir = repo_root / "observability"
    template_dir.mkdir(parents=True)
    (template_dir / "telegraf.conf.template").write_text(
        "{{GNMI_INPUTS}}\n{{INFLUXDB_URL}}\n{{INFLUXDB_TOKEN}}\n{{INFLUXDB_ORG}}\n{{INFLUXDB_BUCKET}}\n{{TOPOLOGY_ID}}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(telegraf_mod, "REPO_ROOT", repo_root)

    output_path = tmp_path / "telegraf.conf"
    rc = telegraf_mod.update_telegraf_config(str(topo_path), output_file=str(output_path))

    assert rc == 0
    rendered = output_path.read_text(encoding="utf-8")
    assert '"172.31.10.11:50051"' in rendered
    assert '"172.31.10.13:50051"' in rendered
    assert "http://influxdb:8086" in rendered
    assert "replace-me" in rendered
    assert "netopsbench" in rendered
    assert "demo-runtime" in rendered


def test_update_telegraf_config_scopes_gnmi_subscriptions_by_device_role(tmp_path, monkeypatch):
    import copy

    from netopsbench.platform.topology.generator import TOPOLOGY_SCALES, TopologyGenerator
    import netopsbench.platform.observability.telegraf as telegraf_mod

    topology_dir = tmp_path / "topology"
    topo_config = copy.deepcopy(TOPOLOGY_SCALES["xlarge"])
    result = TopologyGenerator(config=topo_config, output_dir=str(topology_dir)).generate()
    topology_file = topology_dir / "topology.json"
    output_path = tmp_path / "telegraf.conf"

    rc = telegraf_mod.update_telegraf_config(str(topology_file), output_file=str(output_path))

    assert rc == 0
    rendered = output_path.read_text(encoding="utf-8")
    assert rendered.count("[[inputs.gnmi]]") == 2
    assert "# gNMI role: spine" in rendered
    assert "# gNMI role: leaf" in rendered

    spine_block = rendered.split("# gNMI role: spine", 1)[1].split("# gNMI role: leaf", 1)[0]
    leaf_block = rendered.split("# gNMI role: leaf", 1)[1].split("# 5. sFlow", 1)[0]

    assert '"172.20.20.11:50051"' in spine_block
    assert '"172.20.20.27:50051"' not in spine_block
    assert 'path = "COUNTERS/Ethernet508"' in spine_block

    assert '"172.20.20.27:50051"' in leaf_block
    assert '"172.20.20.11:50051"' not in leaf_block
    assert 'path = "COUNTERS/Ethernet64"' in leaf_block
    assert 'path = "COUNTERS/Ethernet68"' not in leaf_block


def test_grafana_configs_use_runtime_scoping_variables():
    datasource_text = Path("observability/grafana/provisioning/datasources/default.yaml").read_text(encoding="utf-8")
    compose_text = Path("observability/docker-compose.yaml").read_text(encoding="utf-8")

    assert "${NETOPSBENCH_INFLUXDB_TOKEN}" in datasource_text
    assert "${NETOPSBENCH_GRAFANA_DEFAULT_BUCKET}" in datasource_text
    assert "NETOPSBENCH_INFLUXDB_TOKEN=${NETOPSBENCH_INFLUXDB_TOKEN:-replace-me}" in compose_text
    assert "NETOPSBENCH_GRAFANA_DEFAULT_BUCKET=${NETOPSBENCH_GRAFANA_DEFAULT_BUCKET:-netopsbench}" in compose_text


def test_grafana_dashboards_parameterize_bucket_and_topology():
    dashboards = [
        Path("observability/grafana/dashboards/network_overview.json"),
        Path("observability/grafana/dashboards/pingmesh.json"),
    ]

    for dashboard_path in dashboards:
        text = dashboard_path.read_text(encoding="utf-8")
        assert "${bucket}" in text
        assert "${topology_id}" in text
        assert 'r.topology_id == \\"${topology_id}\\" or \\"${topology_id}\\" == \\"all\\"' in text
        assert '"name": "bucket"' in text
        assert '"name": "topology_id"' in text
        assert 'from(bucket: \\"netopsbench\\")' not in text


def test_network_overview_casts_interface_counters_before_derivative():
    text = Path("observability/grafana/dashboards/network_overview.json").read_text(encoding="utf-8")

    # Telegraf's gNMI interface counters arrive as string columns in Flux frames.
    # The dashboard must cast them before applying derivative(), or Grafana shows "No data".
    assert text.count("|> toFloat()\\n  |> derivative(unit: 1s, nonNegative: true)") >= 4


def test_network_overview_removes_cpu_memory_panels_and_enables_bgp_tail_input():
    dashboard_text = Path("observability/grafana/dashboards/network_overview.json").read_text(encoding="utf-8")
    telegraf_text = Path("observability/telegraf.conf.template").read_text(encoding="utf-8")
    start_worker_text = Path("scripts/observability/start_worker_telegraf.sh").read_text(encoding="utf-8")
    deploy_text = Path("scripts/runtime/deploy.sh").read_text(encoding="utf-8")
    deploy_worker_text = Path("scripts/runtime/deploy_worker.sh").read_text(encoding="utf-8")

    assert "CPU Usage (optional)" not in dashboard_text
    assert "Memory Utilization (optional)" not in dashboard_text
    assert "/var/lib/netopsbench/bgp_neighbors.lp" in telegraf_text
    assert "from_beginning = true" in telegraf_text
    assert 'watch_method = "poll"' in telegraf_text
    assert 'metric_batch_size = 5000' in telegraf_text
    assert 'metric_buffer_limit = 200000' in telegraf_text
    assert 'debug = false' in telegraf_text
    assert "[[processors.printer]]" not in telegraf_text
    assert 'os.getenv("GNMI_SUBSCRIPTION_MODE", "sample")' in Path(
        "netopsbench/platform/observability/telegraf.py"
    ).read_text(encoding="utf-8")
    assert 'chmod 755 "$CONFIG_DIR"' in start_worker_text
    assert 'chmod 644 "$CONFIG_PATH" "$BGP_FILE_PATH"' in start_worker_text
    assert "start_worker_telegraf.sh" in deploy_text
    assert "TOPOLOGY_ID=${NETOPSBENCH_TOPOLOGY_ID:-$(basename \"$ACTUAL_TOPO_DIR\")}" in deploy_text
    assert "scripts/runtime/run_bgp_collector.py" in deploy_text
    assert "BGP_COLLECTOR_DETACH=(nohup setsid)" in deploy_text
    assert '"${BGP_COLLECTOR_DETACH[@]}" $PYTHON scripts/runtime/run_bgp_collector.py' in deploy_text
    assert '--parallelism "${NETOPSBENCH_BGP_COLLECTOR_PARALLELISM:-16}"' in deploy_text
    assert "NETOPSBENCH_TOPOLOGY_ID=\"$TOPOLOGY_ID\"" in deploy_worker_text
    assert "BGP_COLLECTOR_DETACH=(nohup setsid)" in deploy_worker_text
    assert '"${BGP_COLLECTOR_DETACH[@]}" $PYTHON scripts/runtime/run_bgp_collector.py' in deploy_worker_text
    assert '--parallelism "${NETOPSBENCH_BGP_COLLECTOR_PARALLELISM:-16}"' in deploy_worker_text
    assert "docker restart telegraf" not in deploy_text
    assert dashboard_text.count('group(columns: [\\"source\\", \\"neighbor_address\\"])\\n  |> last()') >= 3


def test_deploy_scripts_stop_stale_bgp_collector_before_starting_new_one():
    deploy_text = Path("scripts/runtime/deploy.sh").read_text(encoding="utf-8")
    deploy_worker_text = Path("scripts/runtime/deploy_worker.sh").read_text(encoding="utf-8")

    for text in (deploy_text, deploy_worker_text):
        assert 'BGP_PID=$(cat "$BGP_COLLECTOR_PID_FILE" 2>/dev/null || true)' in text
        assert 'kill "$BGP_PID" >/dev/null 2>&1 || true' in text
        assert 'rm -f "$BGP_COLLECTOR_PID_FILE"' in text
        assert text.index('BGP_PID=$(cat "$BGP_COLLECTOR_PID_FILE" 2>/dev/null || true)') < text.index(
            "start_worker_telegraf.sh"
        )


def test_deploy_uses_worker_telegraf_generation_only():
    deploy_text = Path("scripts/runtime/deploy.sh").read_text(encoding="utf-8")

    assert 'start_worker_telegraf.sh "$ACTUAL_TOPO_DIR" "telegraf-${LAB_NAME}"' in deploy_text
    assert "$PYTHON -m netopsbench.platform.observability.telegraf \"$METADATA_FILE\"" not in deploy_text
    assert "[4/7] Generating Telegraf configuration" not in deploy_text


def test_bgp_collector_size_default_is_owned_by_collector():
    deploy_text = Path("scripts/runtime/deploy.sh").read_text(encoding="utf-8")
    deploy_worker_text = Path("scripts/runtime/deploy_worker.sh").read_text(encoding="utf-8")
    collector_text = Path("scripts/runtime/run_bgp_collector.py").read_text(encoding="utf-8")

    assert "NETOPSBENCH_BGP_COLLECTOR_MAX_BYTES" not in deploy_text
    assert "NETOPSBENCH_BGP_COLLECTOR_MAX_BYTES" not in deploy_worker_text
    assert "NETOPSBENCH_BGP_COLLECTOR_MAX_BYTES" in collector_text


def test_runtime_scripts_use_bind_mounted_pingmesh_deploy_path():
    deploy_text = Path("scripts/runtime/deploy.sh").read_text(encoding="utf-8")
    deploy_worker_text = Path("scripts/runtime/deploy_worker.sh").read_text(encoding="utf-8")

    assert "$PYTHON -m netopsbench.platform.pingmesh.deploy" in deploy_text
    assert "$PYTHON -m netopsbench.platform.pingmesh.deploy" in deploy_worker_text
    assert "$ACTUAL_TOPO_DIR/configs/pingmesh/pinglist.json" in deploy_text
    assert "$TOPOLOGY_DIR/configs/pingmesh/pinglist.json" in deploy_worker_text
    assert "$ACTUAL_TOPO_DIR/pinglist.json" not in deploy_text
    assert "$TOPOLOGY_DIR/pinglist.json" not in deploy_worker_text


def test_runtime_scripts_default_sonic_apply_parallelism_to_32():
    deploy_text = Path("scripts/runtime/deploy.sh").read_text(encoding="utf-8")
    deploy_worker_text = Path("scripts/runtime/deploy_worker.sh").read_text(encoding="utf-8")

    assert "APPLY_CONFIG_PARALLEL=${NETOPSBENCH_APPLY_CONFIG_PARALLEL:-32}" in deploy_text
    assert "APPLY_CONFIG_PARALLEL=${NETOPSBENCH_APPLY_CONFIG_PARALLEL:-32}" in deploy_worker_text
    assert "NETOPSBENCH_APPLY_CONFIG_PARALLEL:-4" not in deploy_text
    assert "NETOPSBENCH_APPLY_CONFIG_PARALLEL:-4" not in deploy_worker_text


def test_env_example_documents_runtime_parallelism_and_bgp_retention():
    env_example = Path(".env.example").read_text(encoding="utf-8")

    assert "NETOPSBENCH_TRAFFIC_PARALLELISM=32" in env_example
    assert "NETOPSBENCH_BGP_COLLECTOR_PARALLELISM=16" in env_example
    assert "NETOPSBENCH_BGP_COLLECTOR_MAX_BYTES=134217728" in env_example


def test_pingmesh_agent_can_run_without_repo_package_install(tmp_path):
    import json
    import os
    import subprocess
    import sys
    import time

    pinglist = tmp_path / "pinglist.json"
    pinglist.write_text(json.dumps({"probes": [], "topology_id": "xs"}), encoding="utf-8")

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["HOSTNAME"] = "client1"

    proc = subprocess.Popen(
        [sys.executable, "scripts/runtime/run_pingmesh_agent.py", str(pinglist), "5"],
        cwd=str(Path.cwd()),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(1)
    still_running = proc.poll() is None
    if still_running:
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=3)
    else:
        stdout, stderr = proc.communicate(timeout=3)

    assert "ModuleNotFoundError" not in stderr
    assert still_running or proc.returncode == 0

    agent_text = Path("netopsbench/platform/pingmesh/agent.py").read_text(encoding="utf-8")
    generator_text = Path("netopsbench/platform/pingmesh/generator.py").read_text(encoding="utf-8")
    detector_text = Path("netopsbench/platform/pingmesh/detector.py").read_text(encoding="utf-8")

    assert "except ModuleNotFoundError" not in agent_text
    assert "except ModuleNotFoundError" not in generator_text
    assert "except ModuleNotFoundError" not in detector_text


def test_pingmesh_agent_uses_single_fanout_probe_worker():
    agent_text = Path("netopsbench/platform/pingmesh/agent.py").read_text(encoding="utf-8")
    runtime_text = Path("netopsbench/platform/pingmesh/_agent_runtime.py").read_text(encoding="utf-8")
    support_text = Path("netopsbench/platform/pingmesh/_agent_support.py").read_text(encoding="utf-8")

    assert "Parallel workers" not in agent_text
    assert "ThreadPoolExecutor" not in agent_text
    assert "ThreadPoolExecutor" not in runtime_text
    assert "ThreadPoolExecutor" not in support_text
    assert "as_completed" not in runtime_text
    assert "Probe worker: 1" in agent_text
    assert "Concurrent flows" in agent_text
    assert "Port pool" in agent_text
    assert "Active ports/cycle" in agent_text
    assert "udp_probe_cycle(self.tasks)" in runtime_text


def test_plugin_agent_doc_uses_public_sdk_agent_narrative():
    doc_text = Path("docs/content/docs/build-your-agent/custom-agents.mdx").read_text(encoding="utf-8")

    assert "diagnose(context)" in doc_text
    assert "MinimalDeepAgent" in doc_text
    assert "simple_baseline_agent.py" not in doc_text
    assert "@register_agent" not in doc_text


def test_xs_real_smoke_uses_explicit_stub_agent_name():
    smoke_text = Path("tests/test_runtime_xs_smoke_real.py").read_text(encoding="utf-8")

    assert "_EpisodeAwareAgent" not in smoke_text
    assert "_RuntimeSmokeStubAgent" in smoke_text
