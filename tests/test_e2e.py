#!/usr/bin/env python3
"""
End-to-end tests for NetOpsBench benchmark system.

These tests verify the complete benchmark flow works correctly.
"""

import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netopsbench.evaluator.scorer import AgentOutput, EvaluationResult, Evaluator
from netopsbench.platform.faults.injector import FaultInjector
from netopsbench.platform.faults.services.topology_runtime import TopologyRuntime
from netopsbench.platform.faults.specs import get_builtin_fault_specs
from netopsbench.platform.pingmesh.generator import PinglistGenerator, generate_pinglist_from_topology
from netopsbench.platform.scenario.generator import load_topology, parse_bgp_config, parse_network_interfaces
from netopsbench.platform.scenario.parser import parse_scenario_file
from netopsbench.platform.scenario.validator import validate_scenario, validate_scenario_topology
from netopsbench.platform.session.scoring import resolve_scenario_files, score_scenario_fault_episodes
from netopsbench.platform.toolkit import fastmcp_server
from netopsbench.platform.toolkit.mcp.registry import load_tool_specs

# Internal test path: direct toolkit import keeps implementation-level e2e checks fast.
from netopsbench.platform.toolkit.toolkit import AgentToolkit, ToolResult
from netopsbench.platform.topology.generator import TOPOLOGY_SCALES, generate_topology
from netopsbench.platform.utils.interface_names import are_interfaces_equivalent


def _generated_scenario_path(filename: str) -> str:
    generated_root = Path("scenarios/generated")
    if not generated_root.exists():
        pytest.skip("scenarios/generated not found; generate scenarios before running this test")
    matches = sorted(generated_root.rglob(filename))
    if not matches:
        pytest.skip(f"{filename} not found under scenarios/generated; generate matching scenarios first")
    return str(matches[0])


def _write_config_db(topology_dir: str | Path, device: str, interfaces: dict[str, list[str]]) -> Path:
    interface_table: dict[str, dict] = {}
    for interface_name, cidrs in interfaces.items():
        interface_table[interface_name] = {}
        for cidr in cidrs:
            interface_table[f"{interface_name}|{cidr}"] = {}

    path = Path(topology_dir) / "configs" / "sonic" / device / "config_db.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"INTERFACE": interface_table}), encoding="utf-8")
    return path


