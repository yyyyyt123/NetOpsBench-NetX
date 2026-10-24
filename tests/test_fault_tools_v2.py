"""Optional tool views preserve the registry, output scope and transport limits."""

import json
import subprocess
import time

import pytest

from netopsbench.platform.toolkit._core.control import tool_deadline_scope
from netopsbench.platform.toolkit._core.device.validators import docker_exec
from netopsbench.platform.toolkit.mcp.registry import load_tool_specs, validate_tool_call
from netopsbench.platform.toolkit.toolkit import AgentToolkit
from netopsbench.platform.topology.clos_builder import build_clos_plan
from netopsbench.platform.topology.config import config_for_scale


@pytest.fixture
def toolkit():
    return AgentToolkit(topology_metadata=build_clos_plan(config_for_scale("medium")).manifest.model_dump(mode="json"))


def test_only_one_new_tool_and_optional_contracts():
    names = {tool.name for tool in load_tool_specs()}
    assert len(names) == 16
    assert "get_device_status" in names
    assert "get_pingmesh_flows" not in names
    assert validate_tool_call("traceroute", {"src": "client1", "dst_ip": "192.168.2.2", "max_hops": 32})
    assert validate_tool_call("get_route_table", {"device": "leaf1", "view": "kernel"})
    assert validate_tool_call("get_bgp_neighbor", {"device": "leaf1", "peer": "10.0.0.1", "view": "advertised_routes"})


def test_acl_collection_failure_is_not_an_empty_healthy_ruleset(toolkit, monkeypatch):
    calls = []

    def failed(container, args, timeout):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="permission denied")

    monkeypatch.setattr(toolkit, "_docker_exec", failed)
    result = toolkit.get_device_acl("leaf1")
    assert not result.success
    assert len(calls) == 3
    assert len(result.data["collection_errors"]) == 3
    assert result.data["collected_at"]


def test_kernel_fib_has_actual_multipath_and_prefix_filter(toolkit, monkeypatch):
    calls = []

    def run(container, args, timeout):
        calls.append(args)
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(
                [{"dst": "192.168.2.0/30", "nexthops": [{"gateway": "10.0.0.1"}, {"gateway": "10.0.0.2"}]}]
            ),
        )

    monkeypatch.setattr(toolkit, "_docker_exec", run)
    result = toolkit.get_route_table("leaf1", prefix="192.168.2.0/30", view="kernel")
    assert result.success
    assert len(result.data["routes"][0]["nexthops"]) == 2
    assert calls == [["ip", "-j", "-4", "route", "show", "exact", "192.168.2.0/30"]]


def test_advertised_routes_filtered_locally_with_one_command(toolkit, monkeypatch):
    calls = []

    def run(container, args, timeout):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({"advertisedRoutes": {"192.168.1.0/30": {}, "192.168.2.0/30": {}}})
        )

    monkeypatch.setattr(toolkit, "_docker_exec", run)
    result = toolkit.get_bgp_neighbor("leaf1", "10.0.0.1", view="advertised_routes", prefix="192.168.2.0/30")
    assert result.success
    assert set(result.data["advertised_routes"]) == {"192.168.2.0/30"}
    assert len(calls) == 1


def test_pingmesh_client_filter_preserves_aggregation_and_window(toolkit, monkeypatch):
    calls = []
    monkeypatch.setattr(toolkit, "_query_influx_rows", lambda query, **kw: calls.append(query) or [])
    toolkit.set_pingmesh_time_window("2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z")
    result = toolkit.get_pingmesh_summary(src_client="client1", dst_client="client2")
    assert result.success
    assert 'r.src_name == "client1"' in calls[0]
    assert 'r.dst_name == "client2"' in calls[0]
    assert 'group(columns: ["path_type", "_field"])' in calls[0]
    assert "2026-10-12T00:00:00Z" in calls[0]
    assert not toolkit.get_pingmesh_summary(src_client="leaf1").success


def test_prefix_count_mode_ignores_session_state_filter(toolkit, monkeypatch):
    queries = []
    monkeypatch.setattr(toolkit, "_query_influx_rows", lambda query, **kw: queries.append(query) or [])
    result = toolkit.query_bgp_events(event_type="prefix_count", state="not_a_session_state")
    assert result.success
    assert "bgp_prefix_events" in queries[0]
    assert "session_state ==" not in queries[0]
    assert not toolkit.query_bgp_events(state="not_a_session_state").success


def test_traceroute_hop_limit(toolkit, monkeypatch):
    calls = []

    def run(container, args, timeout):
        calls.append((args, timeout))
        return subprocess.CompletedProcess(args, 0, stdout="trace")

    monkeypatch.setattr(toolkit, "_docker_exec", run)
    assert toolkit.traceroute("client1", "192.168.2.2").success
    assert calls[0][0][-2] == "8" and calls[0][1] == 12
    assert toolkit.traceroute("client1", "192.168.2.2", max_hops=32).success
    assert not toolkit.traceroute("client1", "192.168.2.2", max_hops=33).success


def test_transport_receives_total_session_deadline(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "netopsbench.platform.toolkit._core.device.validators.safe_run", lambda *a, **kw: calls.append(kw)
    )
    deadline = time.monotonic() + 0.2
    with tool_deadline_scope(deadline):
        docker_exec("container", ["traceroute"], timeout=36)
    assert calls[0]["control"].deadline == deadline
    with pytest.raises(TimeoutError):
        with tool_deadline_scope(time.monotonic() - 1):
            docker_exec("container", ["true"], timeout=30)
