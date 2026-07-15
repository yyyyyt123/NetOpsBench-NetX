from __future__ import annotations

import subprocess

from netopsbench.platform.toolkit.toolkit import AgentToolkit
from netopsbench.platform.topology.generator import generate_topology


def _toolkit(tmp_path) -> AgentToolkit:
    metadata = generate_topology("xs", str(tmp_path))["metadata"]
    return AgentToolkit(topology_metadata=metadata)


def test_query_bgp_events_classifies_session_transitions(monkeypatch, tmp_path):
    toolkit = _toolkit(tmp_path)
    rows = [
        {
            "_measurement": "bgp_neighbors",
            "_time": "2026-07-11T00:00:00Z",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "session_state": "ESTABLISHED",
            "prefixes_received": 4,
        },
        {
            "_measurement": "bgp_neighbors",
            "_time": "2026-07-11T00:00:10Z",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "session_state": "IDLE",
        },
        {
            "_measurement": "bgp_neighbors",
            "_time": "2026-07-11T00:00:20Z",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "session_state": "ESTABLISHED",
            "prefixes_received": 3,
        },
        {"_measurement": "bgp_collection", "_time": "2026-07-11T00:00:20Z", "source": "leaf1", "collection_ok": True},
    ]
    monkeypatch.setattr(
        toolkit,
        "_query_influx_rows",
        lambda query, **kwargs: rows[:1] if "range(start: -30d" in query else rows[1:],
    )

    result = toolkit.query_bgp_events(start_time="2026-07-11T00:00:05Z", end_time="2026-07-11T00:00:30Z")

    assert result.success is True
    event = result.data["events"][0]
    assert event["event_type"] == "session_flap"
    assert event["previous_state"] == "ESTABLISHED"
    assert event["latest_state"] == "ESTABLISHED"
    assert event["states_observed"] == ["IDLE", "ESTABLISHED"]


def test_query_bgp_events_reports_non_established_and_collection_gap(monkeypatch, tmp_path):
    toolkit = _toolkit(tmp_path)
    rows = [
        {
            "_measurement": "bgp_neighbors",
            "_time": "2026-07-11T00:00:10Z",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "session_state": "IDLE",
        },
        {
            "_measurement": "bgp_collection",
            "_time": "2026-07-11T00:00:10Z",
            "source": "leaf2",
            "collection_ok": False,
            "error_type": "timeout",
        },
    ]
    monkeypatch.setattr(toolkit, "_query_influx_rows", lambda *a, **k: rows)

    result = toolkit.query_bgp_events(start_time="2026-07-11T00:00:00Z", end_time="2026-07-11T00:00:30Z")

    kinds = {(event["device"], event["event_type"]) for event in result.data["events"]}
    assert ("leaf1", "non_established_observed") in kinds
    assert ("leaf2", "collection_gap") in kinds


def test_query_bgp_events_classifies_down_and_recovery(monkeypatch, tmp_path):
    toolkit = _toolkit(tmp_path)
    prior = [
        {
            "_measurement": "bgp_neighbors",
            "_time": "2026-07-10T23:59:50Z",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "session_state": "ESTABLISHED",
        }
    ]
    window = [
        {
            "_measurement": "bgp_neighbors",
            "_time": "2026-07-11T00:00:10Z",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "session_state": "IDLE",
        },
        {"_measurement": "bgp_collection", "_time": "2026-07-11T00:00:10Z", "source": "leaf1", "collection_ok": True},
        {"_measurement": "bgp_collection", "_time": "2026-07-11T00:00:10Z", "source": "leaf2", "collection_ok": True},
        {"_measurement": "bgp_collection", "_time": "2026-07-11T00:00:10Z", "source": "spine1", "collection_ok": True},
        {"_measurement": "bgp_collection", "_time": "2026-07-11T00:00:10Z", "source": "spine2", "collection_ok": True},
    ]
    monkeypatch.setattr(toolkit, "_query_influx_rows", lambda query, **kwargs: prior if "-30d" in query else window)

    down = toolkit.query_bgp_events(start_time="2026-07-11T00:00:00Z", end_time="2026-07-11T00:00:30Z")
    assert down.data["events"][0]["event_type"] == "session_down"

    prior[0]["session_state"] = "IDLE"
    window[0]["session_state"] = "ESTABLISHED"
    recovered = toolkit.query_bgp_events(
        start_time="2026-07-11T00:00:00Z", end_time="2026-07-11T00:00:30Z", state="all"
    )
    assert recovered.data["events"][0]["event_type"] == "session_recovered"