class TestTopologyGenerator:
    """Tests for topology generation."""

    def test_topology_scales_defined(self):
        """Test that all expected topology scales are defined."""
        expected_scales = ["xs", "small", "medium", "large", "xlarge"]
        for scale in expected_scales:
            assert scale in TOPOLOGY_SCALES, f"Scale {scale} not defined"

    def test_generate_xs_topology(self):
        """Test generating an xs topology."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology("xs", tmpdir)

            assert os.path.exists(result["yaml_file"]), "YAML file not created"
            assert os.path.exists(result["metadata_file"]), "Metadata file not created"
            assert len(result["config_files"]) == 4, "Expected 4 config files (2 spines + 2 leafs)"

            # Verify metadata
            metadata = result["metadata"]
            assert metadata["scale"]["num_spines"] == 2
            assert metadata["scale"]["num_leafs"] == 2
            assert metadata["scale"]["clients_per_leaf"] == 1
            assert metadata["scale"]["total_clients"] == 2

            rendered_yaml = Path(result["yaml_file"]).read_text(encoding="utf-8")
            assert "yyyyyt123/netopsbench-sonic-vs-202505-telemetry:202505-telemetry" in rendered_yaml

    @pytest.mark.parametrize("scale", list(TOPOLOGY_SCALES.keys()))
    def test_all_scales_use_bind_based_preseed_artifacts(self, scale):
        """Every scale should use the same bind-mounted SONiC startup artifact path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology(scale, tmpdir)
            rendered = yaml.safe_load(Path(result["yaml_file"]).read_text(encoding="utf-8"))

            binds = rendered["topology"]["kinds"]["sonic-vs"]["binds"]
            linux_binds = rendered["topology"]["kinds"]["linux"]["binds"]
            assert "configs/sonic/__clabNodeName__/config_db.json:/etc/sonic/config_db.json:rw" in binds
            assert (
                "configs/sonic/__clabNodeName__/port_config.ini:"
                "/usr/share/sonic/device/x86_64-kvm_x86_64-r0/Force10-S6000/port_config.ini:rw"
            ) in binds
            assert (
                "configs/sonic/__clabNodeName__/lanemap.ini:"
                "/usr/share/sonic/device/x86_64-kvm_x86_64-r0/Force10-S6000/lanemap.ini:rw"
            ) in binds
            assert "configs/sonic/start.sh:/usr/bin/start.sh:ro" in binds
            assert "configs/frr/__clabNodeName__.conf:/etc/frr/frr.conf:rw" in binds
            assert "configs/pingmesh:/tmp/pingmesh:ro" in linux_binds

            assert not list(Path(tmpdir, "configs").glob("*.sh"))
            assert not list(Path(tmpdir, "configs").glob("*.configdb.json"))
            assert Path(tmpdir, "configs", "pingmesh").is_dir()

            metadata = result["metadata"]
            first_spine = Path(tmpdir, "configs", "sonic", "spine1", "config_db.json")
            first_leaf = Path(tmpdir, "configs", "sonic", "leaf1", "config_db.json")
            assert first_spine.exists()
            assert first_leaf.exists()
            assert Path(tmpdir, "configs", "sonic", "spine1", "port_config.ini").exists()
            assert Path(tmpdir, "configs", "sonic", "spine1", "lanemap.ini").exists()
            start_wrapper = Path(tmpdir, "configs", "sonic", "start.sh")
            assert start_wrapper.exists()
            assert start_wrapper.stat().st_mode & stat.S_IXUSR
            wrapper_text = start_wrapper.read_text(encoding="utf-8")
            assert (
                "NETOPSBENCH_ORIGINAL_SONIC_START_SHA256="
                "8c5aa959f0a3ed0bf1a57f7ecfd004485d5600b9ab71b388c2b15e109b77ee12"
            ) in wrapper_text
            assert "wait_for_front_panel_links" in wrapper_text
            assert "NETOPSBENCH_SONIC_LINK_WAIT_TIMEOUT" in wrapper_text
            assert "install_generated_config_db" in wrapper_text
            assert 'cat "$src" > "$dst"' in wrapper_text
            assert Path(tmpdir, "configs", "frr", "spine1.conf").exists()

            spine_config = json.loads(first_spine.read_text(encoding="utf-8"))
            assert spine_config["DEVICE_METADATA"]["localhost"]["platform"] == "x86_64-kvm_x86_64-r0"
            assert spine_config["DEVICE_METADATA"]["localhost"]["mac"].startswith("02:")
            spine_last_port = f"Ethernet{(metadata['scale']['num_leafs'] - 1) * 4}"
            assert spine_last_port in spine_config["PORT"]
            assert f"{spine_last_port}|10.1.{metadata['scale']['num_leafs']}.1/30" in spine_config["INTERFACE"]
            assert str(first_spine) in result["config_files"]

            leaf_config = json.loads(first_leaf.read_text(encoding="utf-8"))
            assert leaf_config["DEVICE_METADATA"]["localhost"]["mac"] != spine_config["DEVICE_METADATA"]["localhost"]["mac"]
            leaf_last_port = f"Ethernet{(metadata['scale']['num_spines'] + metadata['scale']['clients_per_leaf'] - 1) * 4}"
            assert leaf_last_port in leaf_config["PORT"]
            assert str(first_leaf) in result["config_files"]

    def test_generated_switch_configs_seed_gnmi_defaults(self):
        """Generated SONiC startup configs should seed telemetry tables expected by 202505 images."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology("xs", tmpdir)

            for config_path in result["config_files"]:
                payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
                assert payload["GNMI"]["gnmi"]["port"] == "50051"
                assert payload["GNMI"]["gnmi"]["client_auth"] == "false"
                assert payload["GNMI"]["certs"]["server_key"].endswith("streamingtelemetryserver.key")
                assert payload["FLEX_COUNTER_TABLE"]["PORT"]["FLEX_COUNTER_STATUS"] == "enable"
                assert payload["FLEX_COUNTER_TABLE"]["PORT"]["POLL_INTERVAL"] == "1000"
                assert "telegraf" in payload["SYSLOG_SERVER"]

    def test_generate_small_topology(self):
        """Test generating a small topology."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology("small", tmpdir)

            metadata = result["metadata"]
            assert metadata["scale"]["num_spines"] == 2
            assert metadata["scale"]["num_leafs"] == 4
            assert metadata["scale"]["clients_per_leaf"] == 2
            assert metadata["scale"]["total_clients"] == 8

    def test_generate_xlarge_topology_metadata_and_addressing(self):
        """xlarge should fit the Clos address plans and avoid collector collisions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology("xlarge", tmpdir)

            metadata = result["metadata"]
            devices = metadata["devices"]
            assert metadata["topology_scale"] == "xlarge"
            assert metadata["management"]["ipv4_subnet"] == "172.20.20.0/23"
            assert metadata["scale"]["num_spines"] == 16
            assert metadata["scale"]["num_leafs"] == 128
            assert metadata["scale"]["clients_per_leaf"] == 1
            assert metadata["scale"]["total_clients"] == 128
            assert metadata["pingmesh"]["max_dests_per_client"] == 16

            mgmt_ips = {
                item["mgmt_ip"]
                for role in ("spines", "leafs", "clients")
                for item in devices[role]
            }
            assert len(mgmt_ips) == 16 + 128 + 128
            assert metadata["collector"]["ipv4"] not in mgmt_ips

            spine16_config = json.loads(
                Path(tmpdir, "configs", "sonic", "spine16", "config_db.json").read_text(encoding="utf-8")
            )
            leaf128_config = json.loads(
                Path(tmpdir, "configs", "sonic", "leaf128", "config_db.json").read_text(encoding="utf-8")
            )
            spine16_ports = Path(tmpdir, "configs", "sonic", "spine16", "port_config.ini").read_text(
                encoding="utf-8"
            )
            spine16_lanemap = Path(tmpdir, "configs", "sonic", "spine16", "lanemap.ini").read_text(
                encoding="utf-8"
            )
            leaf128_frr = Path(tmpdir, "configs", "frr", "leaf128.conf").read_text(encoding="utf-8")
            spine16_frr = Path(tmpdir, "configs", "frr", "spine16.conf").read_text(encoding="utf-8")

            assert not Path(tmpdir, "configs", "spine16.sh").exists()
            assert not Path(tmpdir, "configs", "spine16.configdb.json").exists()
            assert "neighbor 10.16.128.2 remote-as 65138" in spine16_frr
            assert len([key for key in spine16_config["INTERFACE"] if "|" not in key]) == 128
            assert spine16_config["INTERFACE"]["Ethernet508"] == {}
            assert spine16_config["INTERFACE"]["Ethernet508|10.16.128.1/30"] == {}
            assert spine16_config["PORT"]["Ethernet508"]["lanes"] == "509,510,511,512"
            assert "Ethernet508" in spine16_ports
            assert "eth1:1,2,3,4" in spine16_lanemap
            assert "eth128:509,510,511,512" in spine16_lanemap
            assert len([line for line in spine16_lanemap.splitlines() if line.startswith("eth")]) == 128
            assert len([key for key in leaf128_config["INTERFACE"] if "|" not in key]) == 17
            assert leaf128_config["INTERFACE"]["Ethernet60|10.16.128.2/30"] == {}
            assert leaf128_config["INTERFACE"]["Ethernet64|192.168.228.1/30"] == {}
            assert "network 192.168.228.0/30 route-map RM-ALLOW" in leaf128_frr
            leaf128_lanemap = Path(tmpdir, "configs", "sonic", "leaf128", "lanemap.ini").read_text(
                encoding="utf-8"
            )
            assert len([line for line in leaf128_lanemap.splitlines() if line.startswith("eth")]) == 17

    def test_xlarge_interface_sflow_hot_path_is_opt_in(self, monkeypatch):
        """XLarge fast config should avoid per-interface sFlow unless explicitly requested."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology("xlarge", tmpdir)
            default_config = json.loads(Path(result["config_files"][-1]).read_text(encoding="utf-8"))
            assert "SFLOW" not in default_config
            assert "SFLOW_SESSION" not in default_config

        monkeypatch.setenv("NETOPSBENCH_ENABLE_INTERFACE_SFLOW", "1")
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology("xlarge", tmpdir)
            opt_in_config = json.loads(Path(result["config_files"][-1]).read_text(encoding="utf-8"))
            assert opt_in_config["SFLOW"]["global"]["admin_state"] == "up"
            assert opt_in_config["SFLOW_SESSION"]["Ethernet64"]["admin_state"] == "up"

    def test_structured_startup_artifacts_drive_scenario_parsers(self):
        """Scenario parsers should use generated ConfigDB and FRR artifacts as the normal path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            generate_topology("xs", tmpdir)
            spine_cfg = Path(tmpdir, "configs", "sonic", "spine1", "config_db.json")
            spine_frr = Path(tmpdir, "configs", "frr", "spine1.conf")

            assert parse_network_interfaces(spine_cfg) == ["Ethernet0", "Ethernet4"]
            bgp_info = parse_bgp_config(spine_frr)
            assert bgp_info["local_as"] == 65001
            assert [neighbor["remote_as"] for neighbor in bgp_info["neighbors"]] == [65011, 65012]

            context = load_topology("xs", tmpdir)
            assert context.device_interfaces["spine1"] == ["Ethernet0", "Ethernet4"]
            assert context.device_asns["spine1"] == 65001
            assert len(context.bgp_neighbors["spine1"]) == 2


class TestAgentToolkit:
    """Tests for agent toolkit."""

    def test_toolkit_initialization(self):
        """Test toolkit initializes correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            topo = generate_topology("xs", tmpdir)
            toolkit = AgentToolkit(topology_metadata=topo["metadata"])
            assert toolkit is not None
            assert toolkit.grafana_url is not None
            assert toolkit.influxdb_url is not None

    def test_tool_registry_specs_complete(self):
        """Tool registry specs should define non-empty name/group/handler."""
        specs = load_tool_specs()
        assert specs, "tool specs should not be empty"
        for spec in specs:
            assert spec.name
            assert spec.group
            assert callable(spec.handler)

    def test_get_topology_returns_result(self):
        """Test get_topology returns a valid result."""
        with tempfile.TemporaryDirectory() as tmpdir:
            topo = generate_topology("xs", tmpdir)
            toolkit = AgentToolkit(topology_metadata=topo["metadata"])
            result = toolkit.get_topology()

            assert isinstance(result, ToolResult)
            assert result.success is True
            assert result.data is not None
            assert "devices" in result.data
            assert "links" in result.data

    def test_toolkit_loads_dynamic_topology(self):
        """Test toolkit can load dynamic topology from metadata."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Generate topology
            result = generate_topology("xs", tmpdir)
            metadata = result["metadata"]

            # Create toolkit with metadata
            toolkit = AgentToolkit(topology_metadata=metadata)

            assert toolkit.topology_name == "dcn"
            assert "spine1" in toolkit.container_names
            assert "leaf1" in toolkit.container_names
            assert "client1" in toolkit.container_names

    def test_reload_topology(self):
        """Test toolkit can reload topology at runtime."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Generate topology
            result = generate_topology("xs", tmpdir)

            toolkit = AgentToolkit(topology_metadata=result["metadata"])
            reload_result = toolkit.reload_topology(metadata_file=result["metadata_file"])

            assert reload_result.success is True
            assert reload_result.data["topology_name"] == "dcn"


