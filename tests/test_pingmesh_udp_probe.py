"""Loopback unit tests for the UDP burst probe + responder."""

from __future__ import annotations

import time

import pytest

from netopsbench.platform.pingmesh._agent_probe import UdpProbeMixin
from netopsbench.platform.pingmesh._agent_responder import UdpEchoResponder
from netopsbench.platform.pingmesh._detector_analysis import _aggregate_loss_pct
from netopsbench.platform.pingmesh.agent import (
    _default_ports_per_cycle,
    _deterministic_startup_jitter_seconds,
    _resolve_ports_per_cycle,
)


class _Harness(UdpProbeMixin):
    """Bare host class wiring the attrs UdpProbeMixin reads."""

    def __init__(
        self,
        dst_port: int,
        *,
        n_rtt_ports: int = 8,
        n_df_ports: int = 4,
        rtt_src_port_base: int = 34000,
        df_src_port_base: int = 34100,
        enable_df_probe: bool = True,
        burst_timeout_s: float = 1.0,
        rtt_ports_per_cycle: int | None = None,
        df_ports_per_cycle: int | None = None,
    ):
        self.udp_dst_port = dst_port
        self.n_rtt_ports = n_rtt_ports
        self.n_df_ports = n_df_ports
        self.rtt_src_port_base = rtt_src_port_base
        self.df_src_port_base = df_src_port_base
        self.df_payload_size = 1400
        self.burst_timeout_s = burst_timeout_s
        self.enable_df_probe = enable_df_probe
        if rtt_ports_per_cycle is not None:
            self.rtt_ports_per_cycle = rtt_ports_per_cycle
        if df_ports_per_cycle is not None:
            self.df_ports_per_cycle = df_ports_per_cycle


class _SpyHarness(_Harness):
    def __init__(self, dst_port: int, **kwargs):
        super().__init__(dst_port, **kwargs)
        self.open_calls = []

    def _open_burst_sockets(self, src_ip: str, src_port_base: int, n_ports: int, set_df: bool) -> list:
        self.open_calls.append((src_ip, src_port_base, n_ports, set_df))
        return super()._open_burst_sockets(src_ip, src_port_base, n_ports, set_df)


class _FanoutSpyHarness(_Harness):
    def __init__(self, dst_port: int, **kwargs):
        super().__init__(dst_port, **kwargs)
        self.fanout_ports = []

    def _udp_fanout(self, probes: list[dict], sockets: list, payload_size: int, timeout_s: float) -> list[dict]:
        self.fanout_ports.append([src_port for src_port, _sock in sockets])
        return [
            {
                "sent": len(sockets),
                "received": len(sockets),
                "rtts": [0.1] * len(sockets),
                "mtu_drops": 0,
            }
            for _probe in probes
        ]


@pytest.fixture()
def loopback_responder():
    # Pick a high port unlikely to clash with system services.
    port = 38434
    responder = UdpEchoResponder(bind_ip="127.0.0.1", port=port)
    responder.start()
    # Give the responder a beat to enter recvfrom().
    time.sleep(0.05)
    try:
        yield port
    finally:
        responder.stop()


@pytest.fixture()
def loopback_responder_set():
    port = 38436
    bind_ips = ["127.0.0.1", "127.0.0.2", "127.0.0.3"]
    responders = []
    for bind_ip in bind_ips:
        responder = UdpEchoResponder(bind_ip=bind_ip, port=port)
        responder.start()
        responders.append(responder)
    time.sleep(0.05)
    try:
        yield port, bind_ips
    finally:
        for responder in responders:
            responder.stop()


def _probe(dst_ip: str, index: int, src_ip: str = "127.0.0.1") -> dict:
    return {
        "src_ip": src_ip,
        "src_name": "client-src",
        "src_rack": "rack-a",
        "src_leaf": "leaf-a",
        "dst_ip": dst_ip,
        "dst_name": f"client-dst-{index}",
        "dst_rack": "rack-b",
        "dst_leaf": "leaf-b",
        "path_type": "cross_rack",
    }


def test_udp_rtt_burst_loopback(loopback_responder):
    port = loopback_responder
    harness = _Harness(dst_port=port)
    try:
        result = harness.udp_rtt_burst(dst_ip="127.0.0.1", src_ip="127.0.0.1")
    finally:
        harness._close_udp_probe_sockets()

    assert result["packets_sent"] == harness.n_rtt_ports
    # All packets should round-trip on loopback.
    assert result["packets_lost"] == 0
    assert result["success"] is True
    assert result["rtt_min"] >= 0.0
    assert result["rtt_max"] < 1000.0  # loopback RTT is sub-millisecond
    assert result["loss_pct"] == 0.0