def test_query_bgp_events_filters_role_and_limit(monkeypatch, tmp_path):
    toolkit = _toolkit(tmp_path)
    rows = [
        {
            "_measurement": "bgp_neighbors",
            "_time": "2026-07-11T00:00:10Z",
            "source": leaf,
            "neighbor_address": f"10.0.0.{index}",
            "session_state": "IDLE",
        }
        for index, leaf in enumerate(("leaf1", "leaf2"), 1)
    ]
    rows += [
        {"_measurement": "bgp_collection", "_time": "2026-07-11T00:00:10Z", "source": device, "collection_ok": True}
        for device in ("spine1", "spine2", "leaf1", "leaf2")
    ]
    monkeypatch.setattr(toolkit, "_query_influx_rows", lambda *args, **kwargs: rows)

    result = toolkit.query_bgp_events(time_range_minutes=10, role="leaf", limit=1)

    assert result.success is True
    assert result.data["returned_events"] == 1
    assert result.data["truncated"] is True
    assert result.data["events"][0]["role"] == "leaf"


def test_query_bgp_events_query_is_centralized_and_topology_scoped(monkeypatch, tmp_path):
    toolkit = _toolkit(tmp_path)
    queries = []
    monkeypatch.setattr(toolkit, "_query_influx_rows", lambda query, **kwargs: queries.append(query) or [])
    monkeypatch.setattr(
        toolkit, "_docker_exec", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("live query"))
    )

    result = toolkit.query_bgp_events(time_range_minutes=10, device="leaf1", peer="10.0.0.1")

    assert result.success is True
    assert len(queries) == 2
    assert f'r.topology_id == "{toolkit.topology_id}"' in queries[0]
    assert 'r._measurement == "bgp_neighbors"' in queries[0]
    assert 'r._measurement == "bgp_collection"' in queries[1]
    assert 'r.source == "leaf1"' in queries[0]
    assert 'r.neighbor_address == "10.0.0.1"' in queries[0]


def test_query_bgp_events_propagates_influx_failure(monkeypatch, tmp_path):
    toolkit = _toolkit(tmp_path)
    monkeypatch.setattr(toolkit, "_query_influx_rows", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))

    result = toolkit.query_bgp_events(time_range_minutes=10)

    assert result.success is False
    assert "down" in result.error


def test_get_bgp_neighbor_only_queries_requested_device_and_peer(monkeypatch, tmp_path):
    toolkit = _toolkit(tmp_path)
    calls = []

    def fake_exec(container, args, timeout):
        calls.append((container, args))
        if "neighbors" in args[-1]:
            stdout = "BGP neighbor is 10.0.0.1, remote AS 65100, local AS 65001\n  BGP state = Idle\n  Last reset due to Bad Peer AS\n"
        else:
            stdout = "Neighbor V AS MsgRcvd MsgSent TblVer InQ OutQ Up/Down State/PfxRcd\n10.0.0.1 4 65100 3 4 0 0 0 never Idle\n"
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(toolkit, "_docker_exec", fake_exec)
    result = toolkit.get_bgp_neighbor("leaf1", "10.0.0.1")

    assert result.success is True
    assert result.data["state"] == "Idle"
    assert result.data["peer_as"] == 65100
    assert "Bad Peer AS" in result.data["last_reset"]
    assert all(call[0].endswith("leaf1") for call in calls)
    assert all("10.0.0.1" in call[1][-1] or "summary" in call[1][-1] for call in calls)