class TestPingmeshGenerator:
    """Tests for Pingmesh pinglist generation across topology scales."""

    @pytest.mark.parametrize("scale", list(TOPOLOGY_SCALES.keys()))
    def test_pinglist_scales_with_topology(self, scale):
        """Pinglist should be N*(N-1) for N clients across all scales."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology(scale, tmpdir)
            metadata = result["metadata"]

            generator = PinglistGenerator()
            tasks = generator.generate(metadata)

            total_clients = metadata["scale"]["total_clients"]
            assert len(tasks) == total_clients * (total_clients - 1)
            assert all(t.src_name != t.dst_name for t in tasks)
            assert {t.path_type for t in tasks}.issubset({"same_rack", "cross_rack"})

    def test_xlarge_pinglist_uses_metadata_cap_when_generated_from_file(self):
        """xlarge deployment pinglists should be deterministic and bounded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology("xlarge", tmpdir)
            output_file = Path(tmpdir) / "pinglist.json"

            first = generate_pinglist_from_topology(result["metadata_file"], str(output_file))
            second = generate_pinglist_from_topology(result["metadata_file"], str(output_file))

            assert [(task.src_name, task.dst_name) for task in first] == [
                (task.src_name, task.dst_name) for task in second
            ]
            assert len(first) == 128 * 16
            per_src: dict[str, int] = {}
            per_dst: dict[str, int] = {}
            for task in first:
                per_src[task.src_name] = per_src.get(task.src_name, 0) + 1
                per_dst[task.dst_name] = per_dst.get(task.dst_name, 0) + 1
                assert task.src_name != task.dst_name
            assert set(per_src.values()) == {16}
            assert set(per_dst.values()) == {16}

            payload = json.loads(output_file.read_text(encoding="utf-8"))
            assert payload["total_probes"] == 128 * 16
            assert payload["max_dests_per_client"] == 16


