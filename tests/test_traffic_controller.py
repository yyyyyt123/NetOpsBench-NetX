import subprocess

import pytest

from netopsbench.platform.traffic import controller as controller_mod
from netopsbench.platform.traffic.controller import TrafficController, TrafficFlow


def _flows() -> list[TrafficFlow]:
    return [
        TrafficFlow(src="client1", dst="client3", dst_ip="192.168.103.2", dst_port=5201, protocol="udp"),
        TrafficFlow(src="client1", dst="client4", dst_ip="192.168.104.2", dst_port=5202, protocol="tcp"),
        TrafficFlow(src="client2", dst="client3", dst_ip="192.168.103.2", dst_port=5201, protocol="udp"),
        TrafficFlow(src="client2", dst="client4", dst_ip="192.168.104.2", dst_port=5202, protocol="tcp"),
    ]


def _controller() -> TrafficController:
    return TrafficController(
        {
            "client1": "clab-test-client1",
            "client2": "clab-test-client2",
            "client3": "clab-test-client3",
            "client4": "clab-test-client4",
        }
    )


def test_start_matrix_batches_server_ensure_and_client_start_by_container(monkeypatch):
    calls: list[list[str]] = []

    def fake_safe_run(cmd, **kwargs):
        calls.append([str(part) for part in cmd])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(controller_mod, "safe_run", fake_safe_run)

    controller = _controller()
    flow_ids = controller.start_matrix(_flows())

    command_texts = [" ".join(call) for call in calls]
    server_calls = [text for text in command_texts if "iperf3 -s" in text]
    client_calls = [text for text in command_texts if "iperf3 -c" in text]

    assert len(flow_ids) == 4
    assert len(controller.active_flows) == 4
    assert len(server_calls) == 2
    assert len(client_calls) == 2
    assert any("192.168.103.2" in text and "192.168.104.2" in text for text in client_calls)


def test_batched_server_ensure_fails_fast_and_verifies_listeners(monkeypatch):
    calls: list[list[str]] = []

    def fake_safe_run(cmd, **kwargs):
        calls.append([str(part) for part in cmd])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(controller_mod, "safe_run", fake_safe_run)

    controller = _controller()
    controller._ensure_iperf_servers_batch("clab-test-client3", {5201, 5202})

    script = calls[0][-1]
    assert script.startswith("set -e\n")
    assert script.count("ss -lntH") == 2
    assert "required_ports='5201 5202'" in script
    assert "for port in $required_ports" in script
    assert "missing=0" in script


def test_batched_source_start_is_idempotent_for_safe_retry(monkeypatch):
    calls: list[list[str]] = []

    monkeypatch.setattr(
        controller_mod,
        "safe_run",
        lambda cmd, **kwargs: calls.append([str(part) for part in cmd]) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )

    _controller().start_matrix(_flows())

    source_scripts = [call[-1] for call in calls if "iperf3 -c" in " ".join(call)]
    assert source_scripts
    assert all("flow_running" in script for script in source_scripts)
    assert all("/tmp/netopsbench-traffic/" in script for script in source_scripts)
    assert all("/proc/$pid/cmdline" in script for script in source_scripts)
    assert all("</dev/null &" in script for script in source_scripts)
    assert all("missing=0" in script for script in source_scripts)
    assert all("pgrep -f" not in script for script in source_scripts)


def test_start_matrix_retries_transient_batch_failure_at_lower_parallelism(monkeypatch):
    attempts: dict[str, int] = {}

    def fake_safe_run(cmd, **kwargs):
        text = " ".join(str(part) for part in cmd)
        container = next(part for part in cmd if str(part).startswith("clab-test-client"))
        key = f"{container}:{'server' if 'iperf3 -s' in text else 'source'}"
        attempts[key] = attempts.get(key, 0) + 1
        if key == "clab-test-client3:server" and attempts[key] == 1:
            raise subprocess.TimeoutExpired(cmd, 15)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(controller_mod, "safe_run", fake_safe_run)

    controller = _controller()
    flow_ids = controller.start_matrix(_flows())

    assert len(flow_ids) == 4
    assert attempts["clab-test-client3:server"] == 2
    assert controller.last_start_stats.server_first_attempt_successes == 1
    assert controller.last_start_stats.server_first_attempt_failures == 1
    assert controller.last_start_stats.retry_count == 1
    assert controller.last_start_stats.timeout_count == 1
    assert controller.last_start_stats.started_flow_count == 4
    assert controller.last_start_stats.failed_flow_count == 0


def test_stop_all_kills_iperf_clients_once_per_source_container(monkeypatch):
    calls: list[list[str]] = []

    def fake_safe_run(cmd, **kwargs):
        calls.append([str(part) for part in cmd])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(controller_mod, "safe_run", fake_safe_run)

    controller = _controller()
    controller.start_matrix(_flows())
    calls.clear()

    controller.stop_all()

    command_texts = [" ".join(call) for call in calls]
    stop_calls = [text for text in command_texts if "/tmp/netopsbench-traffic" in text]
    assert len(stop_calls) == 2
    assert all("flow_running" in text for text in stop_calls)
    assert controller.active_flows == {}


def test_traffic_parallelism_env_override_and_invalid_value(monkeypatch):
    monkeypatch.setenv("NETOPSBENCH_TRAFFIC_PARALLELISM", "7")
    assert controller_mod._traffic_parallelism() == 7

    monkeypatch.setenv("NETOPSBENCH_TRAFFIC_PARALLELISM", "not-an-int")
    assert controller_mod._traffic_parallelism() == 32

    monkeypatch.delenv("NETOPSBENCH_TRAFFIC_PARALLELISM", raising=False)
    assert controller_mod._traffic_parallelism() == 32


@pytest.mark.parametrize(
    ("configured", "server", "retry"),
    [(32, 16, 4), (8, 4, 4), (1, 1, 1), (64, 16, 4)],
)
def test_controller_derives_server_and_retry_parallelism(configured, server, retry):
    controller = TrafficController({}, parallelism=configured)

    assert controller.parallelism == configured
    assert controller.server_parallelism == server
    assert controller.retry_parallelism == retry


def test_start_matrix_partial_failure_records_only_started_flows(monkeypatch):
    messages: list[str] = []

    def fake_safe_run(cmd, **kwargs):
        text = " ".join(str(part) for part in cmd)
        if "clab-test-client2" in text and "iperf3 -c" in text:
            raise subprocess.CalledProcessError(1, cmd, stderr="boom")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(controller_mod, "safe_run", fake_safe_run)
    monkeypatch.setattr(
        controller_mod.logger,
        "warning",
        lambda message, *args, **kwargs: messages.append(message % args if args else message),
    )
    monkeypatch.setenv("NETOPSBENCH_TRAFFIC_PARALLELISM", "2")

    controller = _controller()
    flow_ids = controller.start_matrix(_flows())

    assert len(flow_ids) == 2
    assert {flow.src for flow in controller.active_flows.values()} == {"client1"}
    assert any(
        "src=client2" in message
        and "dst_ip=192.168.103.2" in message
        and "protocol=udp" in message
        and "port=5201" in message
        and "boom" in message
        for message in messages
    )