def test_udp_df_burst_loopback(loopback_responder):
    port = loopback_responder
    harness = _Harness(dst_port=port)
    # Loopback MTU is 65536 so a 1400-byte DF payload always fits.
    try:
        result = harness.udp_df_burst(dst_ip="127.0.0.1", src_ip="127.0.0.1")
    finally:
        harness._close_udp_probe_sockets()

    assert result["packets_sent"] == harness.n_df_ports
    assert result["packets_lost"] == 0
    assert result["success"] is True
    assert result["mtu_drops"] == 0


def test_udp_rtt_burst_no_responder():
    """Probes to an unbound port should report 100% loss without crashing."""
    harness = _Harness(dst_port=38435)
    try:
        result = harness.udp_rtt_burst(dst_ip="127.0.0.1", src_ip="127.0.0.1")
    finally:
        harness._close_udp_probe_sockets()
    assert result["packets_sent"] == harness.n_rtt_ports
    # On loopback the kernel may deliver an ICMP "port unreachable" that
    # causes recvfrom() to return ECONNREFUSED — those count as loss too.
    assert result["packets_lost"] == harness.n_rtt_ports
    assert result["success"] is False
    assert result["loss_pct"] == pytest.approx(100.0)


def test_udp_probe_cycle_fanout_loopback(loopback_responder_set):
    port, bind_ips = loopback_responder_set
    harness = _Harness(
        dst_port=port,
        n_rtt_ports=3,
        n_df_ports=2,
        rtt_src_port_base=34200,
        df_src_port_base=34300,
    )
    probes = [_probe(dst_ip, index) for index, dst_ip in enumerate(bind_ips)]

    try:
        cycle_results = harness.udp_probe_cycle(probes)
    finally:
        harness._close_udp_probe_sockets()

    assert len(cycle_results) == len(probes)
    for item in cycle_results:
        assert item["success"] is True
        result = item["result"]
        assert result["packets_sent"] == harness.n_rtt_ports
        assert result["packets_lost"] == 0
        assert result["loss_pct"] == 0.0
        assert result["df_success"] == 1
        assert result["df_loss_pct"] == 0.0
        assert result["df_mtu_drops"] == 0


def test_udp_probe_cycle_reuses_bound_source_ports(loopback_responder):
    port = loopback_responder
    harness = _SpyHarness(
        dst_port=port,
        n_rtt_ports=2,
        n_df_ports=1,
        rtt_src_port_base=34400,
        df_src_port_base=34500,
    )
    probes = [_probe("127.0.0.1", 1)]

    try:
        first = harness.udp_probe_cycle(probes)
        second = harness.udp_probe_cycle(probes)
    finally:
        harness._close_udp_probe_sockets()

    assert first[0]["result"]["packets_lost"] == 0
    assert second[0]["result"]["packets_lost"] == 0
    assert harness.open_calls == [
        ("127.0.0.1", harness.rtt_src_port_base, harness.n_rtt_ports, True),
        ("127.0.0.1", harness.df_src_port_base, harness.n_df_ports, True),
    ]


def test_udp_probe_cycle_sequence_maps_shared_dst_probes(loopback_responder):
    port = loopback_responder
    harness = _Harness(
        dst_port=port,
        n_rtt_ports=3,
        n_df_ports=1,
        rtt_src_port_base=34600,
        df_src_port_base=34700,
        enable_df_probe=False,
    )
    probes = [_probe("127.0.0.1", index) for index in range(3)]

    try:
        cycle_results = harness.udp_probe_cycle(probes)
    finally:
        harness._close_udp_probe_sockets()

    assert len(cycle_results) == len(probes)
    for item in cycle_results:
        result = item["result"]
        assert result["packets_sent"] == harness.n_rtt_ports
        assert result["packets_lost"] == 0
        assert result["df_success"] == 0
        assert result["df_loss_pct"] == 0.0


def test_udp_probe_cycle_rotates_active_source_ports():
    harness = _FanoutSpyHarness(
        dst_port=38437,
        n_rtt_ports=4,
        n_df_ports=2,
        rtt_ports_per_cycle=2,
        df_ports_per_cycle=1,
        rtt_src_port_base=34800,
        df_src_port_base=34900,
    )
    probes = [_probe("127.0.0.1", 1)]

    try:
        first = harness.udp_probe_cycle(probes)
        second = harness.udp_probe_cycle(probes)
    finally:
        harness._close_udp_probe_sockets()

    assert harness.fanout_ports == [
        [34800, 34801],
        [34900],
        [34802, 34803],
        [34901],
    ]
    assert first[0]["result"]["packets_sent"] == 2
    assert first[0]["result"]["rtt_ports_active"] == 2
    assert first[0]["result"]["rtt_ports_total"] == 4
    assert first[0]["result"]["df_packets_sent"] == 1
    assert first[0]["result"]["df_ports_active"] == 1
    assert first[0]["result"]["df_ports_total"] == 2
    assert second[0]["result"]["packets_sent"] == 2
    assert second[0]["result"]["df_packets_sent"] == 1