class TestFastMCPServer:
    """Tests for FastMCP server tool wiring."""

    def test_fastmcp_tool_registry_exists(self):
        """FastMCP module should expose a non-empty tool registry."""
        assert len(fastmcp_server.EXPOSED_TOOLS) > 0

    def test_fastmcp_has_all_expected_tools(self):
        """FastMCP module should export all MCP tool callables."""
        expected_tools = [
            "get_topology",
            "get_device_interfaces",
            "get_bgp_neighbors",
            "get_route_table",
            "get_device_config",
            "get_bgp_rib",
            "get_device_acl",
            "get_device_logs",
            "traceroute",
            "ping_test",
            "get_interface_metrics",
            "get_all_bgp_status",
            "get_pingmesh_summary",
            "get_pingmesh_hotspots",
        ]

        for tool in expected_tools:
            assert hasattr(fastmcp_server, tool), f"Tool {tool} callable missing from fastmcp_server"
            assert tool in fastmcp_server.EXPOSED_TOOLS, f"Tool {tool} missing from EXPOSED_TOOLS"

    def test_tool_registry_matches_fastmcp_tools(self):
        """Tool registry should stay in sync with FastMCP exported tools."""
        tool_names = [spec.name for spec in load_tool_specs()]

        assert len(tool_names) == len(fastmcp_server.EXPOSED_TOOLS), "Tool count mismatch"
        for name in tool_names:
            assert name in fastmcp_server.EXPOSED_TOOLS, f"Tool {name} in definitions but not in FastMCP"


class TestFaultInjector:
    """Tests for fault injection."""

    def test_injector_initialization(self):
        """Test fault injector initializes correctly."""
        injector = FaultInjector(
            topology_metadata={"name": "dcn", "devices": {"spines": [], "leafs": [], "clients": []}}
        )
        assert injector is not None
        assert injector.topology_name == "dcn"
        assert injector.container_names == {}
        assert injector.active_faults == []

    def test_injector_loads_dynamic_topology(self):
        """Test injector can load dynamic topology."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology("xs", tmpdir)
            metadata = result["metadata"]

            injector = FaultInjector(topology_metadata=metadata)

            assert "spine1" in injector.container_names
            assert "leaf1" in injector.container_names

    def test_reload_topology(self):
        """Test injector can reload topology at runtime."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology("xs", tmpdir)

            injector = FaultInjector(
                topology_metadata={"name": "dcn", "devices": {"spines": [], "leafs": [], "clients": []}}
            )
            reload_result = injector.reload_topology(metadata_file=result["metadata_file"])

            assert reload_result["success"] is True

    def test_fault_types_defined(self):
        """Test all expected fault types are defined."""
        expected_faults = [
            "link_down",
            "link_flapping",
            "device_down",
            "bgp_neighbor_misconfig",
            "route_policy_misconfig",
            "mtu_mismatch",
            "packet_corruption",
            "packet_loss",
            "high_latency",
            "blackhole_route",
            "static_route_misconfig",
        ]

        defined_faults = [spec.name for spec in get_builtin_fault_specs()]
        for fault in expected_faults:
            assert fault in defined_faults, f"Fault type {fault} not defined"

    def test_inject_high_latency_uses_latency_ms_contract(self):
        injector = FaultInjector(topology_metadata={"devices": {"spines": [], "leafs": [], "clients": []}})

        class FakeImpairment:
            def __init__(self):
                self.calls = []

            def inject_high_latency(self, device, interface, latency_ms=100):
                self.calls.append((device, interface, latency_ms))
                return {"success": True, "type": "high_latency", "latency_ms": latency_ms}

        fake = FakeImpairment()
        injector._impairment = fake

        result = injector.inject_high_latency("leaf1", "Ethernet0", latency_ms=120)

        assert result["success"] is True
        assert fake.calls == [("leaf1", "Ethernet0", 120)]

    def test_inject_mtu_mismatch_uses_mtu_contract(self):
        injector = FaultInjector(topology_metadata={"devices": {"spines": [], "leafs": [], "clients": []}})

        class FakeImpairment:
            def __init__(self):
                self.calls = []

            def inject_mtu_mismatch(self, device, interface, mtu=1400):
                self.calls.append((device, interface, mtu))
                return {"success": True, "type": "mtu_mismatch", "mtu": mtu}

        fake = FakeImpairment()
        injector._impairment = fake

        result = injector.inject_mtu_mismatch("leaf1", "Ethernet0", mtu=1450)

        assert result["success"] is True
        assert fake.calls == [("leaf1", "Ethernet0", 1450)]

    def test_inject_packet_corruption_uses_corruption_pct_contract(self):
        injector = FaultInjector(topology_metadata={"devices": {"spines": [], "leafs": [], "clients": []}})

        class FakeImpairment:
            def __init__(self):
                self.calls = []

            def inject_packet_corruption(self, device, interface, corruption_pct=20):
                self.calls.append((device, interface, corruption_pct))
                return {"success": True, "type": "packet_corruption", "corruption_pct": corruption_pct}

        fake = FakeImpairment()
        injector._impairment = fake

        result = injector.inject_packet_corruption("leaf1", "Ethernet0", corruption_pct=17)

        assert result["success"] is True
        assert fake.calls == [("leaf1", "Ethernet0", 17)]

    def test_inject_packet_loss_uses_loss_pct_contract(self):
        injector = FaultInjector(topology_metadata={"devices": {"spines": [], "leafs": [], "clients": []}})

        class FakeImpairment:
            def __init__(self):
                self.calls = []

            def inject_packet_loss(self, device, interface, loss_pct=10):
                self.calls.append((device, interface, loss_pct))
                return {"success": True, "type": "packet_loss", "loss_pct": loss_pct}

        fake = FakeImpairment()
        injector._impairment = fake

        result = injector.inject_packet_loss("leaf1", "Ethernet0", loss_pct=11)

        assert result["success"] is True
        assert fake.calls == [("leaf1", "Ethernet0", 11)]

    def test_injector_interface_alias_resolution_supports_vendor_style(self):
        injector = FaultInjector(topology_metadata={"devices": {"spines": [], "leafs": [], "clients": []}})
        assert injector._iface.resolve_linux("ethernet-1/2") == "eth2"
        assert injector._iface.resolve_sonic("ethernet-1/2") == "Ethernet4"