def test_udp_burst_compatibility_still_uses_full_port_pool(loopback_responder):
    port = loopback_responder
    harness = _Harness(
        dst_port=port,
        n_rtt_ports=3,
        n_df_ports=2,
        rtt_ports_per_cycle=1,
        df_ports_per_cycle=1,
        rtt_src_port_base=35000,
        df_src_port_base=35100,
    )
    try:
        rtt_result = harness.udp_rtt_burst(dst_ip="127.0.0.1", src_ip="127.0.0.1")
        df_result = harness.udp_df_burst(dst_ip="127.0.0.1", src_ip="127.0.0.1")
    finally:
        harness._close_udp_probe_sockets()

    assert rtt_result["packets_sent"] == harness.n_rtt_ports
    assert df_result["packets_sent"] == harness.n_df_ports


def test_pingmesh_ports_per_cycle_defaults_and_env(monkeypatch):
    monkeypatch.delenv("PINGMESH_RTT_PORTS_PER_CYCLE", raising=False)
    monkeypatch.delenv("PINGMESH_DF_PORTS_PER_CYCLE", raising=False)

    assert _default_ports_per_cycle(8) == (8, 2)
    assert _default_ports_per_cycle(16) == (6, 1)
    assert _default_ports_per_cycle(64) == (4, 1)
    assert _resolve_ports_per_cycle(
        client_count=64,
        n_rtt_ports=16,
        n_df_ports=4,
        enable_df_probe=True,
    ) == (4, 1)
    assert _resolve_ports_per_cycle(
        client_count=4,
        n_rtt_ports=6,
        n_df_ports=1,
        enable_df_probe=True,
    ) == (6, 1)

    monkeypatch.setenv("PINGMESH_RTT_PORTS_PER_CYCLE", "12")
    monkeypatch.setenv("PINGMESH_DF_PORTS_PER_CYCLE", "3")
    assert _resolve_ports_per_cycle(
        client_count=64,
        n_rtt_ports=16,
        n_df_ports=4,
        enable_df_probe=True,
    ) == (12, 3)

    monkeypatch.setenv("PINGMESH_RTT_PORTS_PER_CYCLE", "999")
    monkeypatch.setenv("PINGMESH_DF_PORTS_PER_CYCLE", "999")
    assert _resolve_ports_per_cycle(
        client_count=64,
        n_rtt_ports=16,
        n_df_ports=4,
        enable_df_probe=True,
    ) == (16, 4)
    assert _resolve_ports_per_cycle(
        client_count=64,
        n_rtt_ports=16,
        n_df_ports=4,
        enable_df_probe=False,
    ) == (16, 0)


def test_pingmesh_startup_jitter_is_deterministic():
    first = _deterministic_startup_jitter_seconds("client-17", 1.0)
    second = _deterministic_startup_jitter_seconds("client-17", 1.0)

    assert first == second
    assert 0.0 <= first < 1.0
    assert _deterministic_startup_jitter_seconds("client-17", 0.0) == 0.0


def test_loss_aggregation_prefers_ratio_of_sums():
    points = [
        {"_time": "t1", "_field": "packets_sent", "value": 10},
        {"_time": "t1", "_field": "packets_lost", "value": 0},
        {"_time": "t1", "_field": "packet_loss", "value": 0.0},
        {"_time": "t2", "_field": "packets_sent", "value": 2},
        {"_time": "t2", "_field": "packets_lost", "value": 1},
        {"_time": "t2", "_field": "packet_loss", "value": 50.0},
    ]

    assert _aggregate_loss_pct(
        points,
        pct_field="packet_loss",
        sent_field="packets_sent",
        lost_field="packets_lost",
    ) == pytest.approx((1 / 12) * 100.0)


def test_loss_aggregation_falls_back_to_pct_average():
    points = [
        {"_time": "t1", "_field": "df_loss_pct", "value": 10.0},
        {"_time": "t2", "_field": "df_loss_pct", "value": 20.0},
        {"_time": "t3", "_field": "df_loss_pct", "value": 100.0},
    ]

    assert _aggregate_loss_pct(
        points,
        pct_field="df_loss_pct",
        sent_field="df_packets_sent",
        lost_field="df_packets_lost",
    ) == pytest.approx(130.0 / 3)
    assert _aggregate_loss_pct(
        points,
        pct_field="df_loss_pct",
        sent_field="df_packets_sent",
        lost_field="df_packets_lost",
        drop_unreachable=True,
    ) == pytest.approx(15.0)