class TestEvaluator:
    """Tests for evaluation system."""

    def test_evaluator_initialization(self):
        """Test evaluator initializes correctly."""
        evaluator = Evaluator()
        assert evaluator is not None
        assert sum(evaluator.weights.values()) == 1.0

    def test_evaluate_correct_answer(self):
        """Test evaluating a correct agent answer."""
        evaluator = Evaluator()

        agent_output = AgentOutput(
            verdict="fault_detected",
            fault_type="link_down",
            location={"device": "spine1", "interface": "Ethernet0"},
            confidence=0.95,
        )

        ground_truth = {"fault_type": "link_down", "location": {"device": "spine1", "interface": "Ethernet0"}}

        result = evaluator.evaluate(agent_output, ground_truth, "test_001")

        assert result.correct_verdict is True
        assert result.correct_device is True
        assert result.correct_interface is True
        assert result.correct_fault_type is True
        assert result.score == 1.0

    def test_evaluate_wrong_device(self):
        """Test evaluating answer with wrong device."""
        evaluator = Evaluator()

        agent_output = AgentOutput(
            verdict="fault_detected",
            fault_type="link_down",
            location={"device": "spine2", "interface": "Ethernet0"},  # Wrong device
            confidence=0.8,
        )

        ground_truth = {"fault_type": "link_down", "location": {"device": "spine1", "interface": "Ethernet0"}}

        result = evaluator.evaluate(agent_output, ground_truth, "test_002")

        assert result.correct_verdict is True
        assert result.correct_device is False
        assert result.score < 1.0

    def test_evaluate_fault_case_wrong_verdict_forces_zero_score(self):
        """Fault cases should earn zero score when verdict is incorrect."""
        evaluator = Evaluator()

        agent_output = AgentOutput(
            verdict="inconclusive",
            fault_type="link_down",
            location={"device": "spine1", "interface": "Ethernet0"},
            confidence=0.8,
        )

        ground_truth = {
            "fault_type": "link_down",
            "location": {"device": "spine1", "interface": "Ethernet0"},
        }

        result = evaluator.evaluate(agent_output, ground_truth, "test_wrong_verdict_zero")

        assert result.correct_verdict is False
        assert result.correct_device is True
        assert result.correct_interface is True
        assert result.score == 0.0

    def test_evaluate_link_peer_equivalent_location(self):
        """Link failures should accept either endpoint of the failed routed link."""
        evaluator = Evaluator()

        agent_output = AgentOutput(
            verdict="fault_detected",
            fault_type="link_down",
            location={"device": "leaf1", "interface": "Ethernet0"},
            confidence=0.85,
        )

        ground_truth = {
            "fault_type": "link_down",
            "location": {"device": "spine1", "interface": "e1-1"},
            "equivalent_locations": [{"device": "leaf1", "interface": "ethernet-1/1"}],
        }

        result = evaluator.evaluate(agent_output, ground_truth, "test_002_peer")

        assert result.correct_verdict is True
        assert result.correct_device is True
        assert result.correct_interface is True
        assert result.score == 1.0
        assert result.details["location_match_mode"] == "equivalent"

    def test_generate_report(self):
        """Test report generation."""
        evaluator = Evaluator()

        results = [
            EvaluationResult(
                testcase_id="test_001",
                correct_verdict=True,
                correct_device=True,
                correct_interface=True,
                correct_fault_type=True,
                score=1.0,
                details={"difficulty": "easy"},
            ),
            EvaluationResult(
                testcase_id="test_002",
                correct_verdict=True,
                correct_device=False,
                correct_interface=True,
                correct_fault_type=True,
                score=0.7,
                details={"difficulty": "medium"},
            ),
        ]

        report = evaluator.generate_report(results, "test_agent", "xs")

        assert report["agent_name"] == "test_agent"
        assert report["topology_scale"] == "xs"
        assert report["summary"]["total_cases"] == 2
        assert report["summary"]["average_score"] == 0.85

    def test_generate_report_excludes_negative_samples_from_fault_metrics(self):
        """Healthy-network cases should affect detection, not fault-localization KPIs."""
        evaluator = Evaluator()

        results = [
            EvaluationResult(
                testcase_id="fault_case",
                correct_verdict=True,
                correct_device=False,
                correct_interface=False,
                correct_fault_type=False,
                score=0.0,
                details={
                    "difficulty": "medium",
                    "ground_truth": {
                        "fault_type": "packet_loss",
                        "location": {"device": "leaf1", "interface": "Ethernet0"},
                    },
                },
            ),
            EvaluationResult(
                testcase_id="healthy_case",
                correct_verdict=True,
                correct_device=True,
                correct_interface=True,
                correct_fault_type=True,
                score=1.0,
                details={
                    "difficulty": "easy",
                    "negative_sample": True,
                    "expected_verdict": "network_healthy",
                    "agent_verdict": "network_healthy",
                },
            ),
        ]

        report = evaluator.generate_report(results, "test_agent", "xs")
        summary = report["summary"]

        assert summary["detection_accuracy"] == 1.0
        assert summary["overall_accuracy"] == 0.5
        assert summary["device_accuracy"] == 0.0
        assert summary["fault_type_accuracy"] == 0.0
        assert summary["interface_applicable_cases"] == 1
        assert summary["correct_interface"] == 0
        assert summary["negative_sample_cases"] == 1
        assert summary["positive_sample_cases"] == 1
        assert report["breakdown_by_fault_type"] == {
            "packet_loss": {"total": 1, "correct": 0, "score_sum": 0.0, "accuracy": 0.0, "avg_score": 0.0}
        }

    def test_generate_report_device_localization_requires_correct_verdict(self):
        """Primary KPI should require both correct verdict and correct device."""
        evaluator = Evaluator()

        results = [
            EvaluationResult(
                testcase_id="fault_case_wrong_verdict",
                correct_verdict=False,
                correct_device=True,
                correct_interface=True,
                correct_fault_type=True,
                score=0.0,
                details={
                    "difficulty": "medium",
                    "ground_truth": {
                        "fault_type": "packet_loss",
                        "location": {"device": "leaf1", "interface": "Ethernet0"},
                    },
                    "agent_output": {"verdict": "inconclusive"},
                },
            ),
        ]

        report = evaluator.generate_report(results, "test_agent", "xs")
        summary = report["summary"]

        assert summary["device_localization_rate"] == 0.0
        assert summary["device_accuracy"] == 0.0
        assert summary["interface_localization_rate"] == 0.0

    def test_evaluate_route_policy_alias_as_correct_fault_type(self):
        """Route-origination style answers should normalize to route_policy_misconfig."""
        evaluator = Evaluator()

        agent_output = AgentOutput(
            verdict="fault_detected",
            fault_type="route_origination_missing",
            location={"device": "leaf1"},
            confidence=0.9,
        )

        ground_truth = {
            "fault_type": "route_policy_misconfig",
            "location": {"device": "leaf1"},
        }

        result = evaluator.evaluate(agent_output, ground_truth, "test_route_policy_alias")

        assert result.correct_verdict is True
        assert result.correct_device is True
        assert result.correct_fault_type is True


class TestScenarioBenchmarking:
    """Tests for scenario-centric benchmark helpers."""

    def test_scenario_parser_rejects_unsupported_fault_type(self):
        """Scenario validation should fail for unsupported fault types."""
        invalid_yaml = """
scenario_id: invalid_fault_case
name: "Invalid fault case"
description: "invalid"
topology_scale: xs
traffic_profile: standard
metadata:
  difficulty: easy
  expected_diagnosis: link_down
episodes:
  - episode_id: ep1
    description: bad fault
    fault_type: made_up_fault
    target_device: spine1
"""

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
            tmp.write(invalid_yaml)
            tmp_path = tmp.name

        try:
            scenario = parse_scenario_file(tmp_path)
            errors = validate_scenario(scenario)
            assert any("Unsupported fault_type" in err for err in errors)
        finally:
            os.unlink(tmp_path)

    def test_score_scenario_fault_episodes_only_scores_faults(self):
        """Only non-none episodes should be scored."""
        scenario = parse_scenario_file(_generated_scenario_path("generated_link_down_xs_001.yaml"))
        evaluator = Evaluator()

        scenario_result = {
            "scenario_id": scenario.scenario_id,
            "episodes": [
                {
                    "episode": {
                        "episode_id": "ep001_baseline",
                        "fault_type": "none",
                        "target_device": "spine1",
                        "target_interface": None,
                    }
                },
                {
                    "episode": {
                        "episode_id": "ep002_link_down",
                        "fault_type": "link_down",
                        "target_device": "spine1",
                        "target_interface": "Ethernet0",
                    },
                    "diagnosis": {
                        "verdict": "fault_detected",
                        "fault_type": "link_down",
                        "location": {"device": "spine1", "interface": "Ethernet0"},
                        "confidence": 0.95,
                        "tool_calls": [],
                        "time_taken_seconds": 1.0,
                    },
                },
            ],
        }

        scored = score_scenario_fault_episodes(scenario, scenario_result, evaluator)
        assert len(scored) == 1
        assert scored[0].testcase_id == f"{scenario.scenario_id}:ep002_link_down"
        assert scored[0].score == 1.0

    def test_resolve_scenario_files_from_directory(self):
        """Scenario file resolver should return sorted YAML files by pattern."""
        with tempfile.TemporaryDirectory() as tmpdir:
            p1 = os.path.join(tmpdir, "scenario_02.yaml")
            p2 = os.path.join(tmpdir, "scenario_01.yaml")
            p3 = os.path.join(tmpdir, "note.txt")
            for path in [p1, p2, p3]:
                with open(path, "w") as f:
                    f.write("test")

            files = resolve_scenario_files(tmpdir)
            assert files == sorted([p1, p2])

    def test_topology_guard_rejects_scale_mismatch_by_default(self):
        """Strict topology guard should fail on declared/actual scale mismatch."""
        scenario = parse_scenario_file(_generated_scenario_path("generated_link_down_xs_001.yaml"))

        with tempfile.TemporaryDirectory() as tmpdir:
            metadata = {
                "name": "dcn",
                "scale": {"total_clients": 8},
                "devices": {
                    "spines": [{"name": "spine1"}, {"name": "spine2"}],
                    "leafs": [{"name": "leaf1"}, {"name": "leaf2"}],
                    "clients": [{"name": "client1"}, {"name": "client2"}],
                },
            }

            with open(os.path.join(tmpdir, "topology.json"), "w") as f:
                json.dump(metadata, f)

            result = validate_scenario_topology(
                scenario=scenario,
                topology_dir=tmpdir,
            )

            assert result["status"] == "fail"
            assert result["declared_scale"] == scenario.topology_scale
            assert result["actual_scale"] == "small"

    def test_topology_guard_accepts_e_style_interface_aliases(self):
        """e1-1 style interface labels should map to SONiC Ethernet ports."""
        scenario = parse_scenario_file(_generated_scenario_path("generated_link_down_medium_001.yaml"))

        with tempfile.TemporaryDirectory() as tmpdir:
            metadata = {
                "name": "dcn",
                "scale": {"total_clients": 16},
                "devices": {
                    "spines": [{"name": f"spine{i}"} for i in range(1, 5)],
                    "leafs": [{"name": f"leaf{i}"} for i in range(1, 9)],
                    "clients": [{"name": f"client{i}"} for i in range(1, 17)],
                },
            }

            with open(os.path.join(tmpdir, "topology.json"), "w") as f:
                json.dump(metadata, f)

            _write_config_db(tmpdir, "spine1", {"Ethernet0": []})

            result = validate_scenario_topology(
                scenario=scenario,
                topology_dir=tmpdir,
            )

            assert result["status"] == "pass"

    def test_topology_guard_accepts_vendor_style_interface_aliases(self):
        """Vendor-style ethernet-1/1 labels should also map to SONiC Ethernet ports."""
        scenario = parse_scenario_file(_generated_scenario_path("generated_link_down_small_001.yaml"))

        with tempfile.TemporaryDirectory() as tmpdir:
            metadata = {
                "name": "dcn",
                "scale": {"total_clients": 8},
                "devices": {
                    "spines": [{"name": "spine1"}, {"name": "spine2"}],
                    "leafs": [{"name": f"leaf{i}"} for i in range(1, 5)],
                    "clients": [{"name": f"client{i}"} for i in range(1, 9)],
                },
            }

            with open(os.path.join(tmpdir, "topology.json"), "w") as f:
                json.dump(metadata, f)

            _write_config_db(tmpdir, "spine1", {"Ethernet0": []})
            _write_config_db(tmpdir, "spine2", {"Ethernet4": []})

            result = validate_scenario_topology(
                scenario=scenario,
                topology_dir=tmpdir,
            )

            assert result["status"] == "pass"

    def test_configdb_payload_interfaces_are_used_by_runtime_helpers(self):
        """Runtime helpers read interface metadata from generated ConfigDB artifacts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata = {
                "name": "dcn",
                "topology_scale": "small",
                "scale": {"total_clients": 8},
                "devices": {
                    "spines": [{"name": "spine1"}],
                    "leafs": [{"name": "leaf1"}],
                    "clients": [{"name": f"client{i}"} for i in range(1, 9)],
                },
            }
            Path(tmpdir, "topology.json").write_text(json.dumps(metadata), encoding="utf-8")
            spine_config = _write_config_db(tmpdir, "spine1", {"Ethernet4": ["10.0.0.2/30"]})
            _write_config_db(tmpdir, "leaf1", {"Ethernet4": ["10.0.0.1/30"]})

            scenario = SimpleNamespace(
                scenario_id="configdb_interface_case",
                topology_scale="small",
                episodes=[
                    SimpleNamespace(
                        episode_id="ep001",
                        fault_type="link_down",
                        target_device="spine1",
                        target_interface="e1-2",
                    )
                ],
            )
            result = validate_scenario_topology(scenario=scenario, topology_dir=tmpdir)
            assert result["status"] == "pass"
            assert parse_network_interfaces(spine_config) == ["Ethernet4"]

            topo_runtime = TopologyRuntime(
                sonic=SimpleNamespace(),
                iface=SimpleNamespace(resolve_sonic=lambda interface: interface),
                ctx=SimpleNamespace(clab_dir=tmpdir, clients=[]),
            )
            assert topo_runtime.configured_device_interfaces("spine1") == ["Ethernet4"]

            from netopsbench.platform.session.scoring import build_episode_ground_truth

            ground_truth = build_episode_ground_truth(
                {"fault_type": "link_down", "target_device": "leaf1", "target_interface": "Ethernet4"},
                topology_dir=tmpdir,
            )
            assert ground_truth["equivalent_locations"] == [{"device": "spine1", "interface": "Ethernet4"}]

    def test_interface_alias_helper_keeps_scale_agnostic_equivalence(self):
        assert are_interfaces_equivalent("e1-1", "Ethernet0") is True
        assert are_interfaces_equivalent("ethernet-1/2", "Ethernet4") is True
        assert are_interfaces_equivalent("eth3", "Ethernet8") is True

    def test_score_scenario_fault_episodes_accepts_link_peer_equivalence(self):
        scenario = parse_scenario_file(_generated_scenario_path("generated_link_down_xs_001.yaml"))
        evaluator = Evaluator()
        scenario_result = {
            "episodes": [
                {
                    "episode": {
                        "episode_id": "ep002_link_down",
                        "fault_type": "link_down",
                        "target_device": "spine1",
                        "target_interface": "e1-1",
                    },
                    "diagnosis": {
                        "verdict": "fault_detected",
                        "fault_type": "link_down",
                        "location": {"device": "leaf1", "interface": "Ethernet0"},
                        "confidence": 0.9,
                        "tool_calls": [],
                        "time_taken_seconds": 1.0,
                    },
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            _write_config_db(tmpdir, "spine1", {"Ethernet0": ["192.168.11.1/30"]})
            _write_config_db(tmpdir, "leaf1", {"Ethernet0": ["192.168.11.2/30"]})

            scored = score_scenario_fault_episodes(
                scenario,
                scenario_result,
                evaluator,
                topology_dir=tmpdir,
            )

        assert len(scored) == 1
        assert scored[0].correct_device is True
        assert scored[0].correct_interface is True
        assert scored[0].score == 1.0

    @pytest.mark.parametrize("fault_type", ["packet_loss", "packet_corruption", "high_latency", "mtu_mismatch"])
    def test_score_scenario_interface_symmetric_fault_accepts_peer_endpoint(self, fault_type):
        """Interface-level faults should accept the link-peer endpoint as an equivalent answer."""
        from netopsbench.platform.session.scoring import build_episode_ground_truth

        episode_info = {
            "episode_id": "ep002_fault",
            "fault_type": fault_type,
            "target_device": "leaf1",
            "target_interface": "Ethernet0",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            # leaf1 Ethernet0 and spine1 Ethernet0 share the same /30 subnet
            _write_config_db(tmpdir, "leaf1", {"Ethernet0": ["10.0.0.1/30"]})
            _write_config_db(tmpdir, "spine1", {"Ethernet0": ["10.0.0.2/30"]})

            gt = build_episode_ground_truth(episode_info, topology_dir=tmpdir)

        assert (
            "equivalent_locations" in gt
        ), f"{fault_type} should produce equivalent_locations for symmetric interface fault"
        peer = gt["equivalent_locations"][0]
        assert peer["device"] == "spine1"
        assert peer["interface"] == "Ethernet0"


class TestEndToEnd:
    """End-to-end integration tests."""

    def test_full_flow_without_containers(self):
        """Test the full benchmark flow (without actual containers)."""
        # 1. Generate topology
        with tempfile.TemporaryDirectory() as tmpdir:
            topo_result = generate_topology("xs", tmpdir)
            assert topo_result["metadata"] is not None

            # 2. Initialize components with topology
            metadata = topo_result["metadata"]
            toolkit = AgentToolkit(topology_metadata=metadata)
            _injector = FaultInjector(topology_metadata=metadata)
            evaluator = Evaluator()

            # 3. Get topology (should work)
            topo = toolkit.get_topology()
            assert topo.success
            assert topo.data["scale"]["num_spines"] == 2
            # 4. Simulate agent output (mock)
            agent_output = AgentOutput(
                verdict="fault_detected",
                fault_type="link_down",
                location={"device": "spine1", "interface": "Ethernet0"},
                confidence=0.95,
                reasoning="Mock agent correctly identified link down",
            )

            # 5. Evaluate
            ground_truth = {"fault_type": "link_down", "location": {"device": "spine1", "interface": "Ethernet0"}}
            result = evaluator.evaluate(agent_output, ground_truth, "test_link_down")
            assert result.score == 1.0

            # 6. Generate report
            report = evaluator.generate_report([result], "mock_agent", "xs")
            assert report["summary"]["total_cases"] == 1
            assert report["summary"]["average_score"] == 1.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
